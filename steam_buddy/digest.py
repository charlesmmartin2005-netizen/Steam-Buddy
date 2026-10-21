"""News for the games you care about, and the weekly digest.

Nothing here calls Claude: the digest is built from your data and the store, so
it's free, quick and fine to run on a schedule. tools.py exposes the news and
"worth going back to" parts to the chat as well.
"""

from __future__ import annotations

import html
import math
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from .profile import DAY, DORMANT_DAYS, TasteProfile
from .steam_client import NewsItem, SteamAPIError, SteamClient

if TYPE_CHECKING:
    from .tools import SteamTools

NEWS_DAYS = 14  # news window for the games you're playing
FOLLOW_GAMES = 4  # games whose news the digest shows
NEWS_PER_GAME = 2
NEWS_FETCH = 15  # posts fetched per game; developer posts are mixed in with press articles
REVISIT_MIN_HOURS = 5  # stopped games worth suggesting you go back to
REVISIT_CANDIDATES = 8  # how many of them get their news checked (one request each, cached an hour)
BIG_UPDATE_HALF_LIFE = 30  # days: a big update this old pulls you back half as hard as one from today
SAME_STORY = 0.8  # press headlines this similar are one story syndicated twice
TITLE_CHARS = 110  # press headlines can run to a paragraph

# Developer posts that don't change the game: sale ads ("40% Off Sale Ending Soon!"),
# award campaigns, stream schedules, and news about mobile versions.
_NOT_AN_UPDATE = re.compile(
    r"\bsale\b|\d+\s?%\s?off|\bdiscount|\bdeals?\b|\bbundle\b|\bfree weekend\b|\bvot(e|ing)\b|\bawards?\b"
    r"|\bnominat|\bgiveaway|\bmerch|\b(dev|live)?stream\b|\bdevstream|\btwitch\b"
    r"|\s[x×]\s",  # brand tie-ins: "Iceblade of Narin x Alienware"
    re.I,
)
# Mobile-version news, unless it's also about the PC game: "1.4.5.7 - Out Now for PC! (Console/Mobile soon)".
_MOBILE = re.compile(r"\bmobile\b|\bios\b|\bandroid\b", re.I)
_PC = re.compile(r"\bpc\b|\bsteam\b", re.I)
# Media and their contents, which would otherwise leave image URLs in the text.
# Developer posts that bring something new to play: a named update, an expansion, a new season...
_BIG_UPDATE = re.compile(
    r"\bupdate\b|\bexpansion\b|\bdlc\b|\bseason\b|\bchapter\b|\boverhaul|\brework|\blaunch(es|ed)?\b|\bnew\b"
    r"|(?<![$£€\d.])\bv?\d+\.\d+",
    re.I,
)
# Saying it has arrived settles it, even next to "console version coming soon".
_ARRIVED = re.compile(r"\bout now\b|\bnow (available|live)\b|\bis (live|here|out)\b", re.I)
# ...but not news of one that hasn't arrived yet, routine patches, or the soundtrack.
_NOT_BIG = re.compile(
    r"hotfix|preview|sneak peek|teaser|trailer|roadmap|coming|\bsoon\b|overview|devlog|dev ?update|dev diary"
    r"|state of the|behind the scenes|q ?& ?a|survey|soundtrack|\bost\b|single release|release date|store update"
    r"|stable (update|release)|monthly|maintenance|known issues|\bservers?\b",
    re.I,
)
_MEDIA = re.compile(
    r"\[(img|previewyoutube|video)\b[^\]]*\].*?\[/\1\]|\[img\b[^\]]*\]|<(img|video|iframe)\b[^>]*>|\{STEAM_CLAN_IMAGE\}\S*",
    re.I | re.S,
)
_HEADING_END = re.compile(r"\[/h\d\]|</h\d>", re.I)
_TAG = re.compile(r"<[^>]+>|\[/?[a-zA-Z0-9*]+(?:[= ][^\]]*)?\]")


