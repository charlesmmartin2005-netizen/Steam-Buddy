"""The tools Claude can call, backed by your Steam data.

Each tool is a method on SteamTools plus an entry in TOOL_DEFINITIONS. Results
are compact JSON so they fit comfortably in the conversation. Games you own are
filtered out of every discovery tool (search results flag them instead), so
recommendations can't accidentally suggest something already in your library.
"""

from __future__ import annotations

import html
import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Callable

from .profile import TasteProfile, _game_tags, _is_software, build_profile, gather, session_fit
from .steam_client import OwnedGame, SteamAPIError, SteamClient, TagGame, tag_key

log = logging.getLogger(__name__)

MAX_TAGS_PER_QUERY = 3  # each tag is one SteamSpy request (1/second, cached a week)


def _int(value: Any, name: str, *, lo: int, hi: int, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value:
        raise ToolInputError(f"'{name}' must be a whole number")
    if not lo <= int(value) <= hi:
        raise ToolInputError(f"'{name}' must be between {lo} and {hi}")
    return int(value)


def _num(value: Any, name: str, *, lo: float, hi: float, default: float) -> float:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ToolInputError(f"'{name}' must be a number")
    if not lo <= value <= hi:
        raise ToolInputError(f"'{name}' must be between {lo} and {hi}")
    return float(value)


def _tags(value: Any, *, required: bool) -> list[str]:
    if value is None and not required:
        return []
    if not isinstance(value, list) or not all(isinstance(t, str) and t.strip() for t in value):
        raise ToolInputError("'tags' must be a list of tag names")
    if required and not value:
        raise ToolInputError("give at least one tag")
    return [t.strip() for t in value][:MAX_TAGS_PER_QUERY]


class ToolInputError(ValueError):
    """The model sent arguments the tool can't use; reported back as a tool error."""


def _days_ago(ts: int | None) -> int | None:
    if not ts:
        return None
    return max(0, int((datetime.now(timezone.utc).timestamp() - ts) // 86400))


def _clean_text(text: str, limit: int = 400) -> str:
    """Strip HTML, entities and Steam's BBCode from store and news text."""
    text = re.sub(r"<[^>]+>|\[/?[a-zA-Z0-9*]+(=[^\]]*)?\]", " ", html.unescape(text or ""))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _wilson_lower(pos: int, total: int, z: float = 1.96) -> float:
    """Lower bound of the positive-review rate: rewards both high scores and enough reviews."""
    if total == 0:
        return 0.0
    p = pos / total
    centre = p + z * z / (2 * total)
    spread = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total))
    return (centre - spread) / (1 + z * z / total)


class SteamTools:
    def __init__(self, client: SteamClient):
        self.client = client
        self._profile: TasteProfile | None = None
        self._tag_spellings: dict[str, str] | None = None

    # -- shared state -------------------------------------------------------

    def _owned(self) -> dict[int, OwnedGame]:
        return {g.appid: g for g in self.client.get_owned_games()}

    def profile(self) -> TasteProfile:
        if self._profile is None:
            data = gather(self.client)
            self._profile = build_profile(data)
            self._tag_spellings = {}
            for info in data.tags.values():
                for tag in info.tags:
                    self._tag_spellings.setdefault(tag_key(tag), tag)
        return self._profile

    def _steam_tag(self, tag: str) -> str:
        """Map "roguelike" to the spelling SteamSpy uses for it (e.g. "Rogue-like")."""
        if self._tag_spellings is None:
            self.profile()
        return (self._tag_spellings or {}).get(tag_key(tag), tag)

    def _tag_pool(self, tags: list[str]) -> tuple[dict[int, TagGame], dict[int, list[str]], list[str]]:
        """Union of games under each tag, which tags each one matched, and tags with no games."""
        pool: dict[int, TagGame] = {}
        matched: dict[int, list[str]] = {}
        empty: list[str] = []
        for tag in tags:
            spelled = self._steam_tag(tag)
            games = self.client.get_tag_games(spelled)
            if not games:
                empty.append(tag)
            for g in games:
                pool[g.appid] = g
                matched.setdefault(g.appid, []).append(spelled)
        return pool, matched, empty

    # -- tools ----------------------------------------------------------------

    def get_library_summary(self) -> dict[str, Any]:
        games = sorted(self._owned().values(), key=lambda g: -g.playtime_forever)
        return {
            "total_games": len(games),
            "played": sum(1 for g in games if g.playtime_forever > 0),
            "never_played": sum(1 for g in games if g.playtime_forever == 0),
            "total_hours": round(sum(g.hours for g in games)),
            "most_played": [
                {"appid": g.appid, "name": g.name, "hours": round(g.hours, 1),
                 "last_played_days_ago": _days_ago(g.last_played)}
                for g in games[:20]
            ],
        }

    def get_taste_profile(self) -> dict[str, Any]:
        # Trim long lists so the profile stays a few thousand tokens.
        return {k: v[:15] if isinstance(v, list) else v for k, v in self.profile().to_dict().items()}

    def get_recently_played(self) -> dict[str, Any]:
        recent = self.client.get_recently_played()
        return {
            "games": [
                {"appid": g.appid, "name": g.name, "hours_last_2_weeks": round(g.playtime_2weeks / 60, 1),
                 "hours_total": round(g.playtime_forever / 60, 1)}
                for g in recent
            ]
        }

    def get_game_details(self, appid: Any) -> dict[str, Any]:
        appid = _int(appid, "appid", lo=1, hi=10**8)
        details = self.client.get_app_details(appid)
        if details is None:
            return {"appid": appid, "found": False, "note": "Not on the store in your region, or delisted."}
        reviews = self.client.get_review_summary(appid)
        tags = self.client.get_app_tags(appid)
        owned = self._owned().get(appid)
        out: dict[str, Any] = {
            "appid": appid,
            "name": details.name,
            "type": details.type,
            "description": _clean_text(details.short_description, limit=600),
            "tags": list(tags.tags)[:12],
            "session_style": session_fit(tags),
            "genres": details.genres,
            "release_date": details.release_date,
            "coming_soon": details.coming_soon,
            "developers": details.developers,
            "price": "Free" if details.is_free else details.price_formatted,
            "discount_percent": details.discount_percent,
            "metacritic": details.metacritic,
            "reviews": reviews.to_dict() if reviews else None,
            "owned": owned is not None,
        }
        if owned:
            ach = None
            try:
                ach = self.client.get_achievements(appid)
            except SteamAPIError:
                pass
            out["your_hours"] = round(owned.hours, 1)
            out["your_last_played_days_ago"] = _days_ago(owned.last_played)
            out["your_achievement_pct"] = round(ach.percent, 1) if ach else None
        return out

    def search_store(self, query: Any, max_results: Any = None) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ToolInputError("'query' must be a non-empty string")
        n = _int(max_results, "max_results", lo=1, hi=25, default=10)
        owned = self._owned()
        return {
            "results": [
                {**r, "owned": r["appid"] in owned}
                for r in self.client.search_store(query)[:n]
            ],
            "note": "Matches by title. To find games by genre or feel, use find_similar_games.",
        }

    def find_similar_games(self, appid: Any = None, tags: Any = None, max_results: Any = None) -> dict[str, Any]:
        n = _int(max_results, "max_results", lo=1, hi=25, default=10)
        tag_list = _tags(tags, required=False)
        source = None
        if appid is not None:
            source = _int(appid, "appid", lo=1, hi=10**8)
            info = self.client.get_app_tags(source)
            tag_list = (tag_list or list(_game_tags(info)))[:MAX_TAGS_PER_QUERY]
        if not tag_list:
            raise ToolInputError("give an appid or at least one tag")

        pool, matched, empty = self._tag_pool(tag_list)
        owned = self._owned()
        weights = {self._steam_tag(t): 1.0 - 0.2 * i for i, t in enumerate(tag_list)}
        ranked = sorted(
            (g for g in pool.values() if g.appid not in owned and g.appid != source and g.reviews >= 100),
            key=lambda g: (
                -sum(weights.get(t, 0.5) for t in matched[g.appid]),
                -_wilson_lower(g.positive, g.reviews),
            ),
        )
        return {
            "based_on_tags": [self._steam_tag(t) for t in tag_list],
            "games": [self._tag_game_out(g, matched[g.appid]) for g in ranked[:n]],
            "excluded_owned": sum(1 for a in pool if a in owned),
            **({"tags_with_no_games": empty} if empty else {}),
        }

    def find_hidden_gems(
        self,
        tags: Any,
        min_positive_pct: Any = None,
        max_review_count: Any = None,
        min_review_count: Any = None,
        max_results: Any = None,
    ) -> dict[str, Any]:
        tag_list = _tags(tags, required=True)
        min_pct = _num(min_positive_pct, "min_positive_pct", lo=50, hi=100, default=90)
        max_reviews = _int(max_review_count, "max_review_count", lo=10, hi=1_000_000, default=5_000)
        min_reviews = _int(min_review_count, "min_review_count", lo=1, hi=max_reviews, default=50)
        n = _int(max_results, "max_results", lo=1, hi=25, default=10)

        pool, matched, empty = self._tag_pool(tag_list)
        owned = self._owned()
        gems = [
            g for g in pool.values()
            if g.appid not in owned
            and min_reviews <= g.reviews <= max_reviews
            and g.positive_pct >= min_pct
        ]
        gems.sort(key=lambda g: (-len(matched[g.appid]), -_wilson_lower(g.positive, g.reviews)))
        return {
            "criteria": {"tags": [self._steam_tag(t) for t in tag_list], "min_positive_pct": min_pct,
                         "review_count_between": [min_reviews, max_reviews]},
            "games": [self._tag_game_out(g, matched[g.appid]) for g in gems[:n]],
            "candidates_checked": len(pool),
            "note": "Review counts come from SteamSpy and can lag Steam by a few days; "
                    "call get_game_details to confirm before recommending.",
            **({"tags_with_no_games": empty} if empty else {}),
        }

    @staticmethod
    def _tag_game_out(g: TagGame, matched: list[str]) -> dict[str, Any]:
        return {
            "appid": g.appid,
            "name": g.name,
            "matched_tags": matched,
            "positive_pct": round(g.positive_pct, 1),
            "reviews": g.reviews,
            "owners": g.owners,
            "price_usd": None if g.price is None else round(g.price / 100, 2),
            "discount_percent": g.discount,
        }

    def get_game_news(self, appid: Any, count: Any = None) -> dict[str, Any]:
        appid = _int(appid, "appid", lo=1, hi=10**8)
        n = _int(count, "count", lo=1, hi=10, default=3)
        items = self.client.get_news(appid, count=n, maxlength=600)
        return {
            "appid": appid,
            "news": [
                {
                    "title": i.title,
                    "date": datetime.fromtimestamp(i.date, timezone.utc).strftime("%Y-%m-%d"),
                    "source": i.feedlabel,
                    "url": i.url,
                    "summary": _clean_text(i.contents),
                }
                for i in items
            ],
        }

    def get_wishlist(self, max_items: Any = None) -> dict[str, Any]:
        n = _int(max_items, "max_items", lo=1, hi=50, default=25)
        items = sorted(self.client.get_wishlist(), key=lambda i: (i.priority or 10**6, -i.date_added))
        shown = items[:n]
        prices = self.client.get_prices([i.appid for i in shown]) if shown else {}
        out = []
        for i in shown:
            d = self.client.get_app_details(i.appid)
            p = prices.get(i.appid)
            out.append({
                "appid": i.appid,
                "name": d.name if d else f"App {i.appid}",
                "price": p.formatted if p else ("Free" if d and d.is_free else None),
                "discount_percent": p.discount_percent if p else 0,
                "on_sale": bool(p and p.discount_percent > 0),
                "release_date": d.release_date if d else None,
                "coming_soon": d.coming_soon if d else None,
            })
        result: dict[str, Any] = {"total_on_wishlist": len(items), "items": out}
        if not items:
            result["note"] = "The wishlist came back empty. It may really be empty, or hidden by privacy settings."
        elif len(items) > n:
            result["note"] = f"Showing the first {n} by your wishlist order."
        return result

    def get_backlog(self, max_hours: Any = None, max_results: Any = None) -> dict[str, Any]:
        limit_h = _num(max_hours, "max_hours", lo=0, hi=50, default=2)
        n = _int(max_results, "max_results", lo=1, hi=50, default=25)
        games = [g for g in self._owned().values() if g.hours <= limit_h]
        games.sort(key=lambda g: (g.playtime_forever > 0, g.playtime_forever))
        out = []
        for g in games:
            if len(out) >= n:
                break
            try:
                info = self.client.get_app_tags(g.appid)
            except SteamAPIError:
                info = None
            if _is_software(info):
                continue
            out.append({
                "appid": g.appid,
                "name": g.name,
                "hours": round(g.hours, 1),
                "status": "never played" if g.playtime_forever == 0 else "tried briefly",
                "last_played_days_ago": _days_ago(g.last_played),
                "tags": list(_game_tags(info))[:6],
                "session_style": session_fit(info),
            })
        return {
            "max_hours_played": limit_h,
            "games": out,
            "note": "session_style is inferred from genre tags; Steam has no data on how long a sitting takes.",
        }

    # -- dispatch -------------------------------------------------------------

    def run(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run a tool. Returns (JSON text, is_error)."""
        handler: Callable[..., dict[str, Any]] | None = getattr(self, name, None) if name in TOOL_NAMES else None
        if handler is None:
            return json.dumps({"error": f"unknown tool {name!r}"}), True
        if not isinstance(args, dict):
            return json.dumps({"error": "arguments must be an object"}), True
        allowed = set(TOOL_SCHEMAS[name]["properties"])
        unexpected = set(args) - allowed
        if unexpected:
            return json.dumps({"error": f"unexpected arguments: {sorted(unexpected)}"}), True
        missing = set(TOOL_SCHEMAS[name]["required"]) - set(args)
        if missing:
            return json.dumps({"error": f"missing required arguments: {sorted(missing)}"}), True
        try:
            return json.dumps(handler(**args), ensure_ascii=False), False
        except ToolInputError as e:
            return json.dumps({"error": str(e)}), True
        except SteamAPIError as e:
            return json.dumps({"error": f"Steam request failed: {e}"}), True
        except Exception as e:  # a bug in a tool shouldn't end the chat
            log.exception("tool %s failed", name)
            return json.dumps({"error": f"internal error in {name}: {type(e).__name__}"}), True


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": required or [], "additionalProperties": False}


_APPID = {"type": "integer", "description": "Steam app id, e.g. 413150 for Stardew Valley."}
_TAGS = {
    "type": "array",
    "items": {"type": "string"},
    "maxItems": MAX_TAGS_PER_QUERY,
    "description": "Steam user tags, e.g. [\"Roguelike Deckbuilder\", \"Pixel Graphics\"]. "
                   "Prefer tag names from get_taste_profile. Up to 3; each extra tag narrows the theme.",
}

TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "get_library_summary": _schema({}),
    "get_taste_profile": _schema({}),
    "get_recently_played": _schema({}),
    "get_game_details": _schema({"appid": _APPID}, ["appid"]),
    "search_store": _schema({
        "query": {"type": "string", "description": "Game title or words from it."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
    }, ["query"]),
    "find_similar_games": _schema({
        "appid": {**_APPID, "description": "Find games like this one (uses its top tags)."},
        "tags": _TAGS,
        "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
    }),
    "find_hidden_gems": _schema({
        "tags": _TAGS,
        "min_positive_pct": {"type": "number", "minimum": 50, "maximum": 100, "description": "Default 90."},
        "max_review_count": {"type": "integer", "description": "Upper bound on total reviews. Default 5000."},
        "min_review_count": {"type": "integer", "description": "Lower bound so a handful of reviews can't fake a score. Default 50."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 25},
    }, ["tags"]),
    "get_game_news": _schema({
        "appid": _APPID,
        "count": {"type": "integer", "minimum": 1, "maximum": 10, "description": "Default 3."},
    }, ["appid"]),
    "get_wishlist": _schema({"max_items": {"type": "integer", "minimum": 1, "maximum": 50}}),
    "get_backlog": _schema({
        "max_hours": {"type": "number", "description": "Include games played at most this long. Default 2."},
        "max_results": {"type": "integer", "minimum": 1, "maximum": 50},
    }),
}

TOOL_DESCRIPTIONS = {
    "get_library_summary": "Totals for the user's Steam library and their 20 most-played games with hours "
                           "and days since last played.",
    "get_taste_profile": "The user's taste profile: top tags weighted by playtime (with the games behind "
                         "each), session-style lean, recent trends, games still playing, likely finished, "
                         "likely abandoned, live-service games they stopped, bounced off (<1h), never "
                         "played, plus data caveats. Call this before recommending anything.",
    "get_recently_played": "Games played in the last two weeks with hours.",
    "get_game_details": "Store details for one game: description, tags, genres, price, release date, "
                        "review score and count, and whether the user owns it (with their hours and "
                        "achievement % if so).",
    "search_store": "Search the Steam store by title. Each result says whether the user owns it. Use it "
                    "to look up a game's appid by name, not to discover games by genre.",
    "find_similar_games": "Well-reviewed games the user does NOT own that share tags with a given game "
                          "(appid) or a list of tags. Owned games are already removed.",
    "find_hidden_gems": "Highly rated games with relatively few reviews in the given tags that the user "
                        "does NOT own. Owned games are already removed.",
    "get_game_news": "Latest news posts and patch notes for a game.",
    "get_wishlist": "The user's wishlist with current prices, discounts, and release dates.",
    "get_backlog": "Games the user owns but has never played or barely played, with their tags and a "
                   "session style (short runs vs long sessions) inferred from those tags.",
}

TOOL_NAMES = set(TOOL_SCHEMAS)

TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {"name": name, "description": TOOL_DESCRIPTIONS[name], "input_schema": TOOL_SCHEMAS[name]}
    for name in TOOL_SCHEMAS  # fixed order keeps the prompt cache stable
]
