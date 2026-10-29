import json
from datetime import UTC, datetime

import httpx
import pytest

from immob.models import StoredListing
from immob.notify import TelegramError, TelegramNotifier, format_listing

NOW = datetime(2026, 10, 1, tzinfo=UTC)


def stored(**fields) -> StoredListing:
    defaults = {
        "source": "willhaben",
        "source_id": "1",
        "url": "https://www.willhaben.at/iad/x?a=1&b=2",
        "title": "3 Zimmer <Balkon> & Garage",
        "price_eur": 189000,
        "area_m2": 63,
        "first_seen": NOW,
        "last_seen": NOW,
    }
    return StoredListing(**(defaults | fields))


def test_format_new_listing_escapes_html():
    text = format_listing(
        stored(
            rooms=3,
            floor=2,
            has_garage=True,
            garage_price_eur=18000,
            has_balcony=True,
            monthly_costs_eur=217.85,
            heating="Fernwärme",
            postal_code="9020",
        )
    )
    assert text.startswith("🏠 <b>New listing</b>")
    assert "3 Zimmer &lt;Balkon&gt; &amp; Garage" in text
    assert 'href="https://www.willhaben.at/iad/x?a=1&amp;b=2"' in text
    assert "€ 189.000 · 63 m² · € 3.000/m² · 3 Zi. · 2. OG" in text
    assert "Garage ✓ (+€ 18.000) · Balkon ✓" in text
    assert "€ 218/Monat · Heizung: Fernwärme" in text


def test_format_price_drop():
    text = format_listing(stored(price_eur=180000, notified_price=200000, notified_at=NOW))
    assert "📉 <b>Price drop</b> € 200.000 → € 180.000 (−10%)" in text


def test_send_posts_to_bot_api():
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "123:SECRET", "42", transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    notifier.send("hello")

    assert requests[0].url == "https://api.telegram.org/bot123:SECRET/sendMessage"
    assert json.loads(requests[0].content) == {
        "chat_id": "42",
        "text": "hello",
        "parse_mode": "HTML",
    }


def test_send_retries_once_when_rate_limited():
    responses = [
        httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 7}}),
        httpx.Response(200, json={"ok": True}),
    ]
    slept = []
    notifier = TelegramNotifier(
        "t", "42", transport=httpx.MockTransport(lambda r: responses.pop(0)), sleep=slept.append
    )
    notifier.send("hello")
    assert slept[0] == 7


def test_errors_never_contain_the_token():
    notifier = TelegramNotifier(
        "123:SECRET",
        "42",
        transport=httpx.MockTransport(lambda r: httpx.Response(401, text="Unauthorized")),
        sleep=lambda _: None,
    )
    with pytest.raises(TelegramError) as exc_info:
        notifier.send("hello")
    assert "SECRET" not in str(exc_info.value)
    assert exc_info.value.__suppress_context__  # original httpx error hidden from tracebacks
