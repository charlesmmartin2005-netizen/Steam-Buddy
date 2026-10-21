"""Build a taste profile from your Steam library.

Two steps, so the logic can be tested without the network:

    data = gather(client)          # fetch owned games, tags, achievements (cached)
    profile = build_profile(data)  # pure function: no I/O

All thresholds below are heuristics. Steam doesn't say whether you "finished"
a game or how long your sessions are, so the profile states what it inferred
and how.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any

from .steam_client import (
    AchievementProgress,
    OwnedGame,
    ProfilePrivateError,
    SteamAPIError,
    SteamClient,
    TagInfo,
    normalize_tags,
    tag_key,
)

DAY = 24 * 60 * 60

BOUNCE_MAX_MINUTES = 60  # played, but less than this: you bounced off it
JUST_STARTED_DAYS = 14  # ...unless all of that was in the last two weeks
ACTIVE_DAYS = 30  # last played within this many days: still playing
DORMANT_DAYS = 90  # not played for this long: finished, abandoned, or resting
COMPLETED_PCT = 70  # achievements unlocked at or above this: likely finished
ABANDONED_PCT = 30  # dormant and below this: likely abandoned
MIN_ACHIEVEMENTS = 5  # fewer than this and the percentage says little (CS2 has 1)
RECENT_DAYS = 60  # window for "recent trends"
TAGS_PER_GAME = 10  # only a game's strongest tags count toward your profile
TOP_TAGS = 15  # tags this high in your profile never count as ones you bounce off
PRIVATE_STOP_AFTER = 5  # this many "profile is not public" answers in a row: stop asking

# Tag sets below are matched by tag_key(), so spelling variants like
# "Rogue-like" / "Roguelike" or "Single-player" / "Singleplayer" all match.

# Tags that describe how a game is sold or received, not what it's like to play.
NON_DESCRIPTIVE_TAGS = {
    "Indie", "Singleplayer", "Great Soundtrack", "Early Access", "Free to Play",
    "Addictive", "Replay Value", "Masterpiece", "Classic", "Cult Classic",
    "Beautiful", "Family Friendly", "Moddable", "Controller", "Steam Achievements",
    "Full controller support", "Steam Cloud", "Steam Trading Cards", "Partial Controller Support",
    "Remote Play Together", "Family Sharing", "In-App Purchases", "Includes level editor",
}

# Any of these among a title's top tags marks it as software, not a game.
SOFTWARE_TAGS = {
    "Utilities", "Software", "Design & Illustration", "Animation & Modeling",
    "Video Production", "Audio Production", "Photo Editing", "Web Publishing",
    "Game Development", "Software Training", "Benchmark",
}

# Live-service and competitive games have no ending to finish or abandon.
ENDLESS_TAGS = {
    "PvP", "Massively Multiplayer", "MMORPG", "MMO", "Battle Royale", "Hero Shooter",
    "Competitive", "eSports", "Extraction Shooter", "Live Service", "MOBA",
}

# Genre tags that suggest short, repeatable runs vs long sit-down sessions.
SHORT_SESSION_TAGS = {
    "Roguelike", "Roguelite", "Action Roguelike", "Roguelike Deckbuilder", "Arcade",
    "Card Game", "Deckbuilding", "Puzzle", "Bullet Hell", "Score Attack", "Party Game",
    "Auto Battler", "Bullet Heaven", "Fighting", "Rhythm",
}
LONG_SESSION_TAGS = {
    "RPG", "Open World", "MMORPG", "Massively Multiplayer", "Grand Strategy", "4X",
    "Colony Sim", "City Builder", "Simulation", "Survival", "Sandbox", "JRPG",
    "Open World Survival Craft", "Looter Shooter", "Story Rich", "Base Building",
    "Life Sim", "God Game", "Crafting",
}

_NON_DESCRIPTIVE = {tag_key(t) for t in NON_DESCRIPTIVE_TAGS}
_ENDLESS = {tag_key(t) for t in ENDLESS_TAGS}
_SOFTWARE = {tag_key(t) for t in SOFTWARE_TAGS}
_SHORT = {tag_key(t) for t in SHORT_SESSION_TAGS}
_LONG = {tag_key(t) for t in LONG_SESSION_TAGS}


@dataclass
class LibraryData:
    """Everything build_profile needs, fetched once."""

    games: list[OwnedGame]
    tags: dict[int, TagInfo]
    achievements: dict[int, AchievementProgress | None]
    now: float
    notes: list[str] = field(default_factory=list)
    achievements_private: bool = False


@dataclass
class TagScore:
    tag: str
    score: float  # playtime-weighted strength; only meaningful relative to other tags
    hours: float  # total hours in games that carry this tag
    games: int  # how many of your played games carry it
    top_games: list[str]  # the games contributing most to this tag


@dataclass
class GameSummary:
    appid: int
    name: str
    hours: float
    last_played_days_ago: int | None
    achievement_pct: float | None = None
    top_tags: list[str] = field(default_factory=list)


@dataclass
class TasteProfile:
    total_games: int
    played_games: int
    total_hours: float
    top_tags: list[TagScore]
    most_played: list[GameSummary]
    bounced: list[GameSummary]
    bounced_tags: list[str]  # tags shared by 2+ games you bounced off, minus your top tags
    active: list[GameSummary]
    completed: list[GameSummary]
    abandoned: list[GameSummary]
    stopped_endless: list[GameSummary]  # live-service/competitive games you haven't touched in 90+ days
    unplayed: list[GameSummary]
    recent_games: list[GameSummary]
    rising_tags: list[str]  # bigger share of recent play than of all-time play
    session_lean: dict[str, Any]
    excluded_software: list[str]
    notes: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Fetching


def gather(
    client: SteamClient,
    *,
    refresh: bool = False,
    now: float | None = None,
    progress: Callable[[str, int, int], None] | None = None,
) -> LibraryData:
    """Fetch the library plus tags and achievements for every game you've played.

    The first run makes one or two requests per played game (SteamSpy allows one
    per second), so it can take a minute or two. After that everything is cached.
    """
    games = client.get_owned_games(refresh=refresh)
    played = [g for g in games if g.playtime_forever > 0]
    notes: list[str] = []

    tags: dict[int, TagInfo] = {}
    for i, g in enumerate(played, 1):
        if progress:
            progress("tags", i, len(played))
        try:
            tags[g.appid] = client.get_app_tags(g.appid)
        except SteamAPIError:
            tags[g.appid] = TagInfo(g.appid, {}, "none")
    fallback = sum(1 for t in tags.values() if t.source != "steamspy")
    if fallback:
        notes.append(f"{_plural(fallback, 'game')} had no SteamSpy tags, so their store genres were used (or nothing).")

    achievements: dict[int, AchievementProgress | None] = {}
    failed: dict[str, int] = defaultdict(int)
    private = private_streak = 0
    for i, g in enumerate(played, 1):
        if progress:
            progress("achievements", i, len(played))
        if private_streak >= PRIVATE_STOP_AFTER:
            # Steam keeps saying no; asking for every remaining game only wastes time.
            achievements[g.appid] = None
            private += 1
            continue
        try:
            achievements[g.appid] = client.get_achievements(g.appid)
            private_streak = 0
        except ProfilePrivateError:
            achievements[g.appid] = None
            private += 1
            private_streak += 1
        except SteamAPIError as e:
            achievements[g.appid] = None
            failed[str(e)] += 1
            private_streak = 0
    if private:
        notes.append(
            f"Steam wouldn't share achievements for {_plural(private, 'game')} because your Game details "
            "aren't public. Your library still loads because your API key belongs to your own "
            "account, but achievements don't get that exception. To fix it, open "
            "https://steamcommunity.com/my/edit/settings and set 'Game details' to Public."
        )
    if failed:
        reason, count = max(failed.items(), key=lambda kv: kv[1])
        notes.append(
            f"Couldn't read achievements for {_plural(sum(failed.values()), 'game')}. "
            f"Most common error ({count}x): {reason}"
        )

    return LibraryData(
        games=games,
        tags=tags,
        achievements=achievements,
        now=now or time.time(),
        notes=notes,
        achievements_private=private > 0,
    )


# ---------------------------------------------------------------------------
# Scoring (pure)


def _game_tags(info: TagInfo | None) -> dict[str, float]:
    """A game's top tags with strength 0-1 (its strongest tag = 1)."""
    if not info or not info.tags:
        return {}
    top = list(normalize_tags(info.tags).items())[:TAGS_PER_GAME]
    best = max(v for _, v in top) or 1
    return {t: v / best for t, v in top if tag_key(t) not in _NON_DESCRIPTIVE}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _is_endless(info: TagInfo | None) -> bool:
    return any(tag_key(t) in _ENDLESS for t in list(_game_tags(info))[:5])


