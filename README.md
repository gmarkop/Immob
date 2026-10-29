# immob

A personal tracker for apartments for sale in Carinthia (Kärnten), Austria.
It reads listings from willhaben.at, stores them in a local SQLite database,
filters them with your criteria and sends new matches and price drops to
Telegram.

> **Status: Phase 1.** The willhaben parser has been checked against real saved
> search and listing pages. Not yet seen on a real page: dedicated fields for
> year built and building type, so for now those come from the description text
> (see [Verify the willhaben mapping](#1-verify-the-willhaben-mapping)).

## Quick start

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
```

```bash
uv run immob --help
```

## First run

### 1. Verify the willhaben mapping

Open a willhaben search page for apartments in Kärnten in your browser, press
**F5**, then save it with Ctrl+S. Do the same with one listing page. Then:

```bash
uv run immob inspect ~/Downloads/search-page.html
```

```bash
uv run immob inspect ~/Downloads/listing-page.html
```

The reload matters: willhaben is a single-page app, so after clicking around,
the data embedded in the page still belongs to the first page you opened (often
the home page). If `inspect` finds no adverts, it prints an outline of the data
it did find; `--save-json FILE` saves all of it.

`inspect` prints every attribute willhaben sends. Compare the names with
`ATTRIBUTE_NAMES` in `src/immob/sources/willhaben.py` (e.g. is the floor in
`FLOOR`? the construction year in `CONSTRUCTION_YEAR`?) and adjust the lists.
Fields that aren't structured are also searched for in the description text,
so a wrong name means less precise data, not a crash.

### 2. Edit your profile

`profiles/kaernten.yaml` holds your criteria: price (optionally including a
garage space that costs extra), monthly costs, size, rooms, floor, year built,
old/new building, garage (covered: garage or Tiefgarage), any parking, elevator,
balcony/terrace, cellar, commission, postal codes and words to exclude (e.g.
*Baurecht*). The comments in the file explain each option.

By default the crawler reads all apartments in Kärnten in willhaben's default
order, a few pages per run, and filters them on your machine. It's better to
let willhaben filter first: on willhaben.at, set region, price, area, rooms and
sort by newest, then copy the URL from the address bar into the profile:

```yaml
willhaben:
  search_url: "https://www.willhaben.at/iad/immobilien/eigentumswohnung/kaernten?..."
```

The crawler keeps that URL's filters and only changes `page` and `rows`.
`crawl` prints how many listings willhaben found for the search
("Seen 270 of 412 listings found"), so you can tell whether `max_pages` is
enough. Once your database is seeded, page 1 of a newest-first search is
where new listings appear.

### 3. Seed the database

```bash
uv run immob crawl --seed
```

This stores all current listings and marks them as already notified, so your
first real run doesn't send you hundreds of messages. Then look at what's there:

```bash
uv run immob search
```

```bash
uv run immob show 123456789
```

`show` prints one listing, its price history and why it matches the profile
or not, which helps when tuning the filters.

### 4. Set up Telegram

1. In Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`
   and follow the steps. You get a **bot token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser and find
   `"chat":{"id": ...}`. That number is your **chat id**.
4. Store both in a file outside the repository, `~/.config/immob.env`, as plain
   `KEY=value` lines (no `export`, no quotes), so both the shell and systemd
   can read it:

   ```
   TELEGRAM_BOT_TOKEN=123456:ABC...
   TELEGRAM_CHAT_ID=987654321
   ```

   Then make it readable only by you: `chmod 600 ~/.config/immob.env`.

5. Test it. `set -a` exports the variables the file defines, so `immob` sees them:

   ```bash
   set -a && . ~/.config/immob.env && set +a && uv run immob telegram-test
   ```

The bot token gives full control of the bot: never commit it (`.env` files are
already in `.gitignore`).

### 5. Run it regularly

See [Run on a server](#run-on-a-server): a machine that's always on runs the
crawl on a schedule and alerts you on Telegram when a run fails.

## Run on a server

A Linux machine that's always on (e.g. a Debian box at home) can run immob on a
schedule with a systemd timer. Requests then come from a home connection, and
the database stays on that machine.

**1. Install uv and immob.** No clone is needed: uv installs the `immob` command
straight from GitHub.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

```bash
uv tool install git+https://github.com/gmarkop/Immob
```

For a private repository, give the machine a read-only
[deploy key](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)
and install with `uv tool install git+ssh://git@github.com/gmarkop/Immob.git`
(or a `Host` alias from `~/.ssh/config` in place of `github.com`).

**2. A data folder** for the profile and the database. Copy your profile (and
`immob.db`, to keep its history) there:

```bash
mkdir -p ~/immob/profiles
```

**3. Telegram settings** in `~/.config/immob.env`, as in
[Set up Telegram](#4-set-up-telegram) (plain `KEY=value` lines, `chmod 600`).

**4. Seed** the database once, so the first scheduled run doesn't notify you
about every existing listing:

```bash
cd ~/immob && immob crawl --seed
```

**5. Install the systemd units.** This writes three files to
`~/.config/systemd/user/` with this machine's paths filled in:

```bash
cd ~/immob && immob install-service
```

- `immob.service` runs `immob crawl` in `~/immob`.
- `immob.timer` starts it at 07:00, 13:00 and 19:00 local time, plus a random
  delay of up to 15 minutes. Change the times with
  `--calendar "*-*-* 08,18:00"` (systemd's
  [OnCalendar](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html#Calendar%20Events)
  syntax).
- `immob-failure.service` runs when a crawl fails (including when willhaben's
  pages change and the search can't be read) and sends a Telegram alert with
  the last log lines. Without it, a broken crawler and a quiet market both look
  like silence.

**6. Enable the timer.** `enable-linger` lets your user's timers run while
you're logged out:

```bash
sudo loginctl enable-linger $USER
```

```bash
systemctl --user daemon-reload && systemctl --user enable --now immob.timer
```

Run once now and read the log:

```bash
systemctl --user start immob.service; journalctl --user -u immob -n 20 --no-pager
```

To test the alert path, run `immob alert "test alert"` in `~/immob` after
loading the env file (as in step 5 of the Telegram setup).

**Day to day:**

| What | Command |
|---|---|
| Did the last run work? | `journalctl --user -u immob -n 20 --no-pager` |
| When is the next run? | `systemctl --user list-timers immob.timer` |
| Current matches | `cd ~/immob && immob search` |
| Update immob | `uv tool upgrade immob` |

After an update that changes the unit files, run `immob install-service` and
`systemctl --user daemon-reload` again.

## Commands

| Command | What it does |
|---|---|
| `immob crawl` | Fetch listings, store them, send notifications. `--seed` for the first run, `--no-notify` to skip Telegram, `--pages N`, `--no-details`, `--refresh-details` to re-read detail pages after an update, `--cache-dir DIR` while developing. |
| `immob search` | Table of stored listings that match the profile. |
| `immob show ID` | One listing with price history and the filter verdict. `--text` also prints the ad's text, to see where yes/no/? values came from. |
| `immob notify` | Send pending notifications without crawling. |
| `immob telegram-test` | Check the Telegram setup. |
| `immob alert MESSAGE` | Send an alert to Telegram; text piped in (e.g. log lines) is attached. |
| `immob install-service` | Write systemd units for scheduled runs with failure alerts; see [Run on a server](#run-on-a-server). |
| `immob inspect FILE_OR_URL` | Dump a willhaben page's raw attributes. |

All commands take `--profile` (default `profiles/kaernten.yaml`, or set
`IMMOB_PROFILE`) and `--db` (default `immob.db`, or set `IMMOB_DB`). Add `-v`
before the command for detailed logs: `immob -v crawl`.

## How it works

```
willhaben search pages ──► Listing (summary) ──► matches profile? ──► fetch detail page
                                                                           │
SQLite (listings + price_history) ◄──────────────── upsert ◄───────────────┘
        │
        └─► new listings / price drops matching the profile ──► Telegram
```

| Module | Role |
|---|---|
| `models.py` | `Listing`, the one normalized shape every source produces. `None` means "the ad doesn't say", which differs from `False`/`0`. |
| `sources/base.py` | The `Source` interface. A new portal is a new class with `search()` and `fetch_details()`. |
| `sources/willhaben.py` | Reads the JSON in willhaben's `__NEXT_DATA__` script tag rather than the visible HTML. |
| `extract.py` | Regexes that find floor, year built, parking, elevator and commission in German ad text. |
| `http.py` | A polite HTTP client: obeys robots.txt, waits 3–5 s between requests, backs off on errors. |
| `storage.py` | SQLite with plain SQL. Updates never overwrite known values with unknowns (`COALESCE`). |
| `filters.py` | The YAML profile and the matching logic, with human-readable rejection reasons. |
| `pipeline.py` | The crawl and notify steps. Detail pages are fetched only for possible matches. |
| `notify.py` | Telegram messages; errors are cleaned so the bot token never shows up in logs. |
| `cli.py` | The `immob` command (Typer). |

## Being a good citizen

This is a personal tool. It fetches a few pages a few times a day, waits
between requests and stops if a site's robots.txt disallows the pages. Don't
increase the frequency much, and don't republish the collected data: willhaben's
terms of use and EU database rights protect the listings. If willhaben blocks or
disallows crawling, the fallback is to parse willhaben's own e-mail alerts
("Suchagent") instead.

## Development

```bash
uv run pytest
```

```bash
uv run ruff check . && uv run ruff format .
```

Tests never touch the network: `httpx.MockTransport` serves synthetic pages
built in `tests/conftest.py`.

## Roadmap

- **Phase 2:** more sources (ImmoScout24, derStandard, Kleine Zeitung,
  edikte.justiz.gv.at), duplicate detection across portals.
- **Phase 3 (done):** scheduled runs with failure alerts, via a systemd timer on an always-on machine.
- **Phase 4:** Streamlit dashboard with a map, € per m² by district and a
  full purchase-cost calculator (property transfer tax, land registry fee,
  notary, commission).
