import json
import logging

import httpx
import pytest

from steam_buddy.cache import Cache
from steam_buddy.config import Secret
from steam_buddy.steam_client import (
    ProfilePrivateError,
    RateLimiter,
    SteamAPIError,
    SteamClient,
    library_warnings,
)

KEY = "0123456789ABCDEF0123456789ABCDEF"
STEAM_ID = "76561197960287930"


class FakeSteam:
    """Routes requests by path to canned responses and records what was called."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def on(self, path, *responses):
        self.routes[path] = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        queue = self.routes.get(request.url.path)
        if not queue:
            return httpx.Response(404, text="no route")
        resp = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(resp, Exception):
            raise resp
        status, body = resp
        if body is None:
            return httpx.Response(status, text="null")
        if isinstance(body, str):
            return httpx.Response(status, text=body)
        return httpx.Response(status, json=body)


@pytest.fixture
def fake():
    return FakeSteam()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def client(fake, sleeps):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    c = SteamClient(
        Secret(KEY),
        STEAM_ID,
        http=http,
        limiter=RateLimiter({}, default_interval=0, sleep=sleeps.append),
        sleep=sleeps.append,
    )
    yield c
    c.close()


OWNED = "/IPlayerService/GetOwnedGames/v1/"


def owned_response(*games):
    return 200, {"response": {"game_count": len(games), "games": list(games)}}


def test_owned_games_parsed(client, fake):
    fake.on(
        OWNED,
        owned_response(
            {"appid": 620, "name": "Portal 2", "playtime_forever": 600, "playtime_2weeks": 60, "rtime_last_played": 1700000000},
            {"appid": 10, "name": "Counter-Strike", "playtime_forever": 0},
        ),
    )
    games = client.get_owned_games()
    assert [g.appid for g in games] == [620, 10]
    assert games[0].hours == 10
    assert games[0].last_played == 1700000000
    assert games[1].last_played is None

    params = fake.calls[0].url.params
    assert params["steamid"] == STEAM_ID
    assert params["include_appinfo"] == "true"
    assert params["include_played_free_games"] == "true"


def test_private_library_detected(client, fake):
    fake.on(OWNED, (200, {"response": {}}))
    with pytest.raises(ProfilePrivateError) as exc:
        client.get_owned_games()
    assert "Game details" in str(exc.value)
    assert "steamcommunity.com/my/edit/settings" in str(exc.value)


def test_hidden_playtime_warning(client, fake):
    fake.on(OWNED, owned_response({"appid": 1, "name": "A"}, {"appid": 2, "name": "B"}))
    warnings = library_warnings(client.get_owned_games())
    assert len(warnings) == 1
    assert "Always keep my total playtime private" in warnings[0]


def test_retries_on_429_then_succeeds(client, fake, sleeps):
    fake.on(OWNED, (429, "slow down"), (503, "busy"), owned_response())
    assert client.get_owned_games() == []
    assert len(fake.calls) == 3
    assert sleeps == [1.0, 2.0]


def test_retry_after_header_respected(fake, sleeps):
    def handler(request):
        fake.calls.append(request)
        if len(fake.calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"response": {"game_count": 0}})

    http = httpx.Client(transport=httpx.MockTransport(handler))
    c = SteamClient(Secret(KEY), STEAM_ID, http=http, limiter=RateLimiter({}, default_interval=0, sleep=sleeps.append), sleep=sleeps.append)
    c.get_owned_games()
    assert sleeps == [7.0]


def test_gives_up_after_max_retries(client, fake):
    fake.on(OWNED, (500, "boom"))
    with pytest.raises(SteamAPIError):
        client.get_owned_games()
    assert len(fake.calls) == client.max_retries + 1


def test_network_errors_retried_and_never_leak_key(client, fake):
    fake.on(OWNED, httpx.ConnectError(f"failed to connect to https://api.steampowered.com/?key={KEY}"))
    with pytest.raises(SteamAPIError) as exc:
        client.get_owned_games()
    assert KEY not in str(exc.value)
    assert exc.value.__cause__ is None and exc.value.__suppress_context__


def test_bad_key_message(client, fake):
    fake.on(OWNED, (403, "<html>Access is denied</html>"))
    with pytest.raises(SteamAPIError) as exc:
        client.get_owned_games()
    assert "STEAM_API_KEY" in str(exc.value)
    assert KEY not in str(exc.value)


def test_key_not_logged(client, fake, caplog):
    fake.on(OWNED, (429, "x"), owned_response())
    with caplog.at_level(logging.DEBUG):
        client.get_owned_games()
    assert KEY not in caplog.text


def test_recent_private_vs_empty(client, fake):
    path = "/IPlayerService/GetRecentlyPlayedGames/v1/"
    fake.on(path, (200, {"response": {"total_count": 0}}))
    assert client.get_recently_played() == []
    fake.on(path, (200, {"response": {}}))
    with pytest.raises(ProfilePrivateError):
        client.get_recently_played(refresh=True)


ACH = "/ISteamUserStats/GetPlayerAchievements/v1/"


def test_achievement_progress(client, fake):
    fake.on(
        ACH,
        (200, {"playerstats": {"success": True, "achievements": [{"achieved": 1}, {"achieved": 0}, {"achieved": 1}, {"achieved": 0}]}}),
    )
    progress = client.get_achievements(620)
    assert (progress.unlocked, progress.total, progress.percent) == (2, 4, 50.0)


def test_achievements_are_not_retried_on_server_errors(client, fake, sleeps):
    fake.on(ACH, (500, {}), (500, {}))
    with pytest.raises(SteamAPIError, match="HTTP 500"):
        client.get_achievements(620)
    assert len(fake.calls) == 1 and sleeps == []


def test_achievements_no_stats_is_none(client, fake):
    fake.on(ACH, (400, {"playerstats": {"success": False, "error": "Requested app has no stats"}}))
    assert client.get_achievements(10) is None


def test_achievements_private_profile(client, fake):
    fake.on(ACH, (403, {"playerstats": {"success": False, "error": "Profile is not public"}}))
    with pytest.raises(ProfilePrivateError):
        client.get_achievements(620)


def test_app_details_parsed(client, fake):
    fake.on(
        "/api/appdetails",
        (
            200,
            {
                "620": {
                    "success": True,
                    "data": {
                        "name": "Portal 2",
                        "type": "game",
                        "short_description": "Puzzles.",
                        "genres": [{"id": "1", "description": "Action"}, {"id": "25", "description": "Adventure"}],
                        "categories": [{"id": 2, "description": "Single-player"}],
                        "is_free": False,
                        "price_overview": {"initial": 999, "final": 199, "discount_percent": 80, "final_formatted": "$1.99"},
                        "release_date": {"coming_soon": False, "date": "18 Apr, 2011"},
                        "developers": ["Valve"],
                        "metacritic": {"score": 95},
                    },
                }
            },
        ),
    )
    d = client.get_app_details(620)
    assert d.name == "Portal 2"
    assert d.genres == ["Action", "Adventure"]
    assert d.discount_percent == 80 and d.price_final == 199
    assert d.metacritic == 95
    assert fake.calls[0].url.params["cc"] == "us"


def test_app_details_missing_app(client, fake):
    fake.on("/api/appdetails", (200, {"999": {"success": False}}))
    assert client.get_app_details(999) is None
    fake.on("/api/appdetails", (200, None))
    assert client.get_app_details(998) is None


REVIEWS = "/IUserReviewsService/GetAppReviews/v1/"


def test_review_summary_from_reviews_service(client, fake):
    fake.on(
        REVIEWS,
        (200, {"response": {"query_summary": {"review_score": 9, "review_score_desc": "Overwhelmingly Positive",
                                              "total_positive": 980, "total_negative": 20, "total_reviews": 1000}}}),
    )
    r = client.get_review_summary(620)
    assert r.positive_pct == 98.0
    assert r.description == "Overwhelmingly Positive"
    sent = json.loads(fake.calls[0].url.params["input_json"])
    assert sent["appid"] == 620 and sent["review_type"] == 0 and sent["languages"] == ["all"]


def test_review_summary_falls_back_to_store(client, fake):
    fake.on(REVIEWS, (404, "gone"))
    fake.on(
        "/appreviews/620",
        (200, {"success": 1, "query_summary": {"review_score": 8, "review_score_desc": "Very Positive",
                                                "total_positive": 90, "total_negative": 10, "total_reviews": 100}}),
    )
    assert client.get_review_summary(620).total == 100


def test_tags_from_steamspy_sorted(client, fake):
    fake.on("/api.php", (200, {"appid": 620, "tags": {"Puzzle": 50, "Co-op": 90, "Funny": 10}}))
    tags = client.get_app_tags(620)
    assert tags.source == "steamspy"
    assert list(tags.tags) == ["Co-op", "Puzzle", "Funny"]


def test_tags_fall_back_to_store_and_stop_calling_steamspy(client, fake):
    fake.on("/api.php", (503, "down"))
    fake.on(
        "/api/appdetails",
        (200, {"1": {"success": True, "data": {"name": "X", "genres": [{"description": "RPG"}],
                                                "categories": [{"description": "Single-player"}]}}}),
    )
    tags = client.get_app_tags(1)
    assert tags.source == "store"
    assert set(tags.tags) == {"RPG", "Single-player"}

    spy_calls = sum(1 for c in fake.calls if c.url.path == "/api.php")
    client.get_app_tags(1)
    assert sum(1 for c in fake.calls if c.url.path == "/api.php") == spy_calls


def test_steamspy_empty_tags_list(client, fake):
    fake.on("/api.php", (200, {"appid": 5, "tags": []}))
    fake.on("/api/appdetails", (200, {"5": {"success": False}}))
    assert client.get_app_tags(5).tags == {}


def test_wishlist_and_news(client, fake):
    fake.on("/IWishlistService/GetWishlist/v1/", (200, {"response": {"items": [{"appid": 70, "priority": 1, "date_added": 1600000000}]}}))
    fake.on("/ISteamNews/GetNewsForApp/v2/", (200, {"appnews": {"appid": 620, "newsitems": [
        {"gid": "1", "title": "Patch", "url": "u", "date": 5, "feed_type": 1, "feedname": "steam_community_announcements"},
        {"gid": "2", "title": "Review", "url": "v", "date": 4, "feed_type": 0, "feedname": "pcgamer"},
    ]}}))
    assert client.get_wishlist()[0].appid == 70
    news = client.get_news(620, count=2)
    assert news[0].title == "Patch"
    assert [n.official for n in news] == [True, False]
    assert fake.calls[-1].url.params["maxlength"] == "0"  # full text; Steam's blurbs run words together
    assert "key" not in fake.calls[-1].url.params  # news doesn't need the key


def test_responses_are_cached(fake, sleeps):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    fake.on(OWNED, owned_response({"appid": 1, "name": "A", "playtime_forever": 5}))
    with Cache(":memory:") as cache:
        c = SteamClient(Secret(KEY), STEAM_ID, http=http, cache=cache, limiter=RateLimiter({}, default_interval=0, sleep=sleeps.append))
        c.get_owned_games()
        c.get_owned_games()
        assert len(fake.calls) == 1
        c.get_owned_games(refresh=True)
        assert len(fake.calls) == 2
        # The key never ends up in the cache.
        rows = cache._db.execute("SELECT kind, key, value FROM cache").fetchall()
        assert KEY not in repr(rows)


def test_private_response_not_cached(fake, sleeps):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    fake.on(OWNED, (200, {"response": {}}), owned_response())
    with Cache(":memory:") as cache:
        c = SteamClient(Secret(KEY), STEAM_ID, http=http, cache=cache, limiter=RateLimiter({}, default_interval=0, sleep=sleeps.append))
        with pytest.raises(ProfilePrivateError):
            c.get_owned_games()
        assert c.get_owned_games() == []


def test_rate_limiter_spaces_requests():
    t = [0.0]
    slept = []

    def sleep(s):
        slept.append(s)
        t[0] += s

    limiter = RateLimiter({"store.steampowered.com": 1.5}, clock=lambda: t[0], sleep=sleep)
    limiter.wait("store.steampowered.com")
    t[0] += 0.5
    limiter.wait("store.steampowered.com")
    assert slept == [pytest.approx(1.0)]
    limiter.wait("api.steampowered.com")  # other hosts aren't held up
    assert len(slept) == 1


def test_steamspy_tags_normalized(client, fake):
    fake.on("/api.php", (200, {"appid": 9, "tags": {"Multi-player": 20, "Multiplayer": 50, "Point &amp; Click": 10, "Dystopian ": 5}}))
    tags = client.get_app_tags(9).tags
    assert tags == {"Multiplayer": 50, "Point & Click": 10, "Dystopian": 5}


def test_tag_games_parsed(client, fake):
    fake.on("/api.php", (200, {
        "250900": {"appid": 250900, "name": "Binding", "positive": "900", "negative": 100,
                   "owners": "1,000,000 .. 2,000,000", "price": "1499", "discount": "50"},
        "7": {"appid": 7, "name": "Free Thing", "positive": 60, "negative": 0, "owners": "0 .. 20,000",
              "price": None, "discount": None},
        "8": {"appid": 8, "name": "Nobody Played", "positive": 3, "negative": 1, "owners": "0 .. 20,000"},
    }))
    games = {g.appid: g for g in client.get_tag_games("Rogue-like")}
    assert fake.calls[-1].url.params["tag"] == "Rogue-like"
    assert games[250900].reviews == 1000
    assert games[250900].positive_pct == 90
    assert (games[250900].price, games[250900].discount) == (1499, 50)
    assert (games[7].price, games[7].discount) == (None, 0)
    assert 8 not in games  # too few reviews to judge


def test_store_search_keeps_only_apps(client, fake):
    fake.on("/api/storesearch/", (200, {"total": 2, "items": [
        {"type": "app", "id": 413150, "name": "Stardew Valley", "price": {"final": 1499}},
        {"type": "sub", "id": 9, "name": "Bundle"},
    ]}))
    assert client.search_store("stardew") == [{"appid": 413150, "name": "Stardew Valley", "price_final": 1499}]


def test_prices_batched_and_cached(fake, sleeps):
    http = httpx.Client(transport=httpx.MockTransport(fake))
    fake.on("/api/appdetails", (200, {
        "1": {"success": True, "data": {"price_overview": {"currency": "USD", "initial": 2000, "final": 500,
                                                           "discount_percent": 75, "final_formatted": "$5.00"}}},
        "2": {"success": True, "data": []},  # free games come back with no price
        "3": {"success": False},
    }))
    with Cache(":memory:") as cache:
        c = SteamClient(Secret(KEY), STEAM_ID, http=http, cache=cache,
                        limiter=RateLimiter({}, default_interval=0, sleep=sleeps.append))
        prices = c.get_prices([1, 2, 3])
        assert fake.calls[-1].url.params["appids"] == "1,2,3"
        assert fake.calls[-1].url.params["filters"] == "price_overview"
        assert prices[1].discount_percent == 75 and prices[1].formatted == "$5.00"
        assert prices[2] is None and prices[3] is None
        assert c.get_prices([1, 2]) == {1: prices[1], 2: None}
        assert len(fake.calls) == 1
