import pytest

from steam_buddy.profile import TagScore, TasteProfile
from steam_buddy.recommend import (
    closest_played,
    cosine,
    game_vector,
    is_early_access,
    rank,
    reason_tags,
    seed_tags,
    taste_vector,
)
from steam_buddy.steam_client import OwnedGame, SteamAPIError, TagGame, TagInfo


def game(appid, positive=950, negative=50):
    return TagGame(appid, f"Game {appid}", positive, negative, "", 999, 0)


def info(appid, tags):
    return TagInfo(appid, tags, "steamspy")


def test_cosine():
    assert cosine({"a": 1.0}, {"a": 2.0}) == pytest.approx(1.0)
    assert cosine({"a": 1.0}, {"b": 1.0}) == 0
    assert cosine({}, {"a": 1.0}) == 0


def test_seed_tags_skip_broad_ones_unless_nothing_else():
    assert seed_tags(["Action", "Rogue-like", "Indie", "Pixel Graphics", "RPG"], 3) == ["Rogue-like", "Pixel Graphics"]
    assert seed_tags(["Action", "RPG"], 3) == ["Action", "RPG"]


def test_taste_vector_is_shares_by_tag_key():
    p = TasteProfile(
        total_games=0, played_games=0, total_hours=0,
        top_tags=[TagScore("Rogue-like", 3, 0, 1, []), TagScore("Pixel Graphics", 1, 0, 1, [])],
        most_played=[], bounced=[], bounced_tags=[], active=[], completed=[], abandoned=[],
        stopped_endless=[], unplayed=[], recent_games=[], rising_tags=[], session_lean={},
        excluded_software=[], notes=[],
    )
    assert taste_vector(p) == {"roguelike": 0.75, "pixelgraphics": 0.25}


def test_rank_scores_closer_matches_higher():
    target = {"roguelike": 0.7, "pixelgraphics": 0.3}
    pool = {1: game(1), 2: game(2)}
    tags = {
        1: info(1, {"Rogue-like": 10, "Pixel Graphics": 5}),
        2: info(2, {"Racing": 10, "Rogue-like": 2}),
    }
    ranked = rank(pool, {1: ["Rogue-like"], 2: ["Rogue-like"]}, target, tags.__getitem__)
    assert [s.game.appid for s in ranked] == [1, 2]
    assert ranked[0].match > 0.9


def test_rank_penalises_tags_you_drop_and_skips_software():
    target = {"roguelike": 1.0}
    pool = {1: game(1), 2: game(2), 3: game(3)}
    tags = {
        1: info(1, {"Rogue-like": 10, "Multiplayer": 9}),
        2: info(2, {"Rogue-like": 10, "Strategy": 9}),
        3: info(3, {"Utilities": 10, "Rogue-like": 1}),
    }
    found = {a: ["Rogue-like"] for a in pool}
    ranked = rank(pool, found, target, tags.__getitem__, avoid={"multiplayer"})
    assert [s.game.appid for s in ranked] == [2, 1]
    assert ranked[1].drops == ["Multiplayer"]


def test_rank_only_fetches_the_shortlist():
    calls = []

    def get_tags(appid):
        calls.append(appid)
        return info(appid, {"Rogue-like": 10})

    pool = {a: game(a, positive=900 + a) for a in range(1, 51)}
    rank(pool, {a: ["Rogue-like"] for a in pool}, {"roguelike": 1.0}, get_tags, shortlist=5)
    assert len(calls) == 5
    assert set(calls) == {46, 47, 48, 49, 50}  # the best-reviewed survive the pre-rank


def test_failed_tag_lookup_falls_back_to_the_list_it_came_from():
    def get_tags(appid):
        raise SteamAPIError("SteamSpy down")

    ranked = rank({1: game(1)}, {1: ["Rogue-like"]}, {"roguelike": 1.0}, get_tags)
    assert ranked[0].match == pytest.approx(1.0)


def test_early_access_and_shared_tags():
    i = info(1, {"Rogue-like": 10, "Early Access": 3, "Pixel Graphics": 8})
    assert is_early_access(i)
    assert not is_early_access(info(2, {"Rogue-like": 1}))
    assert "earlyaccess" not in game_vector(i)  # it says how a game is sold, not what it's like
    assert reason_tags(i, {"pixelgraphics": 0.9, "roguelike": 0.1}) == ["Pixel Graphics", "Rogue-like"]


def test_reasons_prefer_specific_tags_and_only_cite_top_tags():
    i = info(1, {"Action": 10, "Bullet Hell": 9, "A": 8, "B": 7, "C": 6, "D": 5, "Pixel Graphics": 4})
    weights = {"action": 0.9, "bullethell": 0.1, "pixelgraphics": 0.9}
    # Action counts more in your taste, but Bullet Hell says more; Pixel Graphics is 7th, so it's not cited.
    assert reason_tags(i, weights) == ["Bullet Hell", "Action"]


def test_closest_played_weighs_similarity_by_hours():
    candidate = info(1, {"Bullet Hell": 10, "Action Roguelike": 8})
    played = [
        (OwnedGame(10, "Tiny Rogues", 9000, 0, None), game_vector(info(10, {"Bullet Hell": 10, "Action Roguelike": 9}))),
        (OwnedGame(11, "Short Clone", 90, 0, None), game_vector(info(11, {"Bullet Hell": 10, "Action Roguelike": 8}))),
        (OwnedGame(12, "Big Sim", 50000, 0, None), game_vector(info(12, {"Farming Sim": 10}))),
    ]
    out = closest_played(candidate, played)
    assert [(g.name, tags) for g, tags in out] == [
        ("Tiny Rogues", ["Bullet Hell", "Action Roguelike"]),
        ("Short Clone", ["Bullet Hell", "Action Roguelike"]),
    ]


def test_store_feature_categories_are_not_taste():
    i = TagInfo(1, {"Action": 1, "DualShock Controller Support": 1, "Xbox Controller Support": 1,
                    "Steam Leaderboards": 1}, "store")
    assert game_vector(i) == {"action": 1.0}