def _is_software(info: TagInfo | None) -> bool:
    if not info or not info.tags:
        return False
    top5 = list(normalize_tags(info.tags))[:5]
    return any(tag_key(t) in _SOFTWARE for t in top5)


def session_fit(info: TagInfo | None) -> str:
    """'short runs', 'long sessions', 'either' or 'unknown', from a game's genre tags."""
    gt = {tag_key(t) for t in _game_tags(info)}
    short, long_ = bool(gt & _SHORT), bool(gt & _LONG)
    if short and long_:
        return "either"
    return "short runs" if short else "long sessions" if long_ else "unknown"


def _weight(game: OwnedGame) -> float:
    # Square root so one 1,000-hour game doesn't drown out everything else.
    return math.sqrt(game.hours)


def _days_ago(ts: int | None, now: float) -> int | None:
    return None if not ts else max(0, int((now - ts) // DAY))


def _score_tags(games: list[OwnedGame], tags: dict[int, TagInfo]) -> dict[str, TagScore]:
    """Tag scores keyed by tag_key(), so spellings that differ between games merge."""
    scores: dict[str, float] = defaultdict(float)
    hours: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    contributions: dict[str, list[tuple[float, str]]] = defaultdict(list)
    spellings: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for g in games:
        w = _weight(g)
        for tag, strength in _game_tags(tags.get(g.appid)).items():
            key = tag_key(tag)
            scores[key] += w * strength
            hours[key] += g.hours
            counts[key] += 1
            contributions[key].append((w * strength, g.name))
            spellings[key][tag] += w * strength
    return {
        key: TagScore(
            tag=max(spellings[key].items(), key=lambda kv: kv[1])[0],  # the dominant spelling
            score=round(scores[key], 2),
            hours=round(hours[key], 1),
            games=counts[key],
            top_games=[name for _, name in sorted(contributions[key], reverse=True)[:3]],
        )
        for key in scores
    }


def _shares(scored: dict[str, TagScore]) -> dict[str, float]:
    """Each tag's share of total tag score, 0-1, keyed like _score_tags."""
    total = sum(s.score for s in scored.values()) or 1
    return {k: s.score / total for k, s in scored.items()}


def build_profile(data: LibraryData) -> TasteProfile:
    now = data.now
    notes = list(data.notes)

    software = [g for g in data.games if _is_software(data.tags.get(g.appid))]
    software_ids = {g.appid for g in software}
    games = [g for g in data.games if g.appid not in software_ids]

    def summary(g: OwnedGame) -> GameSummary:
        ach = data.achievements.get(g.appid)
        return GameSummary(
            appid=g.appid,
            name=g.name,
            hours=round(g.hours, 1),
            last_played_days_ago=_days_ago(g.last_played, now),
            achievement_pct=round(ach.percent, 1) if ach else None,
            top_tags=list(_game_tags(data.tags.get(g.appid)))[:5],
        )

    unplayed = [g for g in games if g.playtime_forever == 0]
    played = [g for g in games if g.playtime_forever > 0]

    def just_started(g: OwnedGame) -> bool:
        days = _days_ago(g.last_played, now)
        return days is not None and days <= JUST_STARTED_DAYS and g.playtime_2weeks >= g.playtime_forever

    bounced = [g for g in played if g.playtime_forever < BOUNCE_MAX_MINUTES and not just_started(g)]
    bounced_ids = {g.appid for g in bounced}
    engaged = [g for g in played if g.appid not in bounced_ids]

    # Top tags come from games you actually stuck with.
    engaged_scores = _score_tags(engaged, data.tags)
    tag_scores = sorted(engaged_scores.values(), key=lambda s: -s.score)

    # Tags you keep dropping, leaving out any you also play a lot: bouncing off two
    # strategy games says little if strategy is one of your biggest genres.
    bounced_scores = _score_tags(bounced, data.tags)
    liked = {k for k, _ in sorted(engaged_scores.items(), key=lambda kv: -kv[1].score)[:TOP_TAGS]}
    bounced_tags = [
        s.tag for k, s in sorted(bounced_scores.items(), key=lambda kv: -kv[1].games)
        if s.games >= 2 and k not in liked
    ][:8]

    # Status of each game you stuck with.
    active, completed, abandoned, stopped_endless = [], [], [], []
    for g in engaged:
        days = _days_ago(g.last_played, now)
        dormant = days is None or days >= DORMANT_DAYS
        ach = data.achievements.get(g.appid)
        if ach and ach.total < MIN_ACHIEVEMENTS:
            ach = None
        if g.playtime_2weeks > 0 or (days is not None and days <= ACTIVE_DAYS):
            active.append(g)
        elif _is_endless(data.tags.get(g.appid)):
            if dormant:
                stopped_endless.append(g)
        elif ach and ach.percent >= COMPLETED_PCT:
            completed.append(g)
        elif ach and ach.percent < ABANDONED_PCT and dormant:
            abandoned.append(g)
    no_ach = sum(1 for g in engaged if data.achievements.get(g.appid) is None)
    if no_ach and not data.achievements_private:  # the privacy note already explains it
        notes.append(
            f"No achievement data for {_plural(no_ach, 'game')} you've played, so they can't be "
            "judged finished or abandoned."
        )

    # Recent trends: tags that make up more of recent play than of all-time play.
    recent = [
        g for g in engaged
        if g.playtime_2weeks > 0 or ((d := _days_ago(g.last_played, now)) is not None and d <= RECENT_DAYS)
    ]
    rising: list[str] = []
    if recent:
        recent_scores = _score_tags(recent, data.tags)
        overall, recent_share = _shares(engaged_scores), _shares(recent_scores)
        deltas = {k: s - overall.get(k, 0) for k, s in recent_share.items() if s >= 0.02}
        rising = [recent_scores[k].tag for k, d in sorted(deltas.items(), key=lambda kv: -kv[1]) if d > 0.005][:5]

    session = _session_lean(engaged, data.tags)

    total_hours = sum(g.hours for g in games)
    by_hours = lambda gs: sorted(gs, key=lambda g: -g.playtime_forever)  # noqa: E731
    by_recent = lambda gs: sorted(gs, key=lambda g: -(g.last_played or 0))  # noqa: E731
    return TasteProfile(
        total_games=len(games),
        played_games=len(played),
        total_hours=round(total_hours, 1),
        top_tags=tag_scores[:15],
        most_played=[summary(g) for g in by_hours(engaged)[:10]],
        bounced=[summary(g) for g in by_hours(bounced)],
        bounced_tags=bounced_tags,
        active=[summary(g) for g in by_recent(active)],
        completed=[summary(g) for g in by_hours(completed)],
        abandoned=[summary(g) for g in by_hours(abandoned)],
        stopped_endless=[summary(g) for g in by_recent(stopped_endless)],
        unplayed=[summary(g) for g in unplayed],
        recent_games=[summary(g) for g in by_recent(recent)],
        rising_tags=rising,
        session_lean=session,
        excluded_software=[g.name for g in software],
        notes=notes,
    )


def _session_lean(games: list[OwnedGame], tags: dict[int, TagInfo]) -> dict[str, Any]:
    """Steam doesn't report sessions, so infer a lean from the genres you play most."""
    short = long_ = total = 0.0
    for g in games:
        w = _weight(g)
        total += w
        gt = {tag_key(t) for t in _game_tags(tags.get(g.appid))}
        if gt & _SHORT:
            short += w
        if gt & _LONG:
            long_ += w
    if not total:
        return {"lean": "unknown", "short_pct": 0, "long_pct": 0, "note": "Not enough play data."}
    short_pct, long_pct = round(100 * short / total), round(100 * long_ / total)
    if short_pct >= long_pct + 15:
        lean = "short runs"
    elif long_pct >= short_pct + 15:
        lean = "long sessions"
    else:
        lean = "mixed"
    return {
        "lean": lean,
        "short_pct": short_pct,
        "long_pct": long_pct,
        "note": "Inferred from genre tags (e.g. roguelikes vs open-world RPGs), since Steam doesn't report session lengths.",
    }
