"""Thin wrappers around the Steam Web API, the Steam store, and SteamSpy.

Endpoints were checked against the current docs (October 2026):

  Web API (api.steampowered.com, key required unless noted)
    IPlayerService/GetOwnedGames/v1            include_appinfo, include_played_free_games
    IPlayerService/GetRecentlyPlayedGames/v1   count (0 = all)
    ISteamUser/GetPlayerSummaries/v2           communityvisibilitystate: 3 = public
    ISteamUserStats/GetPlayerAchievements/v1   appid, l
    ISteamUserStats/GetSchemaForGame/v2        appid, l
    ISteamNews/GetNewsForApp/v2                appid, count, maxlength (no key needed)
    IWishlistService/GetWishlist/v1            steamid -> items[{appid, priority, date_added}]
    IUserReviewsService/GetAppReviews/v1       input_json; replaces the deprecated
                                               store.steampowered.com/appreviews (no key needed)
  Store
    store.steampowered.com/api/appdetails      appids, cc, l; with filters=price_overview
                                               it takes several comma-separated appids
    store.steampowered.com/api/storesearch     term, cc, l -> {total, items[{id, name, price?}]}
  SteamSpy
    steamspy.com/api.php?request=appdetails    tags, positive, negative; limit 1 request/second
    steamspy.com/api.php?request=tag&tag=X     {appid: {name, positive, negative, owners,
                                               price, initialprice, discount}}; prices are
                                               strings in cents

Every request goes through one rate limiter and retry loop. The API key is sent
as a query parameter, so this module never puts a request URL into an error
message, and it quiets httpx's own request logging.
"""

from __future__ import annotations

import html
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from .cache import Cache
from .config import Config, Secret

# httpx logs full request URLs (including ?key=...) at INFO level.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

log = logging.getLogger(__name__)

API_BASE = "https://api.steampowered.com"
STORE_BASE = "https://store.steampowered.com"
STEAMSPY_URL = "https://steamspy.com/api.php"

# Minimum seconds between requests to each host. The store's appdetails limit is
# roughly 200 requests per 5 minutes; SteamSpy documents 1 request per second.
MIN_INTERVAL = {
    "api.steampowered.com": 0.25,
    "store.steampowered.com": 1.5,
    "steamspy.com": 1.1,
}
RETRY_STATUSES = {429, 500, 502, 503, 504}

PRIVACY_FIX = (
    "Steam returned no library data, which means your game details are private.\n"
    "To fix it:\n"
    "  1. Go to https://steamcommunity.com/my/edit/settings (Edit Profile > Privacy Settings).\n"
    "  2. Set 'My profile' to Public.\n"
    "  3. Set 'Game details' to Public.\n"
    "  4. Untick 'Always keep my total playtime private even if users can see my game details'.\n"
    "Changes can take a few minutes to reach the API."
)

PLAYTIME_HIDDEN_FIX = (
    "Every game in your library shows 0 minutes played, so your playtime is probably hidden.\n"
    "Go to https://steamcommunity.com/my/edit/settings and untick\n"
    "'Always keep my total playtime private even if users can see my game details'."
)


class SteamAPIError(Exception):
    """A Steam request failed. Messages never include the API key or request URL."""


class ProfilePrivateError(SteamAPIError):
    def __init__(self, message: str = PRIVACY_FIX):
        super().__init__(message)


# ---------------------------------------------------------------------------
# Data shapes returned by the client


@dataclass(frozen=True)
class OwnedGame:
    appid: int
    name: str
    playtime_forever: int  # minutes
    playtime_2weeks: int  # minutes
    last_played: int | None  # unix time, None if never or unknown

    @property
    def hours(self) -> float:
        return self.playtime_forever / 60

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RecentGame:
    appid: int
    name: str
    playtime_2weeks: int
    playtime_forever: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AchievementProgress:
    appid: int
    unlocked: int
    total: int

    @property
    def percent(self) -> float:
        return 100 * self.unlocked / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "percent": round(self.percent, 1)}


