# Steam Buddy

A personal Steam gaming agent. It reads your Steam library and play habits,
recommends games you'd actually enjoy (especially hidden gems), keeps you posted
on news for the games you care about, and answers questions like "what should I
play tonight?". It's a Python CLI that chats with Claude, and Claude uses tools
backed by your real Steam data.

> Status: **Phase 3 (taste profile)**. See [docs/PLAN.md](docs/PLAN.md) for the roadmap.

## Requirements

- Python 3.11 or newer
- A Steam account with a Steam Web API key
- An Anthropic API key (needed from Phase 4, the chat agent)

## Install

```bash
git clone https://github.com/charlesmmartin2005-netizen/Steam-Buddy.git steam-buddy
cd steam-buddy
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # Windows: copy .env.example .env
```

Then fill in `.env` (next sections) and run:

```bash
python -m steam_buddy.main --check
```

You should see "Config looks good." The check never prints your keys.

## 1. Get a Steam Web API key

1. Sign in at <https://steamcommunity.com/dev/apikey>.
2. Enter any domain name (e.g. `localhost`); it isn't used for this app.
3. Agree to the terms and click **Register**.
4. Copy the 32-character key into `.env` as `STEAM_API_KEY=...`.

Your account needs to be a non-limited account (you've spent at least $5 on
Steam) to register a key. Treat the key like a password: it lives only in
`.env`, which is gitignored. If it ever leaks, revoke it on the same page.

## 2. Find your SteamID64

Your SteamID64 is a 17-digit number starting with `7656119`.

- **If your profile URL looks like** `https://steamcommunity.com/profiles/76561198XXXXXXXXX/`,
  the number at the end is your SteamID64.
- **If you've set a custom URL** like `https://steamcommunity.com/id/yourname/`:
  - In the Steam client or website, open your profile, then **Edit Profile**.
    Clearing the "Custom URL" field temporarily shows the numeric form, or
  - Call the API directly (replace both placeholders):
    `https://api.steampowered.com/ISteamUser/ResolveVanityURL/v1/?key=YOUR_KEY&vanityurl=yourname`
    The `steamid` field in the response is your SteamID64.
  - In the Steam client, **Account details** also shows your Steam ID under your account name.

Put it in `.env` as `STEAM_ID=...`.

## 3. Make your game details public

Steam Buddy can only see your library and playtime if your **Game details** are
public. If they're private, the API returns an empty library with no error, so
Steam Buddy will detect this and remind you.

1. Open your Steam profile and click **Edit Profile**, then **Privacy Settings**
   (or go to <https://steamcommunity.com/my/edit/settings>).
2. Set **My profile** to **Public**.
3. Set **Game details** to **Public**.
4. Make sure **"Always keep my total playtime private even if users can see my
   game details"** is **unchecked**, otherwise playtime comes back as zero.

For the wishlist, your wishlist visibility follows the same Game details setting.
Changes can take a few minutes to reach the API.

## 4. Anthropic API key (for the chat agent)

Create a key at <https://console.anthropic.com/settings/keys> and put it in
`.env` as `ANTHROPIC_API_KEY=...`. You can set `ANTHROPIC_MODEL` to pick a
different Claude model (default `claude-sonnet-5-5`).

## 5. Check that Steam Buddy can see your library

```bash
python scripts/print_library.py              # top 50 games by playtime
python scripts/print_library.py --limit 0    # every game
python scripts/print_library.py --refresh    # skip the cache and re-fetch
```

If your game details are private, it tells you exactly which setting to change
instead of printing an empty table.

Responses are cached locally so repeat runs are fast and polite to Steam:
library data for a day, store data for a week, news for an hour. Use
`--refresh` after changing privacy settings or buying a game.

## 6. See your taste profile

```bash
python scripts/print_profile.py            # readable summary
python scripts/print_profile.py --json     # the same data as JSON
python scripts/print_profile.py --refresh  # re-fetch everything
```

It shows your top tags weighted by playtime (with the games behind each one),
whether you lean toward short runs or long sessions, what's trending up in your
recent play, games you're still playing, likely finished, likely abandoned,
bounced off (under an hour), and never played.

The first run fetches tags and achievements for every game you've played, so it
takes a minute or two (SteamSpy allows one request per second). After that it
comes from the cache.

## Running tests

```bash
pytest
```

## Project layout

See [docs/PLAN.md](docs/PLAN.md#layout).

## Privacy

Everything runs locally. Steam data is cached in a SQLite file on your machine
(`~/.cache/steam-buddy/cache.sqlite3` by default). Your keys are never logged,
printed or committed.