def clean_text(text: str, limit: int = 400) -> str:
    """Strip HTML, entities and Steam's BBCode from store and news text.

    Every tag becomes a space so words on either side don't run together, and a
    heading gets a colon: "[h2]Fixes[/h2]Magnet works" reads "Fixes: Magnet works".
    """
    text = _MEDIA.sub(" ", html.unescape(text or ""))
    text = _HEADING_END.sub(": ", text)
    text = " ".join(_TAG.sub(" ", text).split())
    text = re.sub(r"\s+([.,!?;:])(?=\s|$)", r"\1", text)  # "[b]The big one[/b]." left a space before the stop
    text = re.sub(r"([.!?:;,]):(\s|$)", r"\1\2", text).strip(" :")  # "Fixes!:" or a heading with no text after it
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def is_update(item: NewsItem) -> bool:
    """A post by the developer that changes the game, so not a sale ad or a stream schedule."""
    t = item.title
    return item.official and not _NOT_AN_UPDATE.search(t) and not (_MOBILE.search(t) and not _PC.search(t))


def is_big_update(item: NewsItem) -> bool:
    """An update that adds something to play: new content, an expansion, a named update. Judged by its title."""
    t = item.title
    return is_update(item) and bool(_ARRIVED.search(t) or (_BIG_UPDATE.search(t) and not _NOT_BIG.search(t)))


def _same_story(a: str, b: str) -> bool:
    return SequenceMatcher(None, a.lower(), b.lower()).ratio() >= SAME_STORY


def _mostly_latin(text: str) -> bool:
    """Press feeds include other languages; keep the ones the user can likely read."""
    letters = [c for c in text if c.isalpha()]
    return not letters or sum(c.isascii() for c in letters) / len(letters) >= 0.7


@dataclass
class NewsPost:
    title: str
    date: str  # YYYY-MM-DD
    url: str
    summary: str
    official: bool  # the developer's own announcement, not press
    big_update: bool = False  # new content, an expansion or a named update, going by the title

    @classmethod
    def from_item(cls, i: NewsItem, summary_len: int = 300) -> NewsPost:
        day = datetime.fromtimestamp(i.date, timezone.utc).strftime("%Y-%m-%d")
        return cls(i.title, day, i.url, clean_text(i.contents, summary_len), i.official, is_big_update(i))


def recent_posts(
    client: SteamClient, appid: int, *, since: float, limit: int = NEWS_PER_GAME, always_press: bool = False
) -> list[NewsPost]:
    """Posts since `since`: the developer's updates first, then press in a readable language.

    Press coverage is mostly about what the developer just announced, so it only
    fills in when the developer posted nothing, unless `always_press`. Press
    headlines that are near-copies of each other count once.
    """
    items = [i for i in client.get_news(appid, count=NEWS_FETCH) if i.date >= since]
    updates = [i for i in items if is_update(i)]
    press: list[NewsItem] = []
    if always_press or not updates:
        for i in items:
            if not i.official and _mostly_latin(i.title) and not any(_same_story(i.title, p.title) for p in press):
                press.append(i)
    return [NewsPost.from_item(i) for i in (updates + press)[:limit]]


def followed_games(client: SteamClient, profile: TasteProfile, n: int = FOLLOW_GAMES) -> list[tuple[int, str, float]]:
    """Games played in the last two weeks, most hours first: (appid, name, hours in two weeks)."""
    software = set(profile.excluded_software)
    recent = sorted(client.get_recently_played(), key=lambda g: -g.playtime_2weeks)
    return [(g.appid, g.name, round(g.playtime_2weeks / 60, 1)) for g in recent if g.name not in software][:n]


@dataclass
class Revisit:
    appid: int
    name: str
    hours: float
    last_played_days_ago: int
    why_stopped: str  # how the profile sees it
    updates: list[NewsPost]  # developer updates since you last played: big ones first, then newest first
    more_updates: bool  # every post fetched was newer, so there are probably more
    pull: float = 0.0  # how strongly a recent big update calls you back; 0 without one


