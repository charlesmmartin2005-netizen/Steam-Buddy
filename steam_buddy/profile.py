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

from .steam_client import AchievementProgress, OwnedGame, SteamAPIError, SteamClient, TagInfo

DAY = 24 * 60 * 60

BOUNCE_MAX_MINUTES = 60  # played, but less than this: you bounced off it
JUST_STARTED_DAYS = 14  # ...unless all of that was in the last two weeks
ACTIVE_DAYS = 30  # last played within this many days: still playing
DORMANT_DAYS = 90  # not played for this long: finished, abandoned, or resting
COMPLETED_PCT = 70  # achievements unlocked at or above this: likely finished
ABANDONED_PCT = 30  # dormant and below this: likely abandoned
RECENT_DAYS = 60  # window for "recent trends"
TAGS_PER_GAME = 10  # only a game's strongest tags count toward your profile

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


@dataclass
class LibraryData:
    """Everything build_profile needs, fetched once."""

    games: list[OwnedGame]
    tags: dict[int, TagInfo]
    achievements: dict[int, AchievementProgress | None]
    now: float
    notes: list[str] = field(default_factory=list)


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
    bounced_tags: list[str]  # tags shared by 2+ games you bounced off
    active: list[GameSummary]
    completed: list[GameSummary]
    abandoned: list[GameSummary]
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
        notes.append(f"{fallback} games had no SteamSpy tags, so their store genres were used (or nothing).")

    achievements: dict[int, AchievementProgress | None] = {}
    failed = 0
    for i, g in enumerate(played, 1):
        if progress:
            progress("achievements", i, len(played))
        try:
            achievements[g.appid] = client.get_achievements(g.appid)
        except SteamAPIError:
            achievements[g.appid] = None
            failed += 1
    if failed:
        notes.append(f"Couldn't read achievements for {failed} games.")

    return LibraryData(games=games, tags=tags, achievements=achievements, now=now or time.time(), notes=notes)


# ---------------------------------------------------------------------------
# Scoring (pure)


def _game_tags(info: TagInfo | None) -> dict[str, float]:
    """A game's top tags with strength 0-1 (its strongest tag = 1)."""
    if not info or not info.tags:
        return {}
    top = sorted(info.tags.items(), key=lambda kv: -kv[1])[:TAGS_PER_GAME]
    best = max(v for _, v in top) or 1
    return {t: v / best for t, v in top if t not in NON_DESCRIPTIVE_TAGS}


def _is_software(info: TagInfo | None) -> bool:
    if not info or not info.tags:
        return False
    top5 = sorted(info.tags.items(), key=lambda kv: -kv[1])[:5]
    return any(t in SOFTWARE_TAGS for t, _ in top5)


def _weight(game: OwnedGame) -> float:
    # Square root so one 1,000-hour game doesn't drown out everything else.
    return math.sqrt(game.hours)


def _days_ago(ts: int | None, now: float) -> int | None:
    return None if not ts else max(0, int((now - ts) // DAY))


def _score_tags(games: list[OwnedGame], tags: dict[int, TagInfo]) -> dict[str, TagScore]:
    scores: dict[str, float] = defaultdict(float)
    hours: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)
    contributions: dict[str, list[tuple[float, str]]] = defaultdict(list)
    for g in games:
        w = _weight(g)
        for tag, strength in _game_tags(tags.get(g.appid)).items():
            scores[tag] += w * strength
            hours[tag] += g.hours
            counts[tag] += 1
            contributions[tag].append((w * strength, g.name))
    return {
        tag: TagScore(
            tag=tag,
            score=round(scores[tag], 2),
            hours=round(hours[tag], 1),
            games=counts[tag],
            top_games=[name for _, name in sorted(contributions[tag], reverse=True)[:3]],
        )
        for tag in scores
    }


def _shares(games: list[OwnedGame], tags: dict[int, TagInfo]) -> dict[str, float]:
    """Each tag's share of total tag score, 0-1."""
    scored = _score_tags(games, tags)
    total = sum(s.score for s in scored.values()) or 1
    return {t: s.score / total for t, s in scored.items()}


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
    tag_scores = sorted(_score_tags(engaged, data.tags).values(), key=lambda s: -s.score)

    bounced_tag_counts: dict[str, int] = defaultdict(int)
    for g in bounced:
        for t in _game_tags(data.tags.get(g.appid)):
            bounced_tag_counts[t] += 1
    bounced_tags = [t for t, c in sorted(bounced_tag_counts.items(), key=lambda kv: -kv[1]) if c >= 2][:8]

    # Status of each game you stuck with.
    active, completed, abandoned = [], [], []
    for g in engaged:
        days = _days_ago(g.last_played, now)
        ach = data.achievements.get(g.appid)
        if g.playtime_2weeks > 0 or (days is not None and days <= ACTIVE_DAYS):
            active.append(g)
        elif ach and ach.percent >= COMPLETED_PCT:
            completed.append(g)
        elif ach and ach.percent < ABANDONED_PCT and (days is None or days >= DORMANT_DAYS):
            abandoned.append(g)
    no_ach = sum(1 for g in engaged if data.achievements.get(g.appid) is None)
    if no_ach:
        notes.append(
            f"{no_ach} of the games you've played have no achievement data, so they can't be "
            "judged finished or abandoned."
        )

    # Recent trends: tags that make up more of recent play than of all-time play.
    recent = [
        g for g in engaged
        if g.playtime_2weeks > 0 or ((d := _days_ago(g.last_played, now)) is not None and d <= RECENT_DAYS)
    ]
    rising: list[str] = []
    if recent:
        overall, recent_share = _shares(engaged, data.tags), _shares(recent, data.tags)
        deltas = {t: s - overall.get(t, 0) for t, s in recent_share.items() if s >= 0.02}
        rising = [t for t, d in sorted(deltas.items(), key=lambda kv: -kv[1]) if d > 0.005][:5]

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
        gt = set(_game_tags(tags.get(g.appid)))
        if gt & SHORT_SESSION_TAGS:
            short += w
        if gt & LONG_SESSION_TAGS:
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
