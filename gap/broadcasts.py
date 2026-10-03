"""PREVIOUS BROADCASTS block (v1.15.0): what actually aired on the last World News Tonight shows.

ABC uploads every full broadcast to YouTube and types the rundown into the video description:

    00:00 Intro
    02:33 Death row inmate Christa Pike in critical condition after surviving botched execution
    05:52 Witnesses chase suspect after fatal stabbing in New York City subway station
    ...

Grok on the web finds this by searching. A model without search never sees it, so it cannot know which
story led last night or how long each segment ran. This module reads the last BROADCASTS_N descriptions
through YouTube's official Data API (free key, works from a server; secret YOUTUBE_API_KEY) and turns
them into one text block for the nightly file: each segment in order, with its length.

No AI model is used. No video or audio is downloaded. The key travels in a request header, never in
an address, and nothing here prints or logs it. Without a key, or when YouTube cannot be reached, the
block is simply left out and the file is built as before. Read-only. Never touches orders.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from datetime import date, timedelta

import requests

from . import config as C, netlimit

log = logging.getLogger("gap.broadcasts")

API = "https://www.googleapis.com/youtube/v3"
QUERY = "World News Tonight with David Muir Full Broadcast"
TITLE_RE = re.compile(r"World News Tonight with David Muir Full Broadcast\s*[-–—:]\s*(.+?)\s*$", re.I)
DATE_RE = re.compile(r"([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})")
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
          "oct": 10, "nov": 11, "dec": 12}
STAMP_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s+(.+?)\s*$")
ISO_DUR_RE = re.compile(r"^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$")

last_error: dict = {"why": None}
_cache: dict[str, tuple[float, list[dict]]] = {}
_lock = threading.Lock()


def title_date(title: str) -> str | None:
    """'ABC World News Tonight with David Muir Full Broadcast - Oct. 1, 2026' -> '2026-10-01'."""
    m = TITLE_RE.search(title or "")
    d = DATE_RE.search(m.group(1)) if m else None
    if not d:
        return None
    month = MONTHS.get(d.group(1).lower()[:3])
    if not month:
        return None
    try:
        return date(int(d.group(3)), month, int(d.group(2))).isoformat()
    except ValueError:
        return None


def parse_description(desc: str) -> tuple[str, list[tuple[int, str]]]:
    """(lead paragraph, [(start second, segment title)]) from ABC's description. 'Intro' is kept:
    its end is where the first story starts."""
    lead, chapters = "", []
    for line in (desc or "").replace("\r\n", "\n").split("\n"):
        m = STAMP_RE.match(line)
        if m:
            a, b, c, title = m.groups()
            secs = (int(a) * 3600 + int(b) * 60 + int(c)) if c is not None else (int(a) * 60 + int(b))
            chapters.append((secs, re.sub(r"\s+", " ", title)))
        elif not chapters and not lead and len(line.strip()) > 60:
            lead = re.sub(r"\s+", " ", line.strip())
    return lead, chapters


def _seconds(iso: str) -> int | None:
    m = ISO_DUR_RE.match(iso or "")
    if not m:
        return None
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s


def _get(path: str, params: dict) -> dict:
    """One API call. The key is a header. Errors carry only the status and Google's short reason."""
    url = f"{API}/{path}"
    with netlimit.ticket(url):
        r = requests.get(url, params=params, timeout=C.BROADCASTS_TIMEOUT_S,
                         headers={"X-Goog-Api-Key": C.YOUTUBE_API_KEY, "Accept": "application/json"})
    if r.status_code != 200:
        try:
            why = ((r.json().get("error") or {}).get("errors") or [{}])[0].get("reason") or ""
        except Exception:  # noqa: BLE001
            why = ""
        raise RuntimeError(f"YouTube HTTP {r.status_code} {why}".strip())
    return r.json()