def revisit_candidates(
    client: SteamClient, profile: TasteProfile, now: float, n: int = REVISIT_CANDIDATES
) -> list[Revisit]:
    """Games you put real time into and stopped, the ones with the strongest reason to go back first.

    A big update since you left counts most, and the newer it is the more it
    counts (half as much after BIG_UPDATE_HALF_LIFE days). Your hours only add a
    little on top (logarithmically), so a fresh expansion for a 100-hour game
    beats a music single for a 300-hour one. After those come games with any
    update, then the rest, by hours. Updates are counted since you last played,
    not since the last digest, so a skipped week loses nothing.
    """
    finished = {g.appid for g in profile.completed}
    seen: set[int] = set()
    pool = []
    for why, games in (
        ("likely abandoned", profile.abandoned),
        ("open-ended game you stopped", profile.stopped_endless),
        ("not played in a while", profile.most_played),
    ):
        for g in games:
            days = g.last_played_days_ago
            if g.appid in seen or g.appid in finished or g.hours < REVISIT_MIN_HOURS:
                continue
            if days is None or days < DORMANT_DAYS:
                continue
            seen.add(g.appid)
            pool.append((why, g))
    pool.sort(key=lambda x: -x[1].hours)

    out = []
    for why, g in pool[:n]:
        since = now - g.last_played_days_ago * DAY
        try:
            items = client.get_news(g.appid, count=NEWS_FETCH)
        except SteamAPIError:
            items = []
        newer = [i for i in items if i.date >= since]
        big = [i.date for i in newer if is_big_update(i)]
        pull = 0.0
        if big:
            age_days = max(0.0, now - max(big)) / DAY
            pull = 0.5 ** (age_days / BIG_UPDATE_HALF_LIFE) * (1 + math.log(g.hours / REVISIT_MIN_HOURS))
        updates = sorted((NewsPost.from_item(i, 200) for i in newer if is_update(i)), key=lambda p: not p.big_update)
        out.append(Revisit(g.appid, g.name, g.hours, g.last_played_days_ago, why, updates,
                           more_updates=bool(items) and len(newer) == len(items), pull=round(pull, 2)))
    out.sort(key=lambda r: (-r.pull, not r.updates, -r.hours))
    return out


@dataclass
class Digest:
    day: date
    picks: list[dict[str, Any]]
    gems: list[dict[str, Any]]
    news: list[dict[str, Any]]  # {"appid", "game", "hours_2_weeks", "posts": [NewsPost]}
    revisit: Revisit | None
    backlog: dict[str, Any] | None  # suggested when no game you stopped has updates
    notes: list[str] = field(default_factory=list)


def build_digest(tools: SteamTools, *, now: float | None = None) -> Digest:
    """Three picks, two hidden gems, news for what you're playing, one game to go back to.

    A section that fails is left empty with a note, so one flaky endpoint doesn't
    sink the whole digest. A private profile still raises, since nothing works then.
    """
    now = now or time.time()
    notes: list[str] = []

    def attempt(label: str, fn, default):
        try:
            return fn()
        except (SteamAPIError, ValueError) as e:  # ValueError covers ToolInputError
            notes.append(f"Couldn't load {label}: {e}")
            return default

    profile = tools.profile()
    picks = attempt("recommendations", lambda: tools.recommend_games(max_results=3)["games"], [])
    pick_ids = {p["appid"] for p in picks}
    gems = attempt(
        "hidden gems",
        lambda: [g for g in tools.find_hidden_gems(max_results=6)["games"] if g["appid"] not in pick_ids][:2],
        [],
    )
    news = attempt(
        "news",
        lambda: [
            {"appid": appid, "game": name, "hours_2_weeks": h,
             "posts": recent_posts(tools.client, appid, since=now - NEWS_DAYS * DAY)}
            for appid, name, h in followed_games(tools.client, profile)
        ],
        [],
    )
    revisits = attempt("games to revisit", lambda: revisit_candidates(tools.client, profile, now), [])
    revisit = revisits[0] if revisits and revisits[0].updates else None
    backlog = None if revisit else attempt("your backlog", tools.best_backlog_game, None)
    if not revisit and not backlog and revisits:
        revisit = revisits[0]  # no updates anywhere, but still the best one to go back to
    # The profile's own caveats (games without tags or achievements) belong to the chat, not a weekly read.
    return Digest(date.fromtimestamp(now), picks, gems, news, revisit, backlog, notes)


# -- Markdown -------------------------------------------------------------------


