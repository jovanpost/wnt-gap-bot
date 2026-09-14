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

_handlers: dict[str, Callable[[list[str], dict], str]] = {}
_json_handler: Callable[[str, dict], str] | None = None
_listener_started = False


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


def _looks_like_json(text: str) -> bool:
    t = text.strip()
    if t.startswith("/"):
        cmd = t.split()[0].split("@")[0].lower()
        if cmd in ("/gap_json", "/json"):
            return True
    if t.startswith("{") or t.startswith("```"):
        return True
    return False


def _dispatch(msg: dict) -> str | None:
    raw = _extract_payload(msg) or ""
    if raw.startswith("/"):
        parts = raw.split()
        cmd = parts[0].split("@")[0].lower().lstrip("/")
        if cmd in ("start", "help"):
            names = ", ".join(f"/{n}" for n in sorted(_handlers))
            return f"gap bot commands: {names}\nOr reply to the nightly file with Grok's JSON."
        handler = _handlers.get(cmd)
        if handler:
            try:
                return handler(parts[1:], msg)
            except Exception as exc:
                log.exception("handler /%s", cmd)
                return f"error: {exc}"
        return None

    if _json_handler and _looks_like_json(raw):
        body = raw
        if body.lower().startswith("/gap_json") or body.lower().startswith("/json"):
            body = body.split("\n", 1)[1] if "\n" in body else ""
        try:
            return _json_handler(body, msg)
        except Exception as exc:
            log.exception("json handler")
            return f"parse error: {exc}"
    return None


def _listen() -> None:
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

    # Drain backlog once so a restart does not replay old JSON.
    try:
        primed = requests.get(
            _url("getUpdates"),
            params={"timeout": 0, "offset": -1},
            timeout=20,
        ).json()
        results = primed.get("result") or []
        if results:
            offset = int(results[-1]["update_id"]) + 1
            store.set_state("telegram_offset", offset)
    except Exception:
        pass

    while True:
        try:
            resp = requests.get(
                _url("getUpdates"),
                params={"timeout": 50, "offset": offset},
                timeout=60,
            )
            data = resp.json() if resp.status_code == 200 else {}
            for upd in data.get("result") or []:
                offset = int(upd["update_id"]) + 1
                store.set_state("telegram_offset", offset)
                msg = upd.get("message") or upd.get("channel_post") or {}
                chat_id = (msg.get("chat") or {}).get("id")
                if not _allowed(chat_id):
                    continue
                reply = _dispatch(msg)
                if reply:
                    send(reply, reply_to=msg.get("message_id"))
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
    log.info("telegram listener started")