def _search(event_date: str, channel: str | None) -> list[dict]:
    after = (date.fromisoformat(event_date) - timedelta(days=12)).isoformat() + "T00:00:00Z"
    params = {"part": "snippet", "q": QUERY, "type": "video", "order": "date", "maxResults": 25, "publishedAfter": after}
    if channel:
        params["channelId"] = channel
    out = []
    for it in _get("search", params).get("items") or []:
        sn = it.get("snippet") or {}
        vid = (it.get("id") or {}).get("videoId")
        d = title_date(sn.get("title") or "")
        if not vid or not d or d >= event_date:            # only shows from BEFORE tonight
            continue
        if not channel and "abc news" not in (sn.get("channelTitle") or "").lower():
            continue                                        # without the channel filter: ABC's own uploads only
        out.append({"video_id": vid, "date": d, "title": sn.get("title")})
    best: dict[str, dict] = {}
    for b in out:                                           # one video per night
        best.setdefault(b["date"], b)
    return sorted(best.values(), key=lambda b: b["date"], reverse=True)


def fetch(event_date: str, n: int | None = None) -> list[dict]:
    """The last n broadcasts before event_date, newest first:
    [{"date", "title", "video_id", "lead", "segments": [{"title", "start", "seconds"}], "length"}].
    [] when there is no key, YouTube cannot be reached, or nothing is found. Never raises."""
    n = C.BROADCASTS_N if n is None else n
    if not C.BROADCASTS_ON or not C.YOUTUBE_API_KEY or n <= 0:
        last_error["why"] = "off" if not C.BROADCASTS_ON else ("no YOUTUBE_API_KEY" if not C.YOUTUBE_API_KEY else None)
        return []
    with _lock:
        hit = _cache.get(event_date)
        if hit and time.time() - hit[0] < C.BROADCASTS_CACHE_S:
            return hit[1][:n]
    try:
        found = _search(event_date, C.BROADCASTS_CHANNEL_ID or None)
        if not found and C.BROADCASTS_CHANNEL_ID:
            found = _search(event_date, None)               # the channel id changed: search by name, keep ABC News only
        found = found[:max(n, 1)]
        if not found:
            last_error["why"] = "no full broadcast found"
            return []
        js = _get("videos", {"part": "snippet,contentDetails", "id": ",".join(b["video_id"] for b in found)})
        by_id = {it.get("id"): it for it in js.get("items") or []}
        out = []
        for b in found:
            it = by_id.get(b["video_id"]) or {}
            lead, chapters = parse_description((it.get("snippet") or {}).get("description") or "")
            length = _seconds((it.get("contentDetails") or {}).get("duration") or "")
            segs = []
            for i, (start, title) in enumerate(chapters):
                end = chapters[i + 1][0] if i + 1 < len(chapters) else length
                segs.append({"title": title, "start": start,
                             "seconds": (end - start) if (end is not None and end > start) else None})
            out.append(dict(b, lead=lead, segments=segs, length=length))
        out = [b for b in out if b["segments"]]
        if out:
            with _lock:
                _cache.clear()
                _cache[event_date] = (time.time(), out)
        last_error["why"] = None if out else "the descriptions had no segment list"
        return out[:n]
    except Exception as exc:  # noqa: BLE001  never let this stop tonight's file
        last_error["why"] = str(exc)[:120] if isinstance(exc, RuntimeError) else type(exc).__name__
        log.warning("previous broadcasts skipped: %s", last_error["why"])
        return []


def _mmss(seconds: int | None) -> str:
    return "?" if seconds is None else f"{seconds // 60}:{seconds % 60:02d}"


def render(shows: list[dict]) -> str:
    if not shows:
        return ""
    lines = [
        "",
        f"PREVIOUS BROADCASTS (the last {len(shows)} World News Tonight show(s), from ABC's own segment list on YouTube;",
        "fetched by the bot). This is what ACTUALLY AIRED, in order, with the length of each segment in minutes:seconds.",
        "Long segments near the top were the leads; segments under 0:40 are index items. These are ABC's written",
        "labels, not the spoken words: a word can be said inside a segment whose label does not show it.",
    ]
    for s in shows:
        d = date.fromisoformat(s["date"])
        lines += ["", f"{d.strftime('%a %b')} {d.day}, {d.year}:"]
        n = 0
        for seg in s["segments"]:
            if seg["title"].strip().lower() in ("intro", "introduction"):
                lines.append(f"  (opening headlines: {_mmss(seg['seconds'])})")
                continue
            n += 1
            lines.append(f"  {n}. {seg['title']} ({_mmss(seg['seconds'])})")
    return "\n".join(lines)


def block(event_date: str) -> str:
    """The text block for the nightly file, or "" (no key, switched off, nothing found)."""
    return render(fetch(event_date))
