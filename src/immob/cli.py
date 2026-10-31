"""Command-line interface: `immob --help`."""

from __future__ import annotations

import json
import logging
import shutil
import sys
from datetime import UTC, datetime, timedelta
from html import escape
from importlib import resources
from pathlib import Path
from string import Template
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from immob.filters import FilterProfile, load_profile
from immob.http import PoliteClient, RobotsDisallowed
from immob.models import Listing, StoredListing, format_eur
from immob.notify import NotifierError, TelegramNotifier
from immob.pipeline import CrawlStats, current_matches, resend, run_crawl, send_pending
from immob.sources.base import ParseError, Source
from immob.sources.immoscout24 import (
    ImmoScout24Source,
    find_results,
    initial_state,
    parse_expose,
    parse_hit,
)
from immob.sources.willhaben import (
    WillhabenSource,
    attributes,
    extract_next_data,
    find_adverts,
)
from immob.storage import Storage

app = typer.Typer(help="Track apartment listings in Carinthia.", no_args_is_help=True)
console = Console()

DbOption = Annotated[Path, typer.Option("--db", envvar="IMMOB_DB", help="SQLite database file.")]
ProfileOption = Annotated[
    Path, typer.Option("--profile", "-p", envvar="IMMOB_PROFILE", help="Filter profile (YAML).")
]
DEFAULT_DB = Path("immob.db")
DEFAULT_PROFILE = Path("profiles/kaernten.yaml")


@app.callback()
def main(verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False) -> None:
    logging.basicConfig(
        level=logging.INFO if verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # httpx logs full request URLs at INFO, and Telegram URLs contain the bot token.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def build_sources(profile: FilterProfile, client: PoliteClient) -> list[tuple[Source, int]]:
    """The sites to crawl, each with its page limit. willhaben is always on;
    ImmoScout24 when the profile has an immoscout24.search_url."""
    willhaben = profile.willhaben
    sources: list[tuple[Source, int]] = [
        (
            WillhabenSource(
                client,
                region=willhaben.region,
                property_type=willhaben.property_type,
                search_url=willhaben.search_url,
            ),
            willhaben.max_pages,
        )
    ]
    if profile.immoscout24.search_url:
        sources.append(
            (
                ImmoScout24Source(client, search_url=profile.immoscout24.search_url),
                profile.immoscout24.max_pages,
            )
        )
    return sources


def _load_profile(path: Path) -> FilterProfile:
    try:
        return load_profile(path)
    except FileNotFoundError:
        console.print(f"[red]Profile not found: {path}[/red]")
        raise typer.Exit(1) from None


@app.command()
def crawl(
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    pages: Annotated[int | None, typer.Option(help="Search pages to read.")] = None,
    details: Annotated[bool, typer.Option(help="Fetch detail pages of candidates.")] = True,
    max_details: Annotated[int, typer.Option(help="Detail pages per run, at most.")] = 25,
    refresh_details: Annotated[
        bool, typer.Option(help="Re-fetch detail pages fetched before (after an update).")
    ] = False,
    notify: Annotated[bool, typer.Option(help="Send Telegram notifications.")] = True,
    seed: Annotated[
        bool, typer.Option(help="First run: store everything, mark it as already notified.")
    ] = False,
    cache_dir: Annotated[
        Path | None, typer.Option(help="Cache pages here for an hour (for development).")
    ] = None,
) -> None:
    """Fetch new listings, store them and notify about matches."""
    profile = _load_profile(profile_path)
    notifier = None
    if notify and not seed:
        try:  # fail before crawling for minutes, not after
            notifier = TelegramNotifier.from_env()
        except NotifierError as exc:
            console.print(f"[red]{exc}[/red] Or use --no-notify.")
            raise typer.Exit(1) from None

    with Storage(db) as store, PoliteClient(cache_dir=cache_dir) as client:
        # Every site's search is read before anything is notified, so a flat
        # listed on both is already linked as a duplicate when messages go out.
        failed = False
        for source, max_pages in build_sources(profile, client):
            try:
                stats = run_crawl(
                    source,
                    store,
                    profile,
                    max_pages=pages or max_pages,
                    fetch_details=details,
                    max_details=max_details,
                    refresh_details=refresh_details,
                )
            except RobotsDisallowed as exc:
                # One site refusing crawlers shouldn't stop the others.
                console.print(f"[red]{source.name}: {exc}[/red]")
                failed = True
                continue
            _print_stats(source.name, stats)
            failed = failed or stats.search_failed

        if seed:
            marked = store.mark_all_notified()
            console.print(f"Seeded: {marked} listings marked as already notified.")
        elif notifier:
            _send_notifications(store, profile, notifier)

    if failed:
        # A non-zero exit code makes systemd count the run as failed, which
        # triggers the failure alert (see `immob install-service`).
        console.print("[red]The search failed; see the log above.[/red]")
        raise typer.Exit(1)


def _print_stats(name: str, stats: CrawlStats) -> None:
    seen = f"Seen {stats.seen} listings"
    if stats.total_found is not None:
        seen = f"Seen {stats.seen} of {stats.total_found} listings found"
    console.print(
        f"{name}: {seen}: {stats.new} new, {stats.price_changed} price changes, "
        f"{stats.details_fetched} detail pages fetched."
    )
    if stats.complete:
        console.print(f"  {stats.gone} gone, {stats.duplicates} already known from another site.")
    elif not stats.search_failed:
        console.print("  Not all pages read, so gone listings weren't checked.")
    if stats.errors:
        console.print(f"  [yellow]{stats.errors} errors, run with -v for details.[/yellow]")


@app.command()
def notify(
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    resend_matches: Annotated[
        bool,
        typer.Option("--resend", help="Send all current matches, including ones notified before."),
    ] = False,
    max_age_days: Annotated[
        int, typer.Option(help="With --resend: skip listings not seen for this many days.")
    ] = 7,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Don't ask before sending many messages.")
    ] = False,
) -> None:
    """Send pending notifications without crawling, or with --resend all current matches.

    --resend helps after changing the profile: listings stored earlier that now
    match were already marked as seen, so they would never be sent otherwise.
    """
    profile = _load_profile(profile_path)
    try:
        notifier = TelegramNotifier.from_env()
    except NotifierError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    with Storage(db) as store:
        if not resend_matches:
            _send_notifications(store, profile, notifier)
            return

        matches = current_matches(store, profile, max_age_days=max_age_days)
        if not matches:
            console.print("No listings match the profile.")
            return
        if len(matches) > 10 and not yes:
            typer.confirm(f"Send {len(matches)} Telegram messages?", abort=True)
        with notifier:
            try:
                sent = resend(store, notifier, matches)
            except NotifierError as exc:
                console.print(f"[red]{exc}[/red]")
                raise typer.Exit(1) from None
        console.print(f"Sent {sent} Telegram notifications.")


