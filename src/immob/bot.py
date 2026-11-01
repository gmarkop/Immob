"""React to the buttons under Telegram messages: ⭐ Merken / 🙈 Nicht interessiert.

`immob bot` runs `run_bot`, a loop that long-polls Telegram for button presses
(`getUpdates`). Each press updates the database right away, and the message's
buttons change to show the new state, which is also how a press is undone.

Only one program may poll a bot's updates at a time: Telegram answers a second
poller with HTTP 409. So the crawl never polls; this loop is the only reader.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from immob.notify import TelegramError, TelegramNotifier, listing_buttons
from immob.storage import Storage

log = logging.getLogger(__name__)

# What each button does: (storage method, new state, confirmation shown in Telegram).
ACTIONS = {
    "fav": ("set_favorite", True, "⭐ Gemerkt"),
    "unfav": ("set_favorite", False, "Nicht mehr gemerkt"),
    "hide": ("set_hidden", True, "🙈 Ausgeblendet"),
    "unhide": ("set_hidden", False, "↩️ Wieder eingeblendet"),
}


def handle_update(store: Storage, notifier: TelegramNotifier, update: dict[str, Any]) -> str | None:
    """Apply one button press. Returns a short description, or None if ignored."""
    query = update.get("callback_query")
    if not query:
        return None
    message = query.get("message") or {}
    chat_id = (message.get("chat") or {}).get("id")
    if str(chat_id) != str(notifier.chat_id):
        # Only presses in your own chat count, whoever else finds the bot.
        log.warning("ignoring a button press from chat %s", chat_id)
        return None

    action, _, ref = (query.get("data") or "").partition(":")
    listing = store.get_ref(ref) if action in ACTIONS else None
    if listing is None:
        _quietly(notifier.answer_callback, query["id"], "Unbekannte Wohnung")
        return None

    method, state, confirmation = ACTIONS[action]
    getattr(store, method)(listing, state)
    updated = store.get(*listing.key)
    # Both calls can fail harmlessly: a press answered too late ("query is too
    # old"), or buttons that already look like this ("message is not modified").
    _quietly(notifier.answer_callback, query["id"], confirmation)
    if message.get("message_id") is not None:
        _quietly(notifier.edit_buttons, chat_id, message["message_id"], listing_buttons(updated))
    return f"{action} {ref}"


def run_bot(
    db: Path,
    notifier: TelegramNotifier,
    *,
    keep_running: Callable[[], bool] = lambda: True,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Long-poll Telegram for button presses and apply them, until stopped."""
    offset: int | None = None
    while keep_running():
        try:
            updates = notifier.get_updates(offset)
        except TelegramError as exc:
            log.warning("Telegram unreachable, retrying in 30 s: %s", exc)
            sleep(30)
            continue
        for update in updates:
            # Confirms this update to Telegram on the next poll, so it isn't resent.
            offset = update["update_id"] + 1
            with Storage(db) as store:
                if result := handle_update(store, notifier, update):
                    log.info("button pressed: %s", result)


def _quietly(call: Callable[..., Any], *args: Any) -> None:
    try:
        call(*args)
    except TelegramError as exc:
        log.info("Telegram call failed (harmless): %s", exc)
