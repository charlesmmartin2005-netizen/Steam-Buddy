import inspect
import json
import time

import pytest

from steam_buddy.steam_client import (
    AppDetails,
    OwnedGame,
    Price,
    ReviewSummary,
    SteamAPIError,
    TagGame,
    TagInfo,
    WishlistItem,
)
from steam_buddy.recommend import wilson_lower
from steam_buddy.tools import (
    MAX_TAGS_PER_QUERY,
    TOOL_DEFINITIONS,
    TOOL_SCHEMAS,
    SteamTools,
    _clean_text,
)

NOW = time.time()
DAY = 86400


def owned(appid, name, minutes, days_ago=None):
    return OwnedGame(appid, name, minutes, 0, None if days_ago is None else int(NOW - days_ago * DAY))


def tag_game(appid, name, positive, negative, price=999, discount=0):
    return TagGame(appid, name, positive, negative, "20,000 .. 50,000", price, discount)


def details(appid, name, **kw):
    base = dict(
        appid=appid, name=name, type="game", short_description="", genres=[], categories=[],
        is_free=False, price_final=999, price_initial=999, discount_percent=0,
        price_formatted="$9.99", release_date="1 Jan, 2024", coming_soon=False,
        developers=[], publishers=[], metacritic=None,
    )
    return AppDetails(**{**base, **kw})


class FakeClient:
    """Stands in for SteamClient with canned data."""

    def __init__(self):
        self.owned = [
            owned(1, "Rogue Runner", 12000, 5),
            owned(2, "Deck Delver", 6000, 200),
            owned(8, "Backlog Epic", 0),
            owned(9, "Wallpaper Thing", 0),
            owned(10, "Quick Arcade", 30, 300),
            owned(11, "Arcade Two", 20, 200),
        ]
        self.tags = {
            1: TagInfo(1, {"Rogue-like": 500, "Pixel Graphics": 200, "Action": 100}, "steamspy"),
            2: TagInfo(2, {"Rogue-like": 300, "Deckbuilding": 200}, "steamspy"),
            8: TagInfo(8, {"RPG": 300, "Open World": 200}, "steamspy"),
            9: TagInfo(9, {"Utilities": 300}, "steamspy"),
            10: TagInfo(10, {"Arcade": 300}, "steamspy"),
            11: TagInfo(11, {"Arcade": 300}, "steamspy"),
            # candidates found through tag lists
            100: TagInfo(100, {"Rogue-like": 500, "Action": 200}, "steamspy"),
            101: TagInfo(101, {"Rogue-like": 300, "Deckbuilding": 200, "Early Access": 50}, "steamspy"),
            103: TagInfo(103, {"Rogue-like": 300}, "steamspy"),
            104: TagInfo(104, {"Pixel Graphics": 300, "Rogue-like": 100}, "steamspy"),
            105: TagInfo(105, {"Pixel Graphics": 300, "Arcade": 200}, "steamspy"),
        }
        self.tag_games = {
            "Rogue-like": [
                tag_game(1, "Rogue Runner", 50000, 1000),  # owned
                tag_game(100, "Big Hit", 9000, 1000),  # 90%, 10k reviews
                tag_game(101, "Small Gem", 480, 20, price=499),  # 96%, 500 reviews
                tag_game(102, "Tiny", 20, 0),  # too few reviews
                tag_game(103, "Mixed Bag", 300, 200),  # 60%
                tag_game(104, "Decent", 140, 10),  # 93%, 150 reviews
            ],
            "Pixel Graphics": [tag_game(104, "Decent", 140, 10), tag_game(105, "Pixel Only", 9500, 500)],
            "Deckbuilding": [tag_game(101, "Small Gem", 480, 20, price=499)],
        }
        self.tag_calls = []
        self.app_tag_calls = []
        self.fail_details = False

    def get_owned_games(self, *, refresh=False):
        return self.owned

    def get_app_tags(self, appid, *, refresh=False):
        self.app_tag_calls.append(appid)
        return self.tags.get(appid, TagInfo(appid, {}, "store"))

    def get_achievements(self, appid, *, refresh=False):
        return None

    def get_tag_games(self, tag, *, refresh=False):
        self.tag_calls.append(tag)
        return self.tag_games.get(tag, [])

    def search_store(self, term, *, refresh=False):
        return [{"appid": 1, "name": "Rogue Runner", "price_final": None},
                {"appid": 200, "name": "Rogue Runner 2", "price_final": 1999}]

    def get_app_details(self, appid, *, refresh=False):
        if self.fail_details:
            raise SteamAPIError("store timed out")
        names = {300: "Wanted Game", 301: "Sale Game", 104: "Decent"}
        return details(appid, names.get(appid, f"Game {appid}"))

    def get_review_summary(self, appid, *, refresh=False):
        return ReviewSummary(appid, 8, "Very Positive", 140, 10, 150)

    def get_wishlist(self, *, refresh=False):
        return [WishlistItem(300, 2, 100), WishlistItem(301, 1, 200)]

    def get_prices(self, appids, *, refresh=False):
        return {
            300: Price(300, 1999, 1999, 0, "$19.99", "USD"),
            301: Price(301, 499, 1999, 75, "$4.99", "USD"),
        }