def _send_notifications(store: Storage, profile: FilterProfile, notifier: TelegramNotifier) -> None:
    with notifier:
        try:
            sent = send_pending(store, profile, notifier)
        except NotifierError as exc:
            # Listings not yet sent stay pending and go out on the next run.
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None
    console.print(f"Sent {sent} Telegram notifications.")


@app.command("telegram-test")
def telegram_test() -> None:
    """Send a test message to check the Telegram setup."""
    try:
        with TelegramNotifier.from_env() as notifier:
            notifier.send("👋 immob is set up and can reach you.")
    except NotifierError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print("Sent. Check Telegram.")


@app.command()
def alert(
    message: Annotated[str, typer.Argument(help="The alert's headline.")],
) -> None:
    """Send an alert to Telegram, with any text piped in (e.g. log lines) attached.

    Used by the systemd failure unit: journalctl ... | immob alert "immob failed".
    """
    # Piped text arrives on stdin; on an interactive terminal there is none to read.
    details = "" if sys.stdin.isatty() else sys.stdin.read().strip()
    text = f"⚠️ <b>{escape(message)}</b>"
    if details:
        # Telegram allows 4096 characters; the end of a log matters most.
        text += f"\n<pre>{escape(details[-3000:])}</pre>"
    try:
        with TelegramNotifier.from_env() as notifier:
            notifier.send(text)
    except NotifierError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None


UNIT_FILES = ("immob.service", "immob.timer", "immob-failure.service")


