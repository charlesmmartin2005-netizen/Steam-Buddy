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
│   ├── recommend.py        # Phase 5: scoring candidates against your taste
│   ├── agent.py            # Phase 4: tool-use loop + system prompt
│   └── main.py             # CLI entry: `steam-buddy`, `--check`, `--digest`
├── scripts/
│   ├── print_library.py    # Phase 2: library sorted by playtime
│   └── print_profile.py    # Phase 3: taste profile
└── tests/
    ├── fixtures/           # fake library for profile tests
    └── test_*.py           # config, cache, client, profile, tools, agent
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
3. **Taste profile** (done): `profile.gather()` fetches, `build_profile()` is pure.
   Heuristics, all constants at the top of `profile.py`:
   - Tags: each game's top 10 SteamSpy tags, scaled so its strongest tag = 1, times
     sqrt(hours) so one huge game can't drown out the rest. Sales-y tags (Indie,
     Singleplayer, Great Soundtrack...) are ignored; titles whose top tags say
     software (Wallpaper Engine) are left out.
   - Bounced off: played under 60 minutes, unless all of it was in the last 2 weeks.
   - Still playing: played in the last 30 days. Likely finished: 70%+ achievements.
     Likely abandoned: under 30% achievements and untouched for 90+ days. Games
     without achievements get no verdict.
   - Trending: tags whose share of the last 60 days' play beats their all-time share.
   - Session style: Steam doesn't report sessions, so it's inferred from how much
     play is in short-run genres (roguelikes, card games) vs long-session ones
     (open-world RPGs, survival, sims).
4. **Tools + agent + CLI chat** (done):
   - `tools.py`: ten tools (library summary, taste profile, recently played, game
     details, store search, similar games, hidden gems, news, wishlist, backlog).
     Every argument is validated in code, and failures go back to Claude as tool
     errors instead of crashing the chat. Discovery tools drop owned games before
     Claude sees them; search results carry an `owned` flag.
   - Similar games and hidden gems come from SteamSpy's per-tag game lists (cached
     a week), ranked by how many of the requested tags match and then by the lower
     bound of the positive-review rate, so 95% of 10,000 reviews beats 100% of 12.
     Tags are looked up under the spelling your own library uses.
   - `agent.py`: streaming tool-use loop on `claude-opus-5-5` at `medium` effort.
     The history is append-only (Claude's thinking stays valid across turns and the
     prompt cache keeps hitting); a turn that fails, is declined or is interrupted is
     removed whole. Server-side fallback (`fallbacks: "default"`) retries a mistaken
     safety decline on another model. At most 15 tool rounds per question.
   - `main.py`: the chat REPL with `/reset`, `/help`, `/quit` and `--refresh`.
5. **Hidden gems + recommendation quality** (done), in `recommend.py`:
   - Candidates come from SteamSpy's per-tag lists for your top four *specific*
     tags. Umbrella tags (Action, RPG, Multiplayer...) are skipped as seeds because
     their lists hold tens of thousands of games, but they still count in scoring.
     Tag lists drop games with under 50 reviews before caching.
   - The pool is pre-ranked cheaply (how much the tags a game was found under weigh
     in your profile, then review confidence). The best 15 get their own tags
     fetched and are scored by cosine similarity between their tags and your taste
     vector (your top 30 tags, as shares of playtime-weighted play), times review
     confidence, minus 0.1 for each tag you tend to drop among their top five.
     Umbrella tags count half, and games with fewer than five tags get a
     proportionally lower match (one "Action" tag isn't evidence of a fit).
   - Every result explains itself: `because_you_played` names your most similar
     played games (specific tags only; at least 5 hours or playing now; never one
     you likely abandoned) and the tags they share. A taste-based result with no
     such game is left out and listed as `left_out_no_link_to_your_games`. Also also `fits_your_tags`, `tags_you_tend_to_drop`, `early_access`,
     `session_style`. Prices come live from the store in one batched request,
     since SteamSpy's can be days old; review counts are still SteamSpy's.
   - New `recommend_games` tool (optional price cap); `find_hidden_gems` searches
     your own top tags when no tags are given; `find_similar_games` scores against
     all of the source game's tags; wishlist items get tags, reviews and a match.
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
| Games by tag | `steamspy.com/api.php?request=tag&tag=X` | no | every game with that tag, with review counts and price: 464 games (150 KB) for Roguelike Deckbuilder, 39,000 (13 MB) for Action |
| Store search | `store.steampowered.com/api/storesearch/` | no | matches titles only; price often missing |
| Prices | `store.steampowered.com/api/appdetails?filters=price_overview` | no | the one filter that accepts many app ids per request; cached 6 hours |

The API key travels as a query parameter, so the client never puts request URLs
in error messages, drops exception chains that could carry them, and turns off
httpx's own request logging.

## Defaults chosen (change any of these)

- HTTP: `httpx` (sync) instead of `requests`.
- Claude model: `claude-opus-5-5` at `medium` effort, overridable with
  `ANTHROPIC_MODEL` and `ANTHROPIC_EFFORT` in `.env`.
- Prices are cached for 6 hours rather than a week like other store data, so
  sale answers aren't stale.
- Cache location: `~/.cache/steam-buddy/cache.sqlite3` (override with `STEAM_BUDDY_CACHE`).
