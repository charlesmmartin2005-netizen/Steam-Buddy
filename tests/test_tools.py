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
from steam_buddy.tools import (
    MAX_TAGS_PER_QUERY,
    TOOL_DEFINITIONS,
    TOOL_SCHEMAS,
    SteamTools,
    _clean_news,
    _wilson_lower,
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
        ]
        self.tags = {
            1: TagInfo(1, {"Rogue-like": 500, "Pixel Graphics": 200, "Action": 100}, "steamspy"),
            2: TagInfo(2, {"Rogue-like": 300, "Deckbuilding": 200}, "steamspy"),
            8: TagInfo(8, {"RPG": 300, "Open World": 200}, "steamspy"),
            9: TagInfo(9, {"Utilities": 300}, "steamspy"),
            10: TagInfo(10, {"Arcade": 300}, "steamspy"),
        }
        self.tag_games = {
            "Rogue-like": [
                tag_game(1, "Rogue Runner", 50000, 1000),  # owned
                tag_game(100, "Big Hit", 9000, 1000),  # 90%, 10k reviews
                tag_game(101, "Small Gem", 480, 20),  # 96%, 500 reviews
                tag_game(102, "Tiny", 20, 0),  # too few reviews
                tag_game(103, "Mixed Bag", 300, 200),  # 60%
                tag_game(104, "Decent", 140, 10),  # 93%, 150 reviews
            ],
            "Pixel Graphics": [tag_game(104, "Decent", 140, 10), tag_game(105, "Pixel Only", 9500, 500)],
        }
        self.tag_calls = []
        self.fail_details = False

    def get_owned_games(self, *, refresh=False):
        return self.owned

    def get_app_tags(self, appid, *, refresh=False):
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
    # 9,000 of 10,000 positive (90%) outranks 140 of 150 (93%): far more reviews behind it.
    assert ids.index(100) < ids.index(104)
    assert out["excluded_owned"] == 1


def test_similar_games_prefer_games_matching_more_tags(tools):
    out = call(tools, "find_similar_games", tags=["Rogue-like", "Pixel Graphics"])
    assert out["games"][0]["appid"] == 104
    assert out["games"][0]["matched_tags"] == ["Rogue-like", "Pixel Graphics"]


def test_similar_games_from_an_appid_use_its_top_tags(tools, fake):
    call(tools, "find_similar_games", appid=1)
    assert fake.tag_calls == ["Rogue-like", "Pixel Graphics", "Action"]


def test_tag_queries_are_capped(tools, fake):
    call(tools, "find_similar_games", tags=["a", "b", "c", "d", "e"])
    assert len(fake.tag_calls) == MAX_TAGS_PER_QUERY


def test_hidden_gems_filters_by_score_and_review_count(tools):
    out = call(tools, "find_hidden_gems", tags=["Rogue-like"])
    assert [g["appid"] for g in out["games"]] == [101, 104]
    assert out["criteria"]["review_count_between"] == [50, 5000]


def test_hidden_gems_need_tags(tools):
    text, is_error = tools.run("find_hidden_gems", {})
    assert is_error and "tags" in text


def test_search_flags_owned_games(tools):
    out = call(tools, "search_store", query="rogue runner")
    assert [(r["appid"], r["owned"]) for r in out["results"]] == [(1, True), (200, False)]


def test_backlog_skips_software_and_lists_unplayed_first(tools):
    out = call(tools, "get_backlog")
    assert [g["name"] for g in out["games"]] == ["Backlog Epic", "Quick Arcade"]
    assert out["games"][0]["status"] == "never played"
    assert out["games"][0]["session_style"] == "long sessions"
    assert out["games"][1]["session_style"] == "short runs"


def test_wishlist_marks_sales_in_wishlist_order(tools):
    out = call(tools, "get_wishlist")
    assert [(i["name"], i["on_sale"]) for i in out["items"]] == [("Sale Game", True), ("Wanted Game", False)]
    assert out["items"][0]["discount_percent"] == 75


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


def test_clean_news_strips_markup():
    raw = "[h1]Patch 1.2[/h1]<p>Fixed &amp; improved [url=https://x]stuff[/url]</p>"
    assert _clean_news(raw) == "Patch 1.2 Fixed & improved stuff"
    assert _clean_news("word " * 200, limit=20).endswith("…")


def test_wilson_rewards_more_reviews_at_the_same_score():
    assert _wilson_lower(90, 100) < _wilson_lower(900, 1000)
    assert _wilson_lower(10, 10) < _wilson_lower(950, 1000)
    assert _wilson_lower(0, 0) == 0