def _md(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>#|])", r"\\\1", text)


def _link(text: str, url: str) -> str:
    # Press URLs can hold spaces ("externalpost/Rock, Paper, Shotgun/..."), which break a Markdown link.
    return f"[{_md(text)}]({quote(url, safe=':/?#@!$&*+,;=%~')})"


def _title(t: str) -> str:
    return t if len(t) <= TITLE_CHARS else t[: TITLE_CHARS - 1].rstrip() + "…"


def _short_date(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d:%b} {d.day}"


def _hours(h: float) -> str:
    return f"{h:,.1f}".rstrip("0").rstrip(".") + "h"


def _why(g: dict[str, Any]) -> str:
    because = g.get("because_you_played") or []
    if not because:
        tags = g.get("fits_your_tags") or g.get("shares_tags") or []
        return f"Fits your {', '.join(tags)} games." if tags else ""
    names = " and ".join(f"{_md(b['game'])} ({_hours(b['hours'])})" for b in because[:2])
    shared = because[0]["shared_tags"][:3]
    return f"Like {names}" + (f": {', '.join(shared)}." if shared else ".")


def _game_block(g: dict[str, Any], n: int) -> list[str]:
    facts = []
    if g.get("price"):
        facts.append(g["price"] + (f", {g['discount_percent']}% off" if g.get("discount_percent") else ""))
    facts.append(f"{g['positive_pct']:.0f}% positive of {g['reviews']:,} reviews")
    if g.get("early_access"):
        facts.append("Early Access")
    head = f"{n}. **{_md(g['name'])}** ({_md(', '.join(facts))})"
    detail = _why(g)
    if g.get("session_style") in ("short runs", "long sessions"):
        detail += f" Suits {g['session_style']}."
    if g.get("tags_you_tend_to_drop"):
        detail += f" Heads-up: you tend to drop {', '.join(g['tags_you_tend_to_drop'])} games."
    if not detail.strip():
        return [head]
    return [head + "\\", f"   {detail.strip()}"]  # a trailing backslash is a Markdown line break


def to_markdown(d: Digest) -> str:
    out = ["# Steam Buddy weekly digest", "", f"_{d.day:%A}, {d.day:%B} {d.day.day}, {d.day.year}_", ""]

    out += ["## Top picks for you", ""]
    for i, g in enumerate(d.picks, 1):
        out += _game_block(g, i)
    if not d.picks:
        out.append("No picks this week.")
    out.append("")

    out += ["## Hidden gems", ""]
    for i, g in enumerate(d.gems, 1):
        out += _game_block(g, i)
    if not d.gems:
        out.append("No hidden gems this week.")
    out.append("")

    out += ["## News for what you're playing", ""]
    if not d.news:
        out += ["You haven't played anything in the last two weeks.", ""]
    for entry in d.news:
        out += [f"### {_md(entry['game'])} ({_hours(entry['hours_2_weeks'])} in the last two weeks)", ""]
        for p in entry["posts"]:
            kind = "developer update" if p.official else "press"
            summary = f": {_md(p.summary)}" if p.summary else ""
            out.append(f"- **{_md(_title(p.title))}** ({_short_date(p.date)}, {kind}){summary} {_link('Read more', p.url)}")
        if not entry["posts"]:
            out.append(f"- Nothing new in the last {NEWS_DAYS} days.")
        out.append("")

    if d.revisit:
        r = d.revisit
        out += ["## Worth going back to", ""]
        head = (f"**{_md(r.name)}**: {_hours(r.hours)}, last played {r.last_played_days_ago} days ago "
                f"({r.why_stopped}).")
        if r.updates:
            count = f"{len(r.updates)}{'+' if r.more_updates else ''}"
            noun = "update" if count == "1" else "updates"
            out += [f"{head} {count} developer {noun} since you stopped:", ""]
            out += [f"- {_short_date(p.date)}: {_link(_title(p.title), p.url)}" for p in r.updates[:4]]
        else:
            out.append(f"{head} No developer updates since then, but it's the game you spent most time on "
                       "before stopping.")
        out.append("")
    elif d.backlog:
        b = d.backlog
        out += ["## From your backlog", ""]
        played = "never played" if not b["hours"] else f"{_hours(b['hours'])} played"
        why = _why(b)
        out += [f"**{_md(b['name'])}** ({played}). {why}".rstrip(), ""]

    if d.notes:
        out += ["---", ""]
        for n in d.notes:
            out += [f"_{_md(n)}_", ""]
    return "\n".join(out)
