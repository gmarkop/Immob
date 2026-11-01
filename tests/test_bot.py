import json

import httpx
import pytest

from immob.bot import handle_update, run_bot
from immob.models import Listing
from immob.notify import TelegramNotifier, listing_buttons
from immob.storage import Storage

CHAT = "42"


def recording_notifier(calls: list, updates: list | None = None) -> TelegramNotifier:
    """A notifier whose Bot API calls are recorded as (method, payload)."""
    batches = list(updates or [])

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        payload = json.loads(request.content)
        calls.append((method, payload))
        if method == "getUpdates":
            return httpx.Response(
                200, json={"ok": True, "result": batches.pop(0) if batches else []}
            )
        return httpx.Response(200, json={"ok": True, "result": True})

    return TelegramNotifier("t", CHAT, transport=httpx.MockTransport(handler), sleep=lambda _: None)


def press(data: str, chat: str = CHAT, update_id: int = 1) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "cb1",
            "data": data,
            "message": {"message_id": 7, "chat": {"id": int(chat)}},
        },
    }


@pytest.fixture
def store(tmp_path):
    with Storage(tmp_path / "bot.db") as storage:
        original = Listing(source="willhaben", source_id="1", url="u", title="t", area_m2=54)
        copy = Listing(source="immoscout24", source_id="a", url="u2", title="t", area_m2=54)
        storage.upsert(original)
        storage.upsert(copy)
        storage.set_duplicate_of(copy, original)
        yield storage


def test_save_marks_the_whole_flat_and_updates_the_buttons(store):
    calls = []
    notifier = recording_notifier(calls)

    assert handle_update(store, notifier, press("fav:willhaben:1")) == "fav willhaben:1"

    assert store.get("willhaben", "1").favorite_at is not None
    assert store.get("immoscout24", "a").favorite_at is not None  # same flat, other site
    answer, edit = calls
    assert answer == ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "⭐ Gemerkt"})
    assert edit[0] == "editMessageReplyMarkup"
    assert edit[1]["message_id"] == 7
    assert edit[1]["reply_markup"]["inline_keyboard"][0][0]["text"] == "⭐ Gemerkt ✓"


def test_hide_and_undo(store):
    calls = []
    notifier = recording_notifier(calls)

    handle_update(store, notifier, press("hide:willhaben:1"))
    assert store.get("willhaben", "1").hidden_at is not None
    assert calls[-1][1]["reply_markup"]["inline_keyboard"] == [
        [{"text": "↩️ Doch interessiert", "callback_data": "unhide:willhaben:1"}]
    ]

    handle_update(store, notifier, press("unhide:willhaben:1"))
    assert store.get("willhaben", "1").hidden_at is None
    assert store.get("immoscout24", "a").hidden_at is None


def test_presses_from_other_chats_are_ignored(store):
    calls = []
    assert (
        handle_update(store, recording_notifier(calls), press("hide:willhaben:1", chat="999"))
        is None
    )
    assert store.get("willhaben", "1").hidden_at is None
    assert calls == []


def test_unknown_listing_is_answered(store):
    calls = []
    assert handle_update(store, recording_notifier(calls), press("fav:willhaben:404")) is None
    assert calls == [
        ("answerCallbackQuery", {"callback_query_id": "cb1", "text": "Unbekannte Wohnung"})
    ]


def test_telegram_errors_on_answer_are_harmless(store):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "query is too old"})

    notifier = TelegramNotifier(
        "t", CHAT, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    assert handle_update(store, notifier, press("fav:willhaben:1")) == "fav willhaben:1"
    assert store.get("willhaben", "1").favorite_at is not None  # applied anyway


def test_run_bot_polls_and_confirms_updates(store, tmp_path):
    calls = []
    notifier = recording_notifier(calls, updates=[[press("fav:willhaben:1", update_id=5)], []])
    polls = iter([True, True, False])

    run_bot(tmp_path / "bot.db", notifier, keep_running=lambda: next(polls))

    polls_made = [payload for method, payload in calls if method == "getUpdates"]
    assert "offset" not in polls_made[0]
    assert polls_made[1]["offset"] == 6  # confirms update 5
    assert polls_made[0]["allowed_updates"] == ["callback_query"]
    assert store.get("willhaben", "1").favorite_at is not None


def test_run_bot_waits_when_telegram_is_unreachable(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    slept = []
    notifier = TelegramNotifier(
        "t", CHAT, transport=httpx.MockTransport(handler), sleep=lambda _: None
    )
    polls = iter([True, False])
    run_bot(tmp_path / "bot.db", notifier, keep_running=lambda: next(polls), sleep=slept.append)
    assert slept == [30]


def test_buttons_reflect_state():
    listing = Listing(source="willhaben", source_id="1", url="u", title="t")
    from datetime import UTC, datetime

    from immob.models import StoredListing

    now = datetime.now(UTC)
    fresh = StoredListing(**listing.model_dump(), first_seen=now, last_seen=now)
    assert [b["callback_data"] for b in listing_buttons(fresh)[0]] == [
        "fav:willhaben:1",
        "hide:willhaben:1",
    ]
    saved = fresh.model_copy(update={"favorite_at": now})
    assert listing_buttons(saved)[0][0] == {
        "text": "⭐ Gemerkt ✓",
        "callback_data": "unfav:willhaben:1",
    }
