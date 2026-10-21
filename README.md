# Steam Buddy

A personal Steam gaming agent. It reads your Steam library and play habits,
recommends games you'd actually enjoy (especially hidden gems), keeps you posted
on news for the games you care about, and answers questions like "what should I
play tonight?". It's a Python CLI that chats with Claude, and Claude uses tools
backed by your real Steam data.

> Status: **all six phases built**. See [docs/PLAN.md](docs/PLAN.md) for how it works.

## Requirements

- Python 3.11 or newer
- A Steam account with a Steam Web API key
- An Anthropic API key (for the chat agent)

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
`.env` as `ANTHROPIC_API_KEY=...`.

Two optional settings:

- `ANTHROPIC_MODEL` picks the Claude model (default `claude-opus-5-5`).
- `ANTHROPIC_EFFORT` sets how hard Claude thinks before answering: `low`,
  `medium` (default), `high`, `xhigh` or `max`. Lower is faster and cheaper;
  try `low` if answers feel slow.

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

## 7. Chat

```bash
python -m steam_buddy.main            # or: steam-buddy, after pip install -e .
python -m steam_buddy.main --refresh  # re-fetch your library, wishlist and prices first
```

Ask things like:

- what should I play tonight?
- what in my backlog fits a 2-hour session?
- what have I abandoned that's worth going back to?
- what's on sale from my wishlist?
- find me hidden gems like my favourite roguelikes
- what should I buy next for under $20?
- which game on my wishlist fits me best?
- what's new in the games I've been playing?

While Claude works you'll see a line for each lookup it makes ("Checking your
wishlist and prices"). Type `/reset` to start a new conversation, `/help` for
examples, and `/quit` (or Ctrl+D) to leave. Ctrl+C stops the current answer.

Recommendations are scored against your whole taste profile: each candidate's
tags are compared with the tags you've put the most hours into, games with
tags you tend to drop score lower, and Claude is told which of your games each
pick connects to. The first recommendation or hidden-gem question takes
longer (up to half a minute) while candidate tags are fetched; they're cached
for a week.

Claude only sees your data through tools, and it's told to back every
recommendation with your hours and tags, never to suggest a game you already
own, and to say plainly when data is missing (for example, games without
achievements). Steam doesn't record session length, so "fits a 2-hour session"
is judged from genre tags, and Claude will say so.

If Claude's safety checks decline a question by mistake, the API retries it on
another Claude model automatically, and the chat notes when that happens.

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

## 8. Weekly digest

```bash
python -m steam_buddy.main --digest                            # print it
python -m steam_buddy.main --digest --out "digests/{date}.md"  # save it as Markdown
python -m steam_buddy.main --digest --refresh                  # re-fetch your account data first
```

The digest has:

- **Top picks**: three games you don't own, scored against your taste, each
  with the games of yours it's most like.
- **Hidden gems**: two highly rated, lesser-known games (not repeats of the picks).
- **News** for the games you've played most in the last two weeks: the
  developer's own updates, or press coverage when the developer posted nothing
  (press mostly re-reports announcements). Sale ads, award campaigns, stream
  schedules, brand tie-ins and mobile-version news don't count as updates.
- **Worth going back to**: a game you put real time into and stopped, preferring
  one with a recent big update (new content, an expansion, a named patch) since
  you left. The fresher the update the more it counts; your hours count a
  little. Updates are counted since you last played, so skipping a week loses
  nothing. If nothing you stopped has updates, it suggests the game in your
  backlog that best fits your taste, when one fits well enough.

It's built straight from your Steam data without asking Claude, so it doesn't
need an Anthropic key and costs nothing to run. `{date}` in the `--out` name
becomes the date, so a weekly run keeps one file per week. The `digests/`
folder is gitignored.

### Run it every week on Windows

Open PowerShell and run this once, changing the first line to wherever your
Steam Buddy folder is:

```powershell
$dir = "C:\path\to\Steam Buddy"
$action = New-ScheduledTaskAction -Execute "$dir\.venv\Scripts\python.exe" `
    -Argument '-m steam_buddy.main --digest --out "digests\{date}.md"' -WorkingDirectory $dir
$trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Sunday -At 10am
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName "Steam Buddy digest" -Action $action -Trigger $trigger -Settings $settings
```

Every Sunday at 10am (or as soon as the PC is on, if it was off then, and on
battery too) a window opens for a few seconds and the digest lands in the
`digests` folder. The working directory matters: it's where Steam Buddy finds your `.env`. To try it
straight away, run `Start-ScheduledTask "Steam Buddy digest"`; to remove it,
`Unregister-ScheduledTask "Steam Buddy digest"`.

### On macOS or Linux

Add a line like this with `crontab -e`:

```
0 10 * * 0 cd /path/to/steam-buddy && .venv/bin/python -m steam_buddy.main --digest --out "digests/{date}.md"
```

## 9. The app (web UI)

```
python -m steam_buddy.main --web                # opens in a window of its own
python -m steam_buddy.main --web --refresh      # re-fetch your account data first
python -m steam_buddy.main --web --port 8800 --no-open
```

This runs a small server on your computer only (`http://127.0.0.1:8765/`) and
opens it in an Edge app window, or your default browser if Edge isn't found.
Ctrl+C in the terminal stops it. Four screens:

- **Home**: the games you're playing as story circles (a green ring means news
  from the developer in the last two weeks), the game most worth going back to,
  your top picks and hidden gems as cover-art tiles, recent activity and your
  wishlist.
- **Feed**: developer updates for what you're playing, and what changed in the
  game worth going back to, as posts with the game's art.
- **Picks**: one card per recommendation, with its match score, reviews, price
  and the games of yours it's like.
- **Chat**: the same chat as the terminal. Only this screen calls Claude.

The home screen is built from the same data as the digest, so it's free and
reused for 15 minutes; the refresh button in Activity rebuilds it. Game art
loads in the browser straight from Steam's image servers.

The server only answers requests addressed to `127.0.0.1` or `localhost`, and
every API call needs a token that's made fresh at each launch and only appears
in the page it serves, so other websites can't use it to run up your Anthropic
bill. Your keys never leave the server.
