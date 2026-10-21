# Steam Buddy: build plan

A personal Steam gaming agent: a Python CLI that chats with Claude, and Claude
calls tools backed by your real Steam data.

## Layout

```
steam-buddy/
├── .env.example            # template for secrets (copy to .env)
├── .gitignore              # keeps .env, cache DB, venvs out of git
├── pyproject.toml          # package metadata, Python >= 3.11, pytest config
├── requirements.txt        # runtime + dev deps (light)
├── README.md               # setup: Steam API key, SteamID64, privacy settings
├── docs/PLAN.md            # this file
├── steam_buddy/
│   ├── __init__.py
│   ├── config.py           # loads + validates .env; redacts the key in repr/logs
│   ├── steam_client.py     # Phase 2: endpoint wrappers, rate limiting, retries
│   ├── cache.py            # Phase 2: SQLite cache with per-kind TTLs
│   ├── profile.py          # Phase 3: taste profile
│   ├── tools.py            # Phase 4: Claude tool definitions + handlers
│   ├── agent.py            # Phase 4: tool-use loop + system prompt
│   └── main.py             # CLI entry: `steam-buddy`, `--check`, `--digest`
├── scripts/
│   └── print_library.py    # Phase 2: library sorted by playtime
└── tests/
    ├── fixtures/           # fake library for profile tests
    └── test_config.py
```

The modules live in a `steam_buddy/` package rather than at the top level, mainly
because a top-level `profile.py` would shadow Python's built-in `profile` module.

## Phases

1. **Setup** (done): structure, `.env.example`, `requirements.txt`, README,
   config loader with a `--check` command that validates `.env` without printing
   the key.
2. **Steam client + cache** (done): wrappers for every endpoint below, a per-host
   rate limiter, retries with backoff on 429/5xx/network errors (honouring
   Retry-After), SQLite cache with TTLs (library 24h, store 7d, news 1h) that falls
   back to stale data when Steam is down. Detects a private library and hidden
   playtime and prints the exact setting to change. `scripts/print_library.py`
   confirms it works.
3. **Taste profile**: playtime-weighted tags/genres, bounced-off games (< 1h),
   finished vs abandoned (achievement %), recent trend, inferred session length.
   Readable printout + tests on a fake library fixture.
4. **Tools + agent + CLI chat**: tool schemas, Claude tool-use loop, system prompt
   that grounds every recommendation in data and never suggests owned games.
5. **Hidden gems + recommendation quality**: high positive %, low review count,
   tag match scoring; filter owned games.
6. **News + weekly digest**: `--digest` prints 3 recs, 2 gems, news for recent
   top games, one backlog game to revisit.

## Steam endpoints (checked against the docs, October 2026)

| Data | Endpoint | Key | Notes |
|---|---|---|---|
| Library | `api.steampowered.com/IPlayerService/GetOwnedGames/v1` | yes | private library returns `{"response": {}}` |
| Recent | `IPlayerService/GetRecentlyPlayedGames/v1` | yes | `count=0` returns all |
| Profile | `ISteamUser/GetPlayerSummaries/v2` | yes | `communityvisibilitystate` 3 = public |
| Achievements | `ISteamUserStats/GetPlayerAchievements/v1` | yes | 400 = no stats, 403 = private |
| Achievement schema | `ISteamUserStats/GetSchemaForGame/v2` | yes | |
| News | `ISteamNews/GetNewsForApp/v2` | no | |
| Wishlist | `IWishlistService/GetWishlist/v1` | yes | returns app ids, priority, date added only |
| Store details | `store.steampowered.com/api/appdetails` | no | about 200 requests per 5 min; we space them 1.5s apart |
| Reviews | `IUserReviewsService/GetAppReviews/v1` | no | replaces the deprecated `store.steampowered.com/appreviews`, which we keep as a fallback |
| Tags | `steamspy.com/api.php?request=appdetails` | no | 1 request/second; falls back to store genres + categories |

The API key travels as a query parameter, so the client never puts request URLs
in error messages, drops exception chains that could carry them, and turns off
httpx's own request logging.

## Defaults chosen (change any of these)

- HTTP: `httpx` (sync) instead of `requests`, since `anthropic` already depends on it.
- Claude model: `claude-sonnet-5-5` by default, overridable via `ANTHROPIC_MODEL`
  in `.env`. Good quality for chat at a lower cost than Opus.
- Cache location: `~/.cache/steam-buddy/cache.sqlite3` (override with `STEAM_BUDDY_CACHE`).