@app.command("install-service")
def install_service(
    data_dir: Annotated[
        Path, typer.Option(help="Folder with profiles/ and immob.db; the service runs here.")
    ] = Path("."),
    calendar: Annotated[
        str, typer.Option(help="When to run, in systemd's OnCalendar syntax (local time).")
    ] = "*-*-* 07,13,19:00",
    unit_dir: Annotated[
        Path | None,
        typer.Option(help="Where to write the unit files [default: ~/.config/systemd/user]."),
    ] = None,
) -> None:
    """Write systemd user units that run `immob crawl` on a schedule and alert on failure.

    Secrets stay out of the units: they read TELEGRAM_BOT_TOKEN and
    TELEGRAM_CHAT_ID from ~/.config/immob.env.
    """
    # The path this command was started from, e.g. ~/.local/bin/immob. Not
    # resolved: uv's symlink there keeps working across `uv tool upgrade`.
    executable = shutil.which("immob") or sys.argv[0]
    values = {
        "exec": str(Path(executable).absolute()),
        "workdir": str(data_dir.expanduser().absolute()),
        "calendar": calendar,
    }
    unit_dir = unit_dir or Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    for name in UNIT_FILES:
        template = resources.files("immob.systemd").joinpath(name).read_text(encoding="utf-8")
        (unit_dir / name).write_text(Template(template).substitute(values), encoding="utf-8")
        console.print(f"Wrote {unit_dir / name}")

    workdir = Path(values["workdir"])
    if not (workdir / DEFAULT_PROFILE).exists():
        console.print(f"[yellow]No profile at {workdir / DEFAULT_PROFILE} yet.[/yellow]")
    if not (Path.home() / ".config/immob.env").exists():
        console.print("[yellow]~/.config/immob.env doesn't exist yet (Telegram settings).[/yellow]")
    console.print(
        "\nNext:\n"
        "  sudo loginctl enable-linger $USER     # run timers while you're logged out\n"
        "  systemctl --user daemon-reload\n"
        "  systemctl --user enable --now immob.timer\n"
        "  systemctl --user start immob.service  # one run now, to test",
        markup=False,
        highlight=False,
    )


@app.command()
def search(
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    max_age_days: Annotated[
        int, typer.Option(help="Hide listings not seen for this many days (likely sold).")
    ] = 7,
) -> None:
    """Show stored listings that match the profile."""
    profile = _load_profile(profile_path)
    with Storage(db) as store:
        listings = current_matches(store, profile, max_age_days=max_age_days)

    table = Table(title=f"{len(listings)} listings matching '{profile.name}'")
    for column in (
        "ID",
        "Price",
        "m²",
        "€/m²",
        "Rooms",
        "Floor",
        "Year",
        "Type",
        "Garage",
        "Prov.",
    ):
        table.add_column(column, no_wrap=True)
    table.add_column("Place", overflow="ellipsis", no_wrap=True)  # the one column that may shrink
    for listing in listings:
        table.add_row(
            f"[link={listing.url}]{listing.source_id}[/link]",
            format_eur(listing.price_eur),
            _num(listing.area_m2),
            _num(listing.price_per_m2 and round(listing.price_per_m2)),
            _num(listing.rooms),
            listing.floor_display or "?",
            str(listing.year_built or "?"),
            listing.building_type or "?",
            listing.garage_display,
            _yes_no(None if listing.commission_free is None else not listing.commission_free),
            " ".join(filter(None, [listing.postal_code, listing.location])),
        )
    console.print(table)


@app.command()
def gone(
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    days: Annotated[int, typer.Option(help="Show listings gone in the last N days.")] = 30,
    show_all: Annotated[
        bool, typer.Option("--all", help="Include listings that don't match the profile.")
    ] = False,
) -> None:
    """Listings that left the search: how long they were online, and at what price."""
    profile = _load_profile(profile_path)
    since = datetime.now(UTC) - timedelta(days=days)
    with Storage(db) as store:
        listings = [
            listing for listing in store.gone_since(since) if show_all or profile.matches(listing)
        ]
        histories = {listing.ref: store.price_history(*listing.key) for listing in listings}

    table = Table(title=f"{len(listings)} listings gone in the last {days} days")
    for column in ("ID", "Last price", "Price changes", "Online", "Gone", "Rooms", "m²"):
        table.add_column(column, no_wrap=True)
    table.add_column("Place", overflow="ellipsis", no_wrap=True)
    for listing in listings:
        prices = [price for _, price in histories[listing.ref] if price is not None]
        change = "–"
        if len(prices) > 1:
            change = f"{len(prices) - 1}× ({format_eur(prices[0])} → {format_eur(prices[-1])})"
        table.add_row(
            f"[link={listing.url}]{listing.source_id}[/link]",
            format_eur(listing.price_eur),
            change,
            f"{listing.days_online} days",
            f"{listing.gone_at:%Y-%m-%d}",
            _num(listing.rooms),
            _num(listing.area_m2),
            " ".join(filter(None, [listing.postal_code, listing.location])),
        )
    console.print(table)


