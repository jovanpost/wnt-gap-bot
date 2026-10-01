"""One shared speed limit for the bot's outside news fetches (Google News, ABC feeds).

Why (v1.7.6): the Grok file now fetches from two news sites at once. Without a limit, the
worker threads could all fire in the same millisecond. Here every request must first take a
"ticket" for its website. Tickets for the same website are handed out at least
NET_MIN_GAP_S apart, and at most NET_MAX_PARALLEL requests per website run at the same time.
Different websites do not wait for each other.

Thread-safe: the poll loop and Telegram commands can build files at the same time.
"""
from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from urllib.parse import urlparse

from . import config as C

_lock = threading.Lock()
_next_at: dict[str, float] = {}           # host -> earliest time the next request may start
_slots: dict[str, threading.BoundedSemaphore] = {}
_stats = {"requests": 0, "waited_s": 0.0}


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _slot(host: str) -> threading.BoundedSemaphore:
    with _lock:
        s = _slots.get(host)
        if s is None:
            s = threading.BoundedSemaphore(max(1, C.NET_MAX_PARALLEL))
            _slots[host] = s
        return s


def reserve(host: str, now: float | None = None) -> float:
    """Book the next start time for this host and return how long the caller must wait."""
    gap = max(0.0, C.NET_MIN_GAP_S)
    with _lock:
        t = time.monotonic() if now is None else now
        start = max(t, _next_at.get(host, 0.0))
        _next_at[host] = start + gap
        _stats["requests"] += 1
        _stats["waited_s"] += start - t
        return start - t


@contextmanager
def ticket(url: str, deadline: float | None = None):
    """Wait for a slot and a start time for this URL's website, then run the body.
    If waiting would pass `deadline` (time.monotonic()), raise TimeoutError instead."""
    host = host_of(url)
    slot = _slot(host)
    left = None if deadline is None else deadline - time.monotonic()
    if left is not None and left <= 0:
        raise TimeoutError("out of time")
    if not slot.acquire(timeout=left):  # left=None -> wait as long as needed
        raise TimeoutError("out of time")
    try:
        wait = reserve(host)
        if deadline is not None and time.monotonic() + wait > deadline:
            raise TimeoutError("out of time")
        if wait > 0:
            time.sleep(wait)
        yield
    finally:
        slot.release()


def stats() -> dict:
    with _lock:
        return dict(_stats)


def reset() -> None:
    """Tests only."""
    with _lock:
        _next_at.clear()
        _slots.clear()
        _stats.update(requests=0, waited_s=0.0)
