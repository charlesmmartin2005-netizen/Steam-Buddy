"""Local SQLite cache for Steam responses.

Each entry has a `kind` that decides how long it stays fresh:
library data refreshes daily, store data weekly, news hourly.
Values are stored as JSON. Cache keys never contain the API key.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

HOUR = 60 * 60
DAY = 24 * HOUR

TTL_SECONDS: dict[str, int] = {
    # library: your own account data
    "owned_games": DAY,
    "recent_games": DAY,
    "achievements": DAY,
    "wishlist": DAY,
    "player_summary": DAY,
    # store: data about games
    "app_details": 7 * DAY,
    "app_reviews": 7 * DAY,
    "app_tags": 7 * DAY,
    "game_schema": 7 * DAY,
    # news
    "news": HOUR,
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    fetched_at REAL NOT NULL,
    PRIMARY KEY (kind, key)
)
"""


class Cache:
    def __init__(self, path: str | Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path))
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._clock = clock

    def close(self) -> None:
        self._db.close()

    def __enter__(self) -> Cache:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _ttl(self, kind: str) -> int:
        try:
            return TTL_SECONDS[kind]
        except KeyError:
            raise ValueError(f"unknown cache kind {kind!r}") from None

    def _row(self, kind: str, key: str) -> tuple[Any, float] | None:
        row = self._db.execute(
            "SELECT value, fetched_at FROM cache WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
        if row is None:
            return None
        return json.loads(row[0]), row[1]

    def get(self, kind: str, key: str, *, allow_stale: bool = False) -> Any | None:
        """Return the cached value, or None if missing (or expired, unless allow_stale)."""
        row = self._row(kind, key)
        if row is None:
            return None
        value, fetched_at = row
        if not allow_stale and self._clock() - fetched_at > self._ttl(kind):
            return None
        return value

    def set(self, kind: str, key: str, value: Any) -> None:
        self._ttl(kind)  # validate kind
        self._db.execute(
            "INSERT OR REPLACE INTO cache (kind, key, value, fetched_at) VALUES (?, ?, ?, ?)",
            (kind, key, json.dumps(value), self._clock()),
        )
        self._db.commit()

    def get_or_fetch(
        self,
        kind: str,
        key: str,
        fetch: Callable[[], Any],
        *,
        refresh: bool = False,
    ) -> Any:
        """Return a fresh cached value, or call `fetch` and store its result.

        If `fetch` raises and an expired copy exists, the expired copy is returned
        so a flaky network doesn't break a run. Errors still propagate when there
        is nothing cached at all.
        """
        # Read the row directly so a cached None (e.g. a delisted app) counts as a hit.
        row = self._row(kind, key)
        if row is not None and not refresh and self._clock() - row[1] <= self._ttl(kind):
            return row[0]
        try:
            value = fetch()
        except Exception:
            if row is not None:
                return row[0]
            raise
        self.set(kind, key, value)
        return value

    def clear(self, kind: str | None = None) -> None:
        if kind is None:
            self._db.execute("DELETE FROM cache")
        else:
            self._db.execute("DELETE FROM cache WHERE kind = ?", (kind,))
        self._db.commit()
