# immob

A personal tracker for apartments for sale in Carinthia (Kärnten), Austria.
It reads listings from willhaben.at, stores them in a local SQLite database,
filters them with your criteria and sends new matches and price drops to
Telegram.

> **Status: Phase 1.** The willhaben search-page parser has been checked
> against a real saved page. The detail-page fields (year built, etc.) are not
> verified yet (see [Verify the willhaben mapping](#1-verify-the-willhaben-mapping)).

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

`profiles/kaernten.yaml` holds your criteria: price, size, rooms, floor, year
built, old/new building, parking, elevator, commission, postal codes and words
to exclude (e.g. *Baurecht*). The comments in the file explain each option.

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
4. Store both in a file outside the repository, e.g. `~/.config/immob.env`:

   ```bash
   export TELEGRAM_BOT_TOKEN="123456:ABC..."
   export TELEGRAM_CHAT_ID="987654321"
   ```

5. Test it:

   ```bash
   . ~/.config/immob.env && uv run immob telegram-test
   ```

The bot token gives full control of the bot: never commit it (`.env` files are
already in `.gitignore`).

### 5. Run it regularly

```bash
. ~/.config/immob.env && uv run immob crawl
```

To run it three times a day with cron (`crontab -e`):

```
0 7,13,19 * * * cd $HOME/Immob && . $HOME/.config/immob.env && $HOME/.local/bin/uv run immob crawl >> crawl.log 2>&1
```

## Commands

| Command | What it does |
|---|---|
| `immob crawl` | Fetch listings, store them, send notifications. `--seed` for the first run, `--no-notify` to skip Telegram, `--pages N`, `--no-details`, `--cache-dir DIR` while developing. |
| `immob search` | Table of stored listings that match the profile. |
| `immob show ID` | One listing with price history and the filter verdict. |
| `immob notify` | Send pending notifications without crawling. |
| `immob telegram-test` | Check the Telegram setup. |
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
- **Phase 3:** run on a schedule in the cloud (GitHub Actions).
- **Phase 4:** Streamlit dashboard with a map, € per m² by district and a
  full purchase-cost calculator (property transfer tax, land registry fee,
  notary, commission).
