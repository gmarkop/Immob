"""Command-line interface: `immob --help`."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from immob.filters import FilterProfile, load_profile
from immob.http import PoliteClient, RobotsDisallowed
from immob.models import StoredListing, format_eur
from immob.notify import NotifierError, TelegramNotifier
from immob.pipeline import run_crawl, send_pending
from immob.sources.base import ParseError, Source
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


def build_source(profile: FilterProfile, client: PoliteClient) -> Source:
    settings = profile.willhaben
    return WillhabenSource(
        client,
        region=settings.region,
        property_type=settings.property_type,
        search_url=settings.search_url,
    )


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
        source = build_source(profile, client)
        try:
            stats = run_crawl(
                source,
                store,
                profile,
                max_pages=pages or profile.willhaben.max_pages,
                fetch_details=details,
                max_details=max_details,
            )
        except RobotsDisallowed as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(1) from None

        seen = f"Seen {stats.seen} listings"
        if stats.total_found is not None:
            seen = f"Seen {stats.seen} of {stats.total_found} listings found"
        console.print(
            f"{seen}: {stats.new} new, {stats.price_changed} price changes, "
            f"{stats.details_fetched} detail pages fetched."
        )
        if stats.errors:
            console.print(f"[yellow]{stats.errors} errors, run with -v for details.[/yellow]")

        if seed:
            marked = store.mark_all_notified()
            console.print(f"Seeded: {marked} listings marked as already notified.")
        elif notifier:
            _send_notifications(store, profile, notifier)


@app.command()
def notify(profile_path: ProfileOption = DEFAULT_PROFILE, db: DbOption = DEFAULT_DB) -> None:
    """Send pending notifications without crawling."""
    profile = _load_profile(profile_path)
    try:
        notifier = TelegramNotifier.from_env()
    except NotifierError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(1) from None
    with Storage(db) as store:
        _send_notifications(store, profile, notifier)


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
def search(
    profile_path: ProfileOption = DEFAULT_PROFILE,
    db: DbOption = DEFAULT_DB,
    max_age_days: Annotated[
        int, typer.Option(help="Hide listings not seen for this many days (likely sold).")
    ] = 7,
) -> None:
    """Show stored listings that match the profile."""
    profile = _load_profile(profile_path)
    cutoff = datetime.now(UTC) - timedelta(days=max_age_days)
    with Storage(db) as store:
        listings = [
            listing
            for listing in store.all()
            if listing.last_seen >= cutoff and profile.matches(listing)
        ]

    table = Table(title=f"{len(listings)} listings matching '{profile.name}'")
    for column in ("ID", "Price", "m²", "€/m²", "Rooms", "Floor", "Year", "Type", "Park.", "Prov."):
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
            _yes_no(listing.has_parking),
            _yes_no(None if listing.commission_free is None else not listing.commission_free),
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

    _print_listing(listing)
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
    page: Annotated[str, typer.Argument(help="A saved willhaben .html file, or a URL.")],
    index: Annotated[int, typer.Option(help="Which advert of a search page to show.")] = 0,
    save_json: Annotated[
        Path | None, typer.Option(help="Also save the page's embedded JSON to this file.")
    ] = None,
) -> None:
    """Print the raw attributes of a willhaben page, to check the field mapping.

    Save a willhaben search or listing page from your browser (Ctrl+S) and run
    this on it; compare the names with ATTRIBUTE_NAMES in sources/willhaben.py.
    """
    path = Path(page)
    if path.exists():
        page_html = path.read_text(encoding="utf-8")
    else:
        with PoliteClient() as client:
            page_html = client.get_text(page)

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
    fields = {
        "Price": format_eur(listing.price_eur),
        "Area": f"{_num(listing.area_m2)} m²",
        "Price/m²": format_eur(listing.price_per_m2),
        "Rooms": _num(listing.rooms),
        "Floor": listing.floor_display or "?",
        "Year built": listing.year_built or "?",
        "Building": listing.building_type or "?",
        "Parking": _yes_no(listing.has_parking),
        "Lift": _yes_no(listing.has_elevator),
        "Commission-free": _yes_no(listing.commission_free),
        "Private seller": _yes_no(listing.is_private),
        "Place": " ".join(filter(None, [listing.postal_code, listing.location])) or "?",
        "First seen": f"{listing.first_seen:%Y-%m-%d}",
        "Last seen": f"{listing.last_seen:%Y-%m-%d}",
        "Detail page parsed": _yes_no(listing.details_fetched),
    }
    for label, value in fields.items():
        console.print(f"  {label:<20}{value}")


def _num(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def _yes_no(value: bool | None) -> str:
    return {True: "yes", False: "no", None: "?"}[value]