@dataclass(frozen=True)
class NewsItem:
    gid: str
    title: str
    url: str
    author: str
    contents: str
    feedlabel: str
    date: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class WishlistItem:
    appid: int
    priority: int
    date_added: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AppDetails:
    appid: int
    name: str
    type: str
    short_description: str
    genres: list[str]
    categories: list[str]
    is_free: bool
    price_final: int | None  # cents
    price_initial: int | None
    discount_percent: int
    price_formatted: str | None
    release_date: str
    coming_soon: bool
    developers: list[str]
    publishers: list[str]
    metacritic: int | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ReviewSummary:
    appid: int
    score: int  # 0-9, Steam's review score bucket
    description: str  # e.g. "Very Positive"
    positive: int
    negative: int
    total: int

    @property
    def positive_pct(self) -> float:
        return 100 * self.positive / self.total if self.total else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "positive_pct": round(self.positive_pct, 1)}


@dataclass(frozen=True)
class TagGame:
    """A game from SteamSpy's per-tag list."""

    appid: int
    name: str
    positive: int
    negative: int
    owners: str  # a range, e.g. "20,000 .. 50,000"
    price: int | None  # cents
    discount: int

    @property
    def reviews(self) -> int:
        return self.positive + self.negative

    @property
    def positive_pct(self) -> float:
        return 100 * self.positive / self.reviews if self.reviews else 0.0


@dataclass(frozen=True)
class Price:
    appid: int
    final: int  # cents
    initial: int
    discount_percent: int
    formatted: str
    currency: str


@dataclass(frozen=True)
class TagInfo:
    appid: int
    tags: dict[str, int]  # tag -> vote count, highest first
    source: str  # "steamspy" or "store"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Rate limiting