@pytest.fixture
def fake():
    return FakeClient()


@pytest.fixture
def tools(fake):
    return SteamTools(fake)


def call(tools, name, **args):
    text, is_error = tools.run(name, args)
    assert not is_error, text
    return json.loads(text)


def test_similar_games_skip_owned_and_use_library_spelling(tools, fake):
    out = call(tools, "find_similar_games", tags=["roguelike"])
    # "roguelike" is looked up under the spelling the user's library uses.
    assert fake.tag_calls == ["Rogue-like"]
    ids = [g["appid"] for g in out["games"]]
    assert 1 not in ids  # owned
    assert 102 not in ids  # under 100 reviews
    # Mostly-roguelike Big Hit beats Decent, which is mostly pixel art.
    assert ids[0] == 100
    assert ids.index(100) < ids.index(104)
    assert out["excluded_owned"] == 1
    assert out["games"][0]["shares_tags"] == ["Rogue-like"]


def test_similar_games_prefer_games_matching_more_tags(tools):
    out = call(tools, "find_similar_games", tags=["Rogue-like", "Pixel Graphics"])
    assert out["games"][0]["appid"] == 104
    assert out["games"][0]["shares_tags"] == ["Pixel Graphics", "Rogue-like"]


def test_similar_games_from_an_appid_use_its_specific_tags(tools, fake):
    call(tools, "find_similar_games", appid=1)
    # Action is too broad to search (tens of thousands of games), but still counts in the match.
    assert fake.tag_calls == ["Rogue-like", "Pixel Graphics"]


def test_tag_queries_are_capped(tools, fake):
    call(tools, "find_similar_games", tags=["a", "b", "c", "d", "e"])
    assert len(fake.tag_calls) == MAX_TAGS_PER_QUERY


def test_hidden_gems_filters_by_score_and_review_count(tools):
    out = call(tools, "find_hidden_gems", tags=["Rogue-like"])
    assert {g["appid"] for g in out["games"]} == {101, 104}
    assert out["criteria"]["review_count_between"] == [50, 5000]


def test_hidden_gems_default_to_your_top_tags(tools, fake):
    out = call(tools, "find_hidden_gems")
    assert set(fake.tag_calls) == {"Rogue-like", "Pixel Graphics", "Deckbuilding"}  # Action skipped as too broad
    gem = next(g for g in out["games"] if g["appid"] == 101)
    assert gem["early_access"] is True
    # Small Gem is a roguelike deckbuilder: Deck Delver is the closest thing you played.
    assert gem["because_you_played"][0] == {"game": "Deck Delver", "hours": 100.0,
                                            "shared_tags": ["Rogue-like", "Deckbuilding"]}


def test_recommendations_skip_owned_weak_and_noisy_games(tools, fake):
    out = call(tools, "recommend_games")
    ids = {g["appid"] for g in out["games"]}
    assert ids == {100, 101, 105}  # 1 owned, 103 at 60%, 104 under 200 reviews
    assert all(g["match"] > 0 for g in out["games"])


def test_recommendations_flag_tags_you_drop(tools):
    out = call(tools, "recommend_games")
    pixel_only = next(g for g in out["games"] if g["appid"] == 105)
    # You bounced off two arcade games, so it scores lower and says why.
    assert pixel_only["tags_you_tend_to_drop"] == ["Arcade"]
    assert out["games"][-1]["appid"] == 105


def test_recommendations_respect_a_price_cap(tools):
    out = call(tools, "recommend_games", max_price_usd=5)
    assert [g["appid"] for g in out["games"]] == [101]
    assert out["games"][0]["price"] == "$4.99 (SteamSpy, may be out of date)"


