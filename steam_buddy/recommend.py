"""Ranking candidate games against your taste.

SteamSpy's per-tag lists give a big pool of candidates but only names and review
counts. Ranking happens in two steps:

1. Pre-rank the whole pool cheaply: how strongly the tags a game was found under
   count in your profile, then how sure we are that its reviews are good.
2. For the best SHORTLIST of those, fetch the game's own tags (one SteamSpy
   request each, cached a week) and score how closely they match the target:
   your whole taste profile, or one game for "games like X".

Tags you tend to drop lower a game's score instead of hiding it, so Claude can
still mention a strong pick and say why it might not land.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from .profile import TasteProfile, _game_tags, _is_software
from .steam_client import SteamAPIError, TagGame, TagInfo, normalize_tags, tag_key

SHORTLIST = 15  # candidates whose own tags get fetched
SEED_TAGS = 4  # how many of your top tags seed the candidate pool
TASTE_TAGS = 30  # how many of your top tags make up your taste vector
DROP_PENALTY = 0.1  # score lost per tag you tend to drop among a game's top 5

# Umbrella tags carried by tens of thousands of games (Action: ~39,000). They still
# count when scoring, but a pool seeded from them is mostly noise.
BROAD_TAGS = {
    "Action", "Adventure", "Casual", "Simulation", "Strategy", "RPG", "Multiplayer",
    "Singleplayer", "Indie", "2D", "3D", "Colorful", "Cute", "Atmospheric", "Fantasy",
    "Funny", "Exploration", "Story Rich", "First-Person", "Third Person", "Sci-fi",
    "Free to Play", "Early Access", "Co-op", "Online Co-Op", "PvP", "PvE", "Violent",
    "Shooter", "Female Protagonist", "Relaxing", "Family Friendly",
}
_BROAD = {tag_key(t) for t in BROAD_TAGS}


def wilson_lower(pos: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the positive-review rate: rewards both high scores and enough reviews."""
    if total == 0:
        return 0.0
    p = pos / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (centre - spread) / (1 + z * z / total)


def taste_vector(profile: TasteProfile) -> dict[str, float]:
    """Your top tags as tag_key -> share of your playtime-weighted play."""
    top = profile.top_tags[:TASTE_TAGS]
    total = sum(t.score for t in top) or 1.0
    return {tag_key(t.tag): t.score / total for t in top}


def game_vector(info: TagInfo | None) -> dict[str, float]:
    """A game's top tags as tag_key -> strength 0-1."""
    return {tag_key(t): w for t, w in _game_tags(info).items()}


def seed_tags(tags: list[str], n: int = SEED_TAGS) -> list[str]:
    """The first n specific tags; broad ones only if there's nothing else."""
    specific = [t for t in tags if tag_key(t) not in _BROAD]
    return (specific or tags)[:n]


def cosine(a: dict[str, float], b: dict[str, float]) -> float:
    dot = sum(v * b.get(k, 0.0) for k, v in a.items())
    norm = math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values()))
    return dot / norm if norm else 0.0


def is_early_access(info: TagInfo | None) -> bool:
    return bool(info) and any(tag_key(t) == "earlyaccess" for t in list(normalize_tags(info.tags))[:20])


@dataclass
class Scored:
    game: TagGame
    info: TagInfo | None
    match: float  # 0-1, how closely the game's tags fit the target
    score: float  # match adjusted for review confidence and tags you drop
    drops: list[str]  # tags among its top 5 that you tend to drop
    early_access: bool


def rank(
    pool: dict[int, TagGame],
    found_under: dict[int, list[str]],
    target: dict[str, float],
    get_tags: Callable[[int], TagInfo],
    *,
    avoid: set[str] = frozenset(),
    shortlist: int = SHORTLIST,
) -> list[Scored]:
    """Score the most promising games in `pool` against `target`, best first.

    `found_under` maps each appid to the pool tags it was listed under, `avoid`
    holds tag_keys you tend to drop. Software and games whose tags can't be read
    are skipped; a failed tag lookup falls back to the tags it was found under.
    """
    pre = sorted(
        pool.values(),
        key=lambda g: (
            -sum(target.get(tag_key(t), 0.0) for t in found_under.get(g.appid, [])),
            -wilson_lower(g.positive, g.reviews),
        ),
    )
    scored: list[Scored] = []
    for g in pre[:shortlist]:
        try:
            info: TagInfo | None = get_tags(g.appid)
        except SteamAPIError:
            info = None
        if _is_software(info):
            continue
        vector = game_vector(info) or {tag_key(t): 1.0 for t in found_under.get(g.appid, [])}
        match = cosine(target, vector)
        drops = [t for t in list(_game_tags(info))[:5] if tag_key(t) in avoid]
        score = match * (0.5 + 0.5 * wilson_lower(g.positive, g.reviews)) - DROP_PENALTY * len(drops)
        scored.append(Scored(g, info, match, score, drops, is_early_access(info)))
    scored.sort(key=lambda s: -s.score)
    return scored


def shared_tags(target: dict[str, float], info: TagInfo | None, n: int = 3) -> list[str]:
    """The game's tags that count most toward the target, in the game's spelling."""
    weights = [(target.get(tag_key(t), 0.0) * w, t) for t, w in _game_tags(info).items()]
    return [t for w, t in sorted(weights, key=lambda x: -x[0]) if w > 0][:n]