class RateLimiter:
    """Enforces a minimum gap between requests to the same host."""

    def __init__(
        self,
        min_interval: dict[str, float] | None = None,
        *,
        default_interval: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.min_interval = MIN_INTERVAL if min_interval is None else min_interval
        self.default_interval = default_interval
        self._clock = clock
        self._sleep = sleep
        self._last: dict[str, float] = {}

    def wait(self, host: str) -> None:
        interval = self.min_interval.get(host, self.default_interval)
        last = self._last.get(host)
        if last is not None:
            elapsed = self._clock() - last
            if elapsed < interval:
                self._sleep(interval - elapsed)
        self._last[host] = self._clock()


# ---------------------------------------------------------------------------
# Client


class SteamClient:
    def __init__(
        self,
        api_key: Secret,
        steam_id: str,
        *,
        cache: Cache | None = None,
        country: str = "us",
        http: httpx.Client | None = None,
        limiter: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        self._key = api_key
        self.steam_id = steam_id
        self.cache = cache
        self.country = country
        self._http = http or httpx.Client(
            timeout=20.0, headers={"User-Agent": "steam-buddy/0.1 (personal use)"}
        )
        self._limiter = limiter or RateLimiter(sleep=sleep)
        self._sleep = sleep
        self.max_retries = max_retries
        self._steamspy_down = False

    @classmethod
    def from_config(cls, cfg: Config, *, cache: Cache | None = None) -> SteamClient:
        return cls(cfg.steam_api_key, cfg.steam_id, cache=cache, country=cfg.country)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> SteamClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- plumbing -----------------------------------------------------------

    def _request(self, url: str, params: dict[str, Any], *, with_key: bool = False) -> httpx.Response:
        """GET with rate limiting and retries on 429/5xx/network errors."""
        host = urlsplit(url).hostname or ""
        label = urlsplit(url).path  # safe to show: no query string
        if with_key:
            params = {**params, "key": self._key.reveal()}

        for attempt in range(self.max_retries + 1):
            self._limiter.wait(host)
            try:
                resp = self._http.get(url, params=params)
            except httpx.TransportError as e:
                if attempt == self.max_retries:
                    raise SteamAPIError(f"network error calling {host}{label}: {type(e).__name__}") from None
                self._sleep(self._backoff(attempt, None))
                continue
            if resp.status_code in RETRY_STATUSES and attempt < self.max_retries:
                delay = self._backoff(attempt, resp.headers.get("Retry-After"))
                log.info("Steam %s returned %s, retrying in %.1fs", label, resp.status_code, delay)
                self._sleep(delay)
                continue
            return resp
        raise AssertionError("unreachable")

    @staticmethod
    def _backoff(attempt: int, retry_after: str | None) -> float:
        if retry_after and retry_after.isdigit():
            return min(float(retry_after), 60.0)
        return float(2**attempt)  # 1, 2, 4, 8 seconds

    def _get_json(
        self,
        url: str,
        params: dict[str, Any],
        *,
        with_key: bool = False,
        accept: tuple[int, ...] = (200,),
    ) -> Any:
        resp = self._request(url, params, with_key=with_key)
        label = f"{urlsplit(url).hostname}{urlsplit(url).path}"
        if resp.status_code in accept:
            try:
                return resp.json()  # may be null: appdetails does that for unknown apps
            except ValueError:
                if resp.status_code == 200:
                    raise SteamAPIError(f"{label} returned a response that isn't JSON") from None
        # A bad key gets a plain-text 401/403 rather than a JSON explanation.
        if with_key and resp.status_code in (401, 403):
            raise SteamAPIError(
                f"Steam rejected the request to {label} (HTTP {resp.status_code}). "
                "Check that STEAM_API_KEY in .env is correct and hasn't been revoked."
            )
        raise SteamAPIError(f"{label} returned HTTP {resp.status_code}")

    def _cached(self, kind: str, key: str, fetch: Callable[[], Any], refresh: bool) -> Any:
        if self.cache is None:
            return fetch()
        return self.cache.get_or_fetch(kind, key, fetch, refresh=refresh)

    # -- your account -------------------------------------------------------

    def get_player_summary(self, *, refresh: bool = False) -> dict[str, Any]:
        """Persona name and profile visibility (communityvisibilitystate 3 = public)."""

        def fetch() -> dict[str, Any]:
            data = self._get_json(
                f"{API_BASE}/ISteamUser/GetPlayerSummaries/v2/",
                {"steamids": self.steam_id},
                with_key=True,
            )
            players = data.get("response", {}).get("players", [])
            if not players:
                raise SteamAPIError(f"no Steam account found for SteamID {self.steam_id}")
            p = players[0]
            return {
                "steamid": p.get("steamid"),
                "personaname": p.get("personaname", ""),
                "profileurl": p.get("profileurl", ""),
                "communityvisibilitystate": p.get("communityvisibilitystate"),
            }

        return self._cached("player_summary", self.steam_id, fetch, refresh)

    def get_owned_games(self, *, refresh: bool = False) -> list[OwnedGame]:
        """Every game you own, with total and two-week playtime in minutes.

        Raises ProfilePrivateError when Steam hides the library.
        """

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{API_BASE}/IPlayerService/GetOwnedGames/v1/",
                {
                    "steamid": self.steam_id,
                    "include_appinfo": "true",
                    "include_played_free_games": "true",
                },
                with_key=True,
            )
            resp = data.get("response", {})
            # A private library comes back as an empty "response": {} with HTTP 200.
            if "game_count" not in resp:
                raise ProfilePrivateError()
            return resp.get("games", [])

        raw = self._cached("owned_games", self.steam_id, fetch, refresh)
        return [
            OwnedGame(
                appid=g["appid"],
                name=g.get("name") or f"App {g['appid']}",
                playtime_forever=g.get("playtime_forever", 0),
                playtime_2weeks=g.get("playtime_2weeks", 0),
                last_played=g.get("rtime_last_played") or None,
            )
            for g in raw
        ]

    def get_recently_played(self, count: int = 0, *, refresh: bool = False) -> list[RecentGame]:
        """Games played in the last two weeks (count=0 returns all of them)."""

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{API_BASE}/IPlayerService/GetRecentlyPlayedGames/v1/",
                {"steamid": self.steam_id, "count": count},
                with_key=True,
            )
            resp = data.get("response", {})
            if "total_count" not in resp:
                raise ProfilePrivateError()
            return resp.get("games", [])

        raw = self._cached("recent_games", f"{self.steam_id}:{count}", fetch, refresh)
        return [
            RecentGame(
                appid=g["appid"],
                name=g.get("name") or f"App {g['appid']}",
                playtime_2weeks=g.get("playtime_2weeks", 0),
                playtime_forever=g.get("playtime_forever", 0),
            )
            for g in raw
        ]

    def get_achievements(self, appid: int, *, refresh: bool = False) -> AchievementProgress | None:
        """How many achievements you've unlocked. None if the game has no achievements."""

        def fetch() -> dict[str, int] | None:
            # Steam answers "no stats" with 400 and a private profile with 403,
            # both with a JSON body explaining why.
            data = self._get_json(
                f"{API_BASE}/ISteamUserStats/GetPlayerAchievements/v1/",
                {"steamid": self.steam_id, "appid": appid},
                with_key=True,
                accept=(200, 400, 403),
            )
            stats = data.get("playerstats", {})
            if not stats.get("success", False):
                error = str(stats.get("error", "")).lower()
                if "not public" in error or "private" in error:
                    raise ProfilePrivateError()
                if "no stats" in error or "no achievements" in error:
                    return None
                raise SteamAPIError(f"achievements for app {appid}: {stats.get('error', 'unknown error')}")
            achievements = stats.get("achievements") or []
            if not achievements:
                return None
            unlocked = sum(1 for a in achievements if a.get("achieved"))
            return {"unlocked": unlocked, "total": len(achievements)}

        raw = self._cached("achievements", f"{self.steam_id}:{appid}", fetch, refresh)
        return AchievementProgress(appid=appid, **raw) if raw else None

    def get_game_schema(self, appid: int, *, refresh: bool = False) -> list[dict[str, Any]]:
        """The game's achievement list: api name, display name, description, hidden."""

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{API_BASE}/ISteamUserStats/GetSchemaForGame/v2/",
                {"appid": appid, "l": "english"},
                with_key=True,
                accept=(200, 400),
            )
            achievements = data.get("game", {}).get("availableGameStats", {}).get("achievements", [])
            return [
                {
                    "name": a.get("name"),
                    "display_name": a.get("displayName", ""),
                    "description": a.get("description", ""),
                    "hidden": bool(a.get("hidden")),
                }
                for a in achievements
            ]

        return self._cached("game_schema", str(appid), fetch, refresh)

    def get_wishlist(self, *, refresh: bool = False) -> list[WishlistItem]:
        """Your wishlist (app ids only; look up names with get_app_details)."""

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{API_BASE}/IWishlistService/GetWishlist/v1/",
                {"steamid": self.steam_id},
                with_key=True,
            )
            return data.get("response", {}).get("items", [])

        raw = self._cached("wishlist", self.steam_id, fetch, refresh)
        return [
            WishlistItem(appid=i["appid"], priority=i.get("priority", 0), date_added=i.get("date_added", 0))
            for i in raw
        ]

    # -- games --------------------------------------------------------------

    def get_news(self, appid: int, count: int = 5, *, maxlength: int = 500, refresh: bool = False) -> list[NewsItem]:
        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{API_BASE}/ISteamNews/GetNewsForApp/v2/",
                {"appid": appid, "count": count, "maxlength": maxlength},
            )
            return data.get("appnews", {}).get("newsitems", [])

        raw = self._cached("news", f"{appid}:{count}:{maxlength}", fetch, refresh)
        return [
            NewsItem(
                gid=str(n.get("gid", "")),
                title=n.get("title", ""),
                url=n.get("url", ""),
                author=n.get("author", ""),
                contents=n.get("contents", ""),
                feedlabel=n.get("feedlabel", ""),
                date=n.get("date", 0),
            )
            for n in raw
        ]

    def get_app_details(self, appid: int, *, refresh: bool = False) -> AppDetails | None:
        """Store page data. None if the app is delisted or unavailable in your region."""

        def fetch() -> dict[str, Any] | None:
            data = self._get_json(
                f"{STORE_BASE}/api/appdetails",
                {"appids": appid, "cc": self.country, "l": "english"},
            )
            entry = (data or {}).get(str(appid), {})
            return entry.get("data") if entry.get("success") else None

        d = self._cached("app_details", f"{appid}:{self.country}", fetch, refresh)
        if not d:
            return None
        price = d.get("price_overview") or {}
        release = d.get("release_date") or {}
        return AppDetails(
            appid=appid,
            name=d.get("name", ""),
            type=d.get("type", ""),
            short_description=d.get("short_description", ""),
            genres=[g.get("description", "") for g in d.get("genres", [])],
            categories=[c.get("description", "") for c in d.get("categories", [])],
            is_free=bool(d.get("is_free")),
            price_final=price.get("final"),
            price_initial=price.get("initial"),
            discount_percent=price.get("discount_percent", 0),
            price_formatted=price.get("final_formatted"),
            release_date=release.get("date", ""),
            coming_soon=bool(release.get("coming_soon")),
            developers=d.get("developers", []),
            publishers=d.get("publishers", []),
            metacritic=(d.get("metacritic") or {}).get("score"),
        )

    def get_review_summary(self, appid: int, *, refresh: bool = False) -> ReviewSummary | None:
        """Overall review score and counts (all languages, Steam purchases, like the store page)."""

        def fetch() -> dict[str, Any] | None:
            try:
                return self._fetch_reviews_service(appid)
            except SteamAPIError as e:
                # The old store endpoint is deprecated but still a useful fallback.
                log.info("GetAppReviews failed for %s (%s); trying store appreviews", appid, e)
                return self._fetch_reviews_store(appid)

        raw = self._cached("app_reviews", str(appid), fetch, refresh)
        return ReviewSummary(appid=appid, **raw) if raw else None

    def _fetch_reviews_service(self, appid: int) -> dict[str, Any] | None:
        # Score fields only appear on the first page with review_type = All (0).
        params = {"appid": appid, "languages": ["all"], "review_type": 0, "purchase_type": 0, "num_per_page": 1}
        data = self._get_json(
            f"{API_BASE}/IUserReviewsService/GetAppReviews/v1/",
            {"input_json": json.dumps(params)},
        )
        return _parse_query_summary(data.get("response", data).get("query_summary"))

    def _fetch_reviews_store(self, appid: int) -> dict[str, Any] | None:
        data = self._get_json(
            f"{STORE_BASE}/appreviews/{appid}",
            {"json": 1, "language": "all", "purchase_type": "steam", "num_per_page": 0},
        )
        if not data.get("success"):
            return None
        return _parse_query_summary(data.get("query_summary"))

    def get_app_tags(self, appid: int, *, refresh: bool = False) -> TagInfo:
        """User tags from SteamSpy, falling back to store genres and categories."""

        def fetch() -> dict[str, int]:
            data = self._get_json(STEAMSPY_URL, {"request": "appdetails", "appid": appid})
            tags = data.get("tags") or {}
            # SteamSpy returns {} or [] when it has no tags for a game.
            if isinstance(tags, list):
                return {}
            return {str(k): int(v) for k, v in tags.items()}

        tags: dict[str, int] = {}
        if not self._steamspy_down:
            try:
                tags = normalize_tags(self._cached("app_tags", str(appid), fetch, refresh))
            except SteamAPIError as e:
                # Don't pay the retry cost on every game if SteamSpy is down.
                log.info("SteamSpy unavailable (%s); using store genres for the rest of this run", e)
                self._steamspy_down = True
        if tags:
            return TagInfo(appid, tags, "steamspy")

        details = self.get_app_details(appid)
        fallback = normalize_tags({name: 1 for name in (details.genres + details.categories)}) if details else {}
        return TagInfo(appid, fallback, "store")


    def get_tag_games(self, tag: str, *, refresh: bool = False) -> list[TagGame]:
        """Every game SteamSpy files under a user tag (e.g. "Roguelike Deckbuilder")."""

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(STEAMSPY_URL, {"request": "tag", "tag": tag})
            if not isinstance(data, dict):
                return []
            # Keep only what we use; the full response is ~150 KB per tag.
            return [
                {
                    "appid": int(g.get("appid") or k),
                    "name": g.get("name", ""),
                    "positive": int(g.get("positive") or 0),
                    "negative": int(g.get("negative") or 0),
                    "owners": str(g.get("owners", "")),
                    "price": _int_or_none(g.get("price")),
                    "discount": _int_or_none(g.get("discount")) or 0,
                }
                for k, g in data.items()
                if isinstance(g, dict)
            ]

        raw = self._cached("tag_games", tag_key(tag), fetch, refresh)
        return [TagGame(**g) for g in raw]

    def search_store(self, term: str, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Store search by name. Ranks by title match, so it finds games, not genres."""

        def fetch() -> list[dict[str, Any]]:
            data = self._get_json(
                f"{STORE_BASE}/api/storesearch/", {"term": term, "cc": self.country, "l": "english"}
            )
            items = (data or {}).get("items", [])
            return [
                {
                    "appid": i["id"],
                    "name": i.get("name", ""),
                    # price is often missing from search results
                    "price_final": (i.get("price") or {}).get("final"),
                }
                for i in items
                if i.get("type") == "app" and "id" in i
            ]

        return self._cached("store_search", f"{self.country}:{term.lower().strip()}", fetch, refresh)

    def get_prices(self, appids: list[int], *, refresh: bool = False) -> dict[int, Price | None]:
        """Current prices, several apps per request. None for free or unreleased games."""
        result: dict[int, Price | None] = {}
        missing: list[int] = []
        for appid in appids:
            key = f"{appid}:{self.country}"
            cached = None if (refresh or self.cache is None) else self.cache.get("price", key)
            if cached is None:
                missing.append(appid)
            else:
                result[appid] = Price(**cached["price"]) if cached["price"] else None

        for chunk in (missing[i : i + 50] for i in range(0, len(missing), 50)):
            data = self._get_json(
                f"{STORE_BASE}/api/appdetails",
                {"appids": ",".join(map(str, chunk)), "cc": self.country, "filters": "price_overview"},
            ) or {}
            for appid in chunk:
                entry = data.get(str(appid)) or {}
                po = (entry.get("data") or {}).get("price_overview") if entry.get("success") else None
                price = (
                    Price(
                        appid=appid,
                        final=po.get("final", 0),
                        initial=po.get("initial", 0),
                        discount_percent=po.get("discount_percent", 0),
                        formatted=po.get("final_formatted", ""),
                        currency=po.get("currency", ""),
                    )
                    if po
                    else None
                )
                result[appid] = price
                if self.cache is not None:
                    self.cache.set("price", f"{appid}:{self.country}", {"price": asdict(price) if price else None})
        return result


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def tag_key(tag: str) -> str:
    """Spelling-insensitive key: "Rogue-like", "Roguelike" and "roguelike " all match."""
    return re.sub(r"[^a-z0-9]", "", html.unescape(tag).lower())


def normalize_tags(tags: dict[str, int]) -> dict[str, int]:
    """Clean up tag names and merge variant spellings, highest votes first.

    SteamSpy and the store disagree on spelling ("Multiplayer" vs "Multi-player"),
    sometimes HTML-escape names ("Point &amp; Click"), and leave stray spaces.
    The variant with the most votes keeps its spelling.
    """
    merged: dict[str, tuple[str, int]] = {}
    for raw, votes in tags.items():
        name = " ".join(html.unescape(str(raw)).split())
        key = tag_key(name)
        if not key:
            continue
        if key not in merged or votes > merged[key][1]:
            merged[key] = (name, max(votes, merged.get(key, ("", 0))[1]))
    return dict(sorted(merged.values(), key=lambda kv: -kv[1]))


def _parse_query_summary(summary: dict[str, Any] | None) -> dict[str, Any] | None:
    if not summary or "total_reviews" not in summary:
        return None
    return {
        "score": int(summary.get("review_score", 0)),
        "description": summary.get("review_score_desc", ""),
        "positive": int(summary.get("total_positive", 0)),
        "negative": int(summary.get("total_negative", 0)),
        "total": int(summary.get("total_reviews", 0)),
    }


def library_warnings(games: list[OwnedGame]) -> list[str]:
    """Problems with library data that aren't errors but make results misleading."""
    warnings = []
    if games and all(g.playtime_forever == 0 for g in games):
        warnings.append(PLAYTIME_HIDDEN_FIX)
    return warnings