@app.command()
def show(
    source_id: str,
    source: str = WillhabenSource.name,
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    text: Annotated[bool, typer.Option("--text", help="Also print the ad's text.")] = False,
) -> None:
    """Show one listing, its price history and why it does or doesn't match."""
    profile = _load_profile(profile_path)
    with Storage(db) as store:
        listing = store.get(source, source_id)
        if listing is None:
            console.print(f"[red]No listing {source}/{source_id} in {db}[/red]")
            raise typer.Exit(1)
        history = store.price_history(source, source_id)
        original = store.get_ref(listing.duplicate_of) if listing.duplicate_of else None
        copies = store.duplicates_of(listing)

    _print_listing(listing)
    for label, other in [("Same flat as", original)] + [("Also listed as", c) for c in copies]:
        if other is not None:
            console.print(f"  {label:<20}{other.source} {other.source_id}: {other.url}")
    if text:
        # The text the yes/no/? fields were extracted from (markup=False: ad
        # text may contain [brackets] that rich would read as formatting).
        console.print("\n[bold]Text[/bold]")
        console.print(listing.description or "(none)", markup=False, highlight=False)
    console.print("\n[bold]Price history[/bold]")
    for seen_at, price in history:
        console.print(f"  {seen_at:%Y-%m-%d %H:%M}  {format_eur(price)}")
    reasons = profile.rejections(listing)
    verdict = (
        "[green]matches[/green]" if not reasons else "[red]rejected:[/red] " + "; ".join(reasons)
    )
    console.print(f"\nProfile '{profile.name}': {verdict}")


@app.command()
def inspect(
    page: Annotated[str, typer.Argument(help="A saved .html file, or a URL.")],
    index: Annotated[int, typer.Option(help="Which advert of a search page to show.")] = 0,
    save_json: Annotated[
        Path | None, typer.Option(help="Also save the page's embedded JSON to this file.")
    ] = None,
) -> None:
    """Show what a saved page contains, to check the field mapping.

    Save a willhaben or ImmoScout24 search or listing page from your browser
    (F5, then Ctrl+S) and run this on it. For willhaben it prints the raw
    attributes (compare with ATTRIBUTE_NAMES in sources/willhaben.py); for
    ImmoScout24, what immob extracts.
    """
    if page.startswith(("http://", "https://")):
        with PoliteClient() as client:
            page_html = client.get_text(page)
    else:
        path = Path(page).expanduser()
        if not path.is_file():
            console.print(
                f"[red]File not found: {path}[/red]\n"
                "Put quotes around names with spaces, and keep ~ outside them. Files "
                "saved by a Windows browser are under /mnt/c/Users/<name>/Downloads in WSL."
            )
            raise typer.Exit(1)
        page_html = path.read_text(encoding="utf-8", errors="replace")

    if "__NEXT_DATA__" not in page_html and "immobilienscout24" in page_html:
        _inspect_immoscout24(page_html, page)
        return

    try:
        data = extract_next_data(page_html)
    except ParseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None

    if save_json:
        save_json.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        console.print(f"Saved the embedded JSON to {save_json}")

    adverts = find_adverts(data)
    if not adverts:
        console.print(
            "[red]No advert-shaped objects found. This is how the page's data looks:[/red]"
        )
        for line in _describe_structure(data):
            console.print(line, markup=False, highlight=False)
        raise typer.Exit(1)

    console.print(f"Found {len(adverts)} advert(s); showing #{index}.")
    advert = adverts[index]
    table = Table(title=f"Advert {advert.get('id')}")
    table.add_column("Attribute")
    table.add_column("Values")
    for name, values in sorted(attributes(advert).items()):
        table.add_row(name, " | ".join(v[:120] for v in values))
    console.print(table)
    console.print(f"Other keys: {', '.join(k for k in advert if k != 'attributes')}")
    console.print(f"Path to adverts: {_path_to(data, advert) or '?'}")


def _inspect_immoscout24(page_html: str, page: str) -> None:
    """Print what immob extracts from an ImmoScout24 search or listing page."""
    try:
        results = find_results(initial_state(page_html))
    except ParseError:
        results = None
    if results is not None:
        hits = results.get("hits") or []
        table = Table(title=f"ImmoScout24 search: {len(hits)} of {results.get('totalHits')} hits")
        for column in ("ID", "Price", "m²", "Rooms", "Garage"):
            table.add_column(column, no_wrap=True)
        table.add_column("Place", overflow="ellipsis", no_wrap=True)
        for hit in hits:
            listing = parse_hit(hit)
            table.add_row(
                listing.source_id,
                format_eur(listing.price_eur),
                _num(listing.area_m2),
                _num(listing.rooms),
                listing.garage_display,
                " ".join(filter(None, [listing.postal_code, listing.location])),
            )
        console.print(table)
        console.print(
            f"Pagination: {json.dumps(results.get('pagination'), ensure_ascii=False)}",
            markup=False,
            highlight=False,
        )
        return

    url = page if page.startswith("http") else "https://www.immobilienscout24.at/expose/saved"
    try:
        listing, _ = parse_expose(page_html, url)
    except ParseError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    console.print(f"[bold]{listing.title}[/bold]")
    for field, value in listing.model_dump(exclude={"description"}).items():
        if value is not None:
            console.print(f"  {field:<20}{value}", markup=False, highlight=False)
    console.print(f"  {'labels found':<20}{listing.raw['labels']}", markup=False, highlight=False)


