"""Telegram I/O. New bot. Commands are /gap_* only."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

import requests

from . import config as C

log = logging.getLogger("gap.notify")

API = "https://api.telegram.org"

# After the last inbound message, wait this long with no new parts, then stitch.
DEBOUNCE_SEC = 2.5
STALE_SEC = 180.0

_handlers: dict[str, Callable[[list[str], dict], str]] = {}
_json_handler: Callable[[str, dict], str] | None = None
_listener_started = False
# (received_monotonic, msg, payload)
_pending: list[tuple[float, dict, str]] = []
_incomplete_notified = False


def _url(method: str) -> str:
    return f"{API}/bot{C.TELEGRAM_TOKEN}/{method}"


def configured() -> bool:
    return bool(C.TELEGRAM_TOKEN and C.TELEGRAM_CHAT_ID)


def send(text: str, quiet: bool = False, reply_to: int | None = None) -> int | None:
    if not configured():
        log.warning("telegram not configured; skip send")
        return None
    payload = {
        "chat_id": C.TELEGRAM_CHAT_ID,
        "text": text[:4000],
        "disable_web_page_preview": True,
        "disable_notification": quiet,
    }
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    try:
        resp = requests.post(_url("sendMessage"), json=payload, timeout=30)
    except requests.RequestException as exc:
        log.error("send failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.error("send %s: %s", resp.status_code, resp.text[:300])
        return None
    try:
        return int((resp.json().get("result") or {}).get("message_id"))
    except Exception:
        return None


def send_document(filename: str, content: str, caption: str) -> int | None:
    if not configured():
        log.warning("telegram not configured; skip document")
        return None
    files = {"document": (filename, content.encode("utf-8"), "text/plain")}
    data = {
        "chat_id": C.TELEGRAM_CHAT_ID,
        "caption": caption[:1024],
    }
    try:
        resp = requests.post(_url("sendDocument"), data=data, files=files, timeout=60)
    except requests.RequestException as exc:
        log.error("sendDocument failed: %s", exc)
        return None
    if resp.status_code != 200:
        log.error("sendDocument %s: %s", resp.status_code, resp.text[:300])
        return None
    try:
        return int((resp.json().get("result") or {}).get("message_id"))
    except Exception:
        return None


def download_file(file_id: str) -> str | None:
    try:
        meta = requests.get(_url("getFile"), params={"file_id": file_id}, timeout=30)
        path = ((meta.json() or {}).get("result") or {}).get("file_path")
        if not path:
            return None
        raw = requests.get(
            f"{API}/file/bot{C.TELEGRAM_TOKEN}/{path}",
            timeout=60,
        )
        raw.raise_for_status()
        return raw.content.decode("utf-8", errors="replace")
    except Exception as exc:
        log.error("download_file failed: %s", exc)
        return None


def register(command: str, handler: Callable[[list[str], dict], str]) -> None:
    _handlers[command.lower().lstrip("/")] = handler


def on_json(handler: Callable[[str, dict], str]) -> None:
    global _json_handler
    _json_handler = handler


def _allowed(chat_id) -> bool:
    return str(chat_id) == str(C.TELEGRAM_CHAT_ID)


def _extract_payload(msg: dict) -> str | None:
    text = (msg.get("text") or msg.get("caption") or "").strip()
    doc = msg.get("document")
    if doc and doc.get("file_id"):
        body = download_file(doc["file_id"])
        if body:
            return body
    if text:
        return text
    return None


def _dispatch_command(raw: str, msg: dict) -> str | None:
    global _incomplete_notified
    parts = raw.split()
    cmd = parts[0].split("@")[0].lower().lstrip("/")
    if cmd in ("start", "help"):
        names = ", ".join(f"/{n}" for n in sorted(_handlers))
        return (
            f"gap bot commands: {names}\n"
            "/gap_clear drops a half-pasted JSON buffer.\n"
            "Paste Grok's full answer. Telegram splits are repaired."
        )
    if cmd == "gap_clear":
        n = len(_pending)
        _pending.clear()
        _incomplete_notified = False
        return f"cleared {n} buffered message(s). paste Grok's answer again."
    handler = _handlers.get(cmd)
    if handler:
        try:
            return handler(parts[1:], msg)
        except Exception as exc:
            log.exception("handler /%s", cmd)
            return f"error: {exc}"
    return None


def _flush_pending() -> None:
    """Silence, then join every part, extract the JSON object, book."""
    global _incomplete_notified
    if not _pending or _json_handler is None:
        return

    parts = list(_pending)
    raw_parts = [p[2] for p in parts if p[2]]
    last_msg = parts[-1][1]
    n = len(parts)

    from . import parser

    blobs = [
        "".join(raw_parts),
        "\n".join(raw_parts),
    ]
    extracted = None
    last_exc: Exception | None = None
    for blob in blobs:
        try:
            parser.load_json(blob)
            extracted = blob
            break
        except Exception as exc:
            last_exc = exc

    if extracted is None:
        if not _incomplete_notified:
            send(
                f"got {n} message(s), {sum(len(p) for p in raw_parts)} chars — "
                f"still assembling ({last_exc}). send the rest."
            )
            _incomplete_notified = True
        return

    _pending.clear()
    _incomplete_notified = False

    send(
        f"JSON extracted from {n} message(s) ({len(extracted)} chars). "
        "Scoring gaps and booking A/B/C/D…"
    )
    try:
        result = _json_handler(extracted, last_msg)
    except Exception as exc:
        log.exception("json handler")
        send(f"parse/book error: {exc}")
        return
    if result:
        send(result)


def _listen() -> None:
    global _incomplete_notified
    from . import store

    offset = 0
    saved = store.get_state("telegram_offset")
    if isinstance(saved, int):
        offset = saved
    elif isinstance(saved, dict) and "offset" in saved:
        try:
            offset = int(saved["offset"])
        except (TypeError, ValueError):
            offset = 0

    while True:
        try:
            long_poll = 1 if _pending else 50
            resp = requests.get(
                _url("getUpdates"),
                params={"timeout": long_poll, "offset": offset},
                timeout=long_poll + 15,
            )
            data = resp.json() if resp.status_code == 200 else {}
            now = time.monotonic()
            for upd in data.get("result") or []:
                offset = int(upd["update_id"]) + 1
                store.set_state("telegram_offset", offset)
                msg = upd.get("message") or upd.get("channel_post") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                if not _allowed(chat_id):
                    continue
                raw = (_extract_payload(msg) or "").strip()
                if not raw:
                    continue
                if raw.startswith("/"):
                    reply = _dispatch_command(raw, msg)
                    if reply:
                        send(reply, reply_to=msg.get("message_id"))
                    continue
                if msg.get("document"):
                    _pending.clear()
                    _incomplete_notified = False
                _pending.append((now, msg, raw))

            if _pending:
                age = time.monotonic() - _pending[-1][0]
                oldest = time.monotonic() - _pending[0][0]
                if oldest > STALE_SEC and age >= DEBOUNCE_SEC:
                    send(
                        f"dropped {len(_pending)} stale message(s) "
                        f"({int(oldest)}s old) — no complete JSON."
                    )
                    _pending.clear()
                    _incomplete_notified = False
                elif age >= DEBOUNCE_SEC:
                    _flush_pending()
        except Exception as exc:
            log.warning("listen loop: %s", exc)
            time.sleep(10)


def start_listener() -> None:
    global _listener_started
    if _listener_started or not C.TELEGRAM_COMMANDS or not configured():
        return
    t = threading.Thread(target=_listen, name="gap-telegram", daemon=True)
    t.start()
    _listener_started = True
    log.info("telegram listener started (debounce %.1ss)", DEBOUNCE_SEC)