def test_live_store_prices_replace_steamspy_ones(tools, fake):
    fake.get_prices = lambda appids, **kw: {101: Price(101, 999, 999, 0, "$9.99", "USD")}
    # SteamSpy still shows Small Gem at $4.99, but the sale has ended, so it no longer fits under $5.
    assert call(tools, "recommend_games", max_price_usd=5)["games"] == []
    gem = next(g for g in call(tools, "recommend_games")["games"] if g["appid"] == 101)
    assert (gem["price"], gem["discount_percent"]) == ("$9.99", 0)


def test_search_flags_owned_games(tools):
    out = call(tools, "search_store", query="rogue runner")
    assert [(r["appid"], r["owned"]) for r in out["results"]] == [(1, True), (200, False)]


def test_backlog_skips_software_and_lists_unplayed_first(tools):
    out = call(tools, "get_backlog")
    assert [g["name"] for g in out["games"]] == ["Backlog Epic", "Arcade Two", "Quick Arcade"]
    assert out["games"][0]["status"] == "never played"
    assert out["games"][0]["session_style"] == "long sessions"
    assert out["games"][1]["session_style"] == "short runs"


def test_wishlist_marks_sales_in_wishlist_order(tools):
    out = call(tools, "get_wishlist")
    assert [(i["name"], i["on_sale"]) for i in out["items"]] == [("Sale Game", True), ("Wanted Game", False)]
    assert out["items"][0]["discount_percent"] == 75
    assert out["items"][0]["reviews"]["summary"] == "Very Positive"


def test_game_details_report_ownership(tools):
    out = call(tools, "get_game_details", appid=104)
    assert out["owned"] is False
    assert out["reviews"]["total"] == 150


def test_taste_profile_lists_are_trimmed(tools):
    out = call(tools, "get_taste_profile")
    assert out["total_games"] >= 1
    assert all(len(v) <= 15 for v in out.values() if isinstance(v, list))


def test_run_rejects_bad_calls(tools):
    for name, args, expected in [
        ("delete_library", {}, "unknown tool"),
        ("get_wishlist", {"user": "me"}, "unexpected arguments"),
        ("get_game_details", {"appid": "abc"}, "whole number"),
        ("get_game_details", {"appid": True}, "whole number"),
        ("get_backlog", {"max_hours": -1}, "between"),
    ]:
        text, is_error = tools.run(name, args)
        assert is_error, name
        assert expected in json.loads(text)["error"]


def test_run_reports_steam_failures_as_tool_errors(tools, fake):
    fake.fail_details = True
    text, is_error = tools.run("get_game_details", {"appid": 104})
    assert is_error
    assert "store timed out" in json.loads(text)["error"]


def test_run_contains_unexpected_bugs(tools, fake):
    fake.get_wishlist = lambda **kw: 1 / 0
    text, is_error = tools.run("get_wishlist", {})
    assert is_error
    assert "ZeroDivisionError" in json.loads(text)["error"]


def test_schemas_match_tool_methods():
    assert [t["name"] for t in TOOL_DEFINITIONS] == list(TOOL_SCHEMAS)
    for name, schema in TOOL_SCHEMAS.items():
        params = inspect.signature(getattr(SteamTools, name)).parameters
        args = {p for p in params if p != "self"}
        assert args == set(schema["properties"]), name
        required = {p for p in args if params[p].default is inspect.Parameter.empty}
        assert required == set(schema["required"]), name
        assert schema["additionalProperties"] is False


def test_clean_text_strips_markup():
    raw = "[h1]Patch 1.2[/h1]<p>Fixed &amp; improved [url=https://x]stuff[/url]</p>"
    assert _clean_text(raw) == "Patch 1.2 Fixed & improved stuff"
    assert _clean_text("word " * 200, limit=20).endswith("…")


def test_wilson_rewards_more_reviews_at_the_same_score():
    assert wilson_lower(90, 100) < wilson_lower(900, 1000)
    assert wilson_lower(10, 10) < wilson_lower(950, 1000)
    assert wilson_lower(0, 0) == 0


def test_game_details_description_is_plain_text(tools, fake):
    fake.get_app_details = lambda appid, **kw: details(appid, "HOLE", short_description="Casual &amp; Hard <b>FPS</b>")
    assert call(tools, "get_game_details", appid=5)["description"] == "Casual & Hard FPS"