def _describe_structure(data: object, max_depth: int = 6, max_lines: int = 150) -> list[str]:
    """An indented outline of nested JSON: keys, list lengths and short values."""
    lines: list[str] = []

    def walk(node: object, label: str, depth: int) -> None:
        if len(lines) >= max_lines:
            return
        indent = "  " * depth
        if isinstance(node, dict):
            lines.append(f"{indent}{label}: {{{len(node)} keys}}")
            if depth < max_depth:
                for key, child in node.items():
                    walk(child, key, depth + 1)
        elif isinstance(node, list):
            lines.append(f"{indent}{label}: [{len(node)} items]")
            if node and depth < max_depth:
                walk(node[0], "[0]", depth + 1)  # lists are usually uniform: show one item
        else:
            lines.append(f"{indent}{label}: {json.dumps(node, ensure_ascii=False)[:60]}")

    walk(data, "(root)", 0)
    if len(lines) >= max_lines:
        lines.append(f"... cut at {max_lines} lines; use --save-json to see everything")
    return lines


def _path_to(data: object, target: object, path: str = "") -> str | None:
    """Where `target` sits inside `data`, e.g. 'props.pageProps.x[0]'."""
    if data is target:
        return path
    if isinstance(data, dict):
        children = ((f"{path}.{k}" if path else str(k), v) for k, v in data.items())
    elif isinstance(data, list):
        children = ((f"{path}[{i}]", v) for i, v in enumerate(data))
    else:
        return None
    for child_path, child in children:
        if (found := _path_to(child, target, child_path)) is not None:
            return found
    return None


def _print_listing(listing: StoredListing) -> None:
    console.print(f"[bold]{listing.title}[/bold]\n{listing.url}\n")
    commission = _yes_no(listing.commission_free)
    if listing.commission_text:
        commission += f" ({listing.commission_text})"
    fields = {
        "Price": format_eur(listing.price_eur),
        "Area": f"{_num(listing.area_m2)} m²",
        "Price/m²": format_eur(listing.price_per_m2),
        "Monthly costs": _eur_cents(listing.monthly_costs_eur, "gross"),
        "Heating costs": _eur_cents(listing.heating_costs_eur, "net"),
        "Heating": listing.heating or "?",
        "Energy (HWB)": _energy(listing),
        "Rooms": _num(listing.rooms),
        "Floor": listing.floor_display or "?",
        "Year built": listing.year_built or "?",
        "Building": listing.building_type or "?",
        "Garage": listing.garage_display,
        "Parking (any)": _yes_no(listing.has_parking),
        "Lift": _yes_no(listing.has_elevator),
        "Balcony/terrace": _yes_no(listing.has_balcony),
        "Cellar": _yes_no(listing.has_cellar),
        "Commission-free": commission,
        "Private seller": _yes_no(listing.is_private),
        "Place": " ".join(filter(None, [listing.postal_code, listing.location])) or "?",
        "First seen": f"{listing.first_seen:%Y-%m-%d}",
        "Last seen": f"{listing.last_seen:%Y-%m-%d}",
        "Gone": f"{listing.gone_at:%Y-%m-%d}" if listing.gone_at else "no, still listed",
        "Detail page parsed": _yes_no(listing.details_fetched),
    }
    for label, value in fields.items():
        console.print(f"  {label:<20}{value}")


def _eur_cents(amount: float | None, note: str) -> str:
    return "?" if amount is None else f"{format_eur(amount, cents=True)} per month ({note})"


def _energy(listing: Listing) -> str:
    if listing.hwb is None and listing.energy_class is None:
        return "?"
    hwb = f"{listing.hwb:g} kWh/m²a" if listing.hwb is not None else "HWB ?"
    return f"{hwb} (class {listing.energy_class or '?'})"


def _num(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def _yes_no(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "?"}[value]
