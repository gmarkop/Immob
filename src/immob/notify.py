"""Telegram notifications.

Setup: create a bot by messaging @BotFather in Telegram, then set the
TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables (see README).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from html import escape

import httpx

from immob.models import Listing, StoredListing, format_eur

API_URL = "https://api.telegram.org"


class NotifierError(Exception):
    """Base class. Messages never contain the bot token, so they're safe to print."""


class NotifierConfigError(NotifierError):
    pass


class TelegramError(NotifierError):
    pass


class TelegramNotifier:
    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._chat_id = chat_id
        self._sleep = sleep
        # The token is part of the URL path, so never log request URLs at INFO
        # level (the CLI silences the httpx logger for this reason).
        self._client = httpx.Client(
            base_url=f"{API_URL}/bot{token}", timeout=30, transport=transport
        )

    @classmethod
    def from_env(cls) -> TelegramNotifier:
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise NotifierConfigError(
                "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to send Telegram notifications."
            )
        return cls(token, chat_id)

    def __enter__(self) -> TelegramNotifier:
        return self

    def __exit__(self, *exc: object) -> None:
        self._client.close()

    def send(self, html_text: str) -> None:
        payload = {"chat_id": self._chat_id, "text": html_text, "parse_mode": "HTML"}
        # httpx's own exceptions include the request URL, i.e. the bot token. Replace
        # them with a clean message, and "from None" drops the original from tracebacks.
        try:
            response = self._client.post("/sendMessage", json=payload)
            if response.status_code == 429:  # rate limited: Telegram says how long to wait
                retry_after = response.json().get("parameters", {}).get("retry_after", 5)
                self._sleep(retry_after)
                response = self._client.post("/sendMessage", json=payload)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status, body = exc.response.status_code, exc.response.text
            raise TelegramError(f"Telegram answered HTTP {status}: {body}") from None
        except httpx.HTTPError as exc:
            raise TelegramError(f"could not reach Telegram ({type(exc).__name__})") from None
        self._sleep(1)  # stay well under Telegram's limit of ~1 message/second per chat


def format_listing(
    listing: StoredListing, *, resent: bool = False, also_on: Sequence[Listing] = ()
) -> str:
    """One listing as a Telegram message (HTML subset; all ad text escaped).

    `resent` marks a listing sent again on request (`immob notify --resend`);
    `also_on` are the same flat's listings on other sites.
    """
    old_price = listing.notified_price
    if resent:
        headline = "🏠 <b>Current match</b>"
    elif old_price is not None and listing.price_eur is not None and listing.price_eur < old_price:
        drop = 100 * (old_price - listing.price_eur) / old_price
        headline = (
            f"📉 <b>Price drop</b> {format_eur(old_price)} → "
            f"{format_eur(listing.price_eur)} (−{drop:.0f}%)"
        )
    else:
        headline = "🏠 <b>New listing</b>"

    facts = [format_eur(listing.price_eur)]
    if listing.area_m2:
        facts.append(f"{listing.area_m2:g} m²")
    if listing.price_per_m2:
        facts.append(f"{format_eur(listing.price_per_m2)}/m²")
    if listing.rooms:
        facts.append(f"{listing.rooms:g} Zi.")
    if listing.floor_display:
        facts.append(listing.floor_display)
    if listing.year_built:
        facts.append(f"Bj. {listing.year_built}")

    features = [
        _garage_feature(listing),
        _feature("Balkon", listing.has_balcony),
        _feature("Keller", listing.has_cellar),
        _feature("Lift", listing.has_elevator),
        _feature("Provisionsfrei", listing.commission_free),
    ]
    costs = []
    if listing.monthly_costs_eur:
        costs.append(f"{format_eur(listing.monthly_costs_eur)}/Monat")
    if listing.heating:
        costs.append(f"Heizung: {listing.heating}")
    if listing.hwb is not None or listing.energy_class:
        hwb = f"{listing.hwb:g}" if listing.hwb is not None else "?"
        costs.append(f"HWB {hwb}" + (f" ({listing.energy_class})" if listing.energy_class else ""))
    if listing.building_type:
        features.insert(0, listing.building_type.value.capitalize())

    place = " ".join(filter(None, [listing.postal_code, listing.location]))
    lines = [
        headline,
        f'<a href="{escape(listing.url)}">{escape(listing.title)}</a>',
        escape(" · ".join(facts)),
        escape(" · ".join(costs)),
        escape(place),
        escape(" · ".join(f for f in features if f)),
        " · ".join(
            f'Auch auf <a href="{escape(other.url)}">{escape(other.source)}</a>'
            for other in also_on
        ),
    ]
    return "\n".join(line for line in lines if line)


def _garage_feature(listing: StoredListing) -> str | None:
    """ "Garage ✓", "Garage ✓ (+€ 18.000)", "Garage ✓ (incl.)" or "Garage ✗"."""
    if listing.has_garage is None:
        return None
    if not listing.has_garage:
        return "Garage ✗"
    if listing.garage_price_eur is None:
        return "Garage ✓"
    return f"Garage ✓ ({listing.garage_display})"


def _feature(label: str, value: bool | None) -> str | None:
    if value is None:
        return None
    return f"{label} {'✓' if value else '✗'}"
