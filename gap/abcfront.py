"""ABC's own front page and video page as two more "feeds" (v1.14.0).

Why: ABC's section RSS feeds lag its homepage. On Oct 2, 2026 the homepage carried "Tony Romo parting
ways with CBS Sports..." and "Amazon to invest $1B into data center communities" while the RSS block
said "none in ABC feeds" for both words. A model without search then cannot know ABC has the story.

What it does: one plain GET of https://abcnews.com/ and one of https://abcnews.com/video per run
(same 5-minute cache and per-site speed limit as the RSS feeds), then the headlines are read out of
the HTML. No browser, no JavaScript. Three ways are tried, most reliable first, and merged:

  1. story links: every <a> whose address looks like an ABC story or video
     (/Section/slug/story?id=123, /Section/wireStory/slug-123, /video/123). The title is the link's
     aria-label, else its visible text, else the words of the address itself.
  2. aria-label texts anywhere on the page (the way ABC labels its story cards).
  3. "headline": "..." values inside the page's embedded data.

The page layout is ABC's and can change without notice. When nothing can be read the feed comes back
empty and the Grok file says so ("ABC feeds empty right now (dropped): Front Page"); nothing else breaks.
Read-only. Never touches orders.
"""
from __future__ import annotations

import json
import logging
import re
from html import unescape

import requests

from . import config as C, netlimit
from .headlines import body_of

log = logging.getLogger("gap.abcfront")

PAGES = {"home": "https://abcnews.com/", "video": "https://abcnews.com/video"}
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0.0.0 Safari/537.36")
last_stats: dict[str, dict] = {}          # for the preview script: how each page was read

_A = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.S | re.I)
_HREF = re.compile(r'href\s*=\s*"([^"]+)"', re.I)
_ARIA = re.compile(r'aria-label\s*=\s*"([^"]{8,260})"', re.I)
_TAG = re.compile(r"<[^>]+>")
_STORY = re.compile(
    r"^(?:https?://(?:www\.)?abcnews\.(?:go\.)?com)?"
    r"(/(?:[A-Za-z0-9_]+/)*(?:[\w%.'-]+/story\?id=\d+|wireStory/[\w%.'-]+-\d+|video/[\w%.'-]*\d{5,}/?|live-updates/[\w%.'/-]+))", re.I)
_ID = re.compile(r"(\d{6,})")
_HEADLINE_JSON = re.compile(r'"(?:headline|title)"\s*:\s*"((?:[^"\\]|\\.){12,240})"')
_STAMP = re.compile(r"\s+\d{4}-\d{2}-\d{2}T[\d:.]+Z$")
SKIP_ANY = ("dropdown", "open profile", "privacy", "terms of use", "advertisement", "interest-based ads",
            "newsletter", "sign in", "log in", "cookie")            # anywhere in the label: page furniture
SKIP_START = ("previous", "next", "share", "menu", "search", "close", "play", "pause", "more from", "see all",
              "contact us", "live tv", "shop", "watch live", "skip to", "abc news", "24/7")
MIN_LINKS = 5                                 # fewer story links than this = ABC changed its layout: use the fallbacks


_DURATION = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?\s+")
_AGE = re.compile(r"\s+((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* \d{1,2})(?:, \d{4})?$|"
                  r"\s+((?:\d+|an?) (?:minute|hour|day)s? ago)$")


def _clean(text: str) -> str:
    """Visible text of a link: tags out, spaces joined, a video's length cut ("1:12 Judge ..."), and its
    age kept in brackets at the end ("... [Oct 01]", "... [2 hours ago]") so an old video reads as old."""
    t = unescape(_TAG.sub(" ", text or ""))
    t = _STAMP.sub("", re.sub(r"\s+", " ", t)).strip(" •|-–—")
    t = _DURATION.sub("", t)
    m = _AGE.search(t)
    if m:
        t = f"{t[:m.start()].rstrip()} [{m.group(1) or m.group(2)}]"
    return t


def _ok_title(t: str) -> bool:
    low = t.lower()
    if not (12 <= len(t) <= 220) or len(t.split()) < 3:
        return False
    if any(x in low for x in SKIP_ANY):
        return False
    return not any(low == x or low.startswith(x + " ") or low.startswith(x + ":") for x in SKIP_START)


def _slug_title(path: str) -> str:
    m = re.search(r"/([\w%.'-]+)/story\?id=\d+|/wireStory/([\w%.'-]+)-\d+|/video/([\w%.'-]*?)-?\d{5,}", path)
    slug = next((g for g in (m.groups() if m else ()) if g), "")
    return re.sub(r"[-_]+", " ", slug).strip()


def parse_page(html: str, limit: int, only_video: bool = False) -> tuple[list[dict], dict]:
    """(items, stats). items = [{rank, title, summary, link, published}] in page order.
    only_video: keep video links only (the video page repeats the homepage's top stories above its own list)."""
    if isinstance(html, (bytes, bytearray)):
        html = html.decode("utf-8", "replace")
    items: list[dict] = []
    by_id: dict[str, dict] = {}
    seen_titles: set[str] = set()
    stats = {"links": 0, "aria": 0, "embedded": 0}

    def add(title: str, link: str, how: str, sid: str | None = None) -> None:
        key = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()
        if not key or key in seen_titles:
            return
        seen_titles.add(key)
        it = {"rank": len(items) + 1, "title": title, "summary": "", "link": link, "published": None}
        items.append(it)
        if sid:
            by_id[sid] = it
        stats[how] += 1

    for attrs, inner in _A.findall(html or ""):
        h = _HREF.search(attrs)
        m = _STORY.match(unescape(h.group(1))) if h else None
        if not m:
            continue
        path = m.group(1)
        if only_video and "/video/" not in path.lower():
            continue
        idm = _ID.search(path)
        sid = idm.group(1) if idm else path
        aria = _ARIA.search(attrs)
        title = _clean(aria.group(1)) if aria else ""
        if not _ok_title(title):
            title = _clean(inner)
        slug = False
        if not _ok_title(title):
            title, slug = _slug_title(path), True
            if len(title.split()) < 3:
                continue
        old = by_id.get(sid)
        if old is not None:                       # same story linked twice (picture + headline): keep the real title
            if old.get("_slug") and not slug:
                seen_titles.add(re.sub(r"[^a-z0-9]+", " ", title.lower()).strip())
                old["title"], old["_slug"] = title, False
            continue
        add(title, "https://abcnews.com" + path, "links", sid)
        items[-1]["_slug"] = slug

    if len(items) < MIN_LINKS:                    # layout changed: fall back to every labelled card on the page
        for raw in _ARIA.findall(html or ""):
            t = _clean(raw)
            if _ok_title(t):
                add(t, "", "aria")
    if len(items) < MIN_LINKS:
        for raw in _HEADLINE_JSON.findall(html or ""):
            try:
                t = _clean(json.loads('"' + raw + '"'))
            except ValueError:
                continue
            if _ok_title(t):
                add(t, "", "embedded")

    out = []
    for it in items[:limit]:
        it.pop("_slug", None)
        out.append(it)
    stats["kept"] = len(out)
    return out, stats


def fetch_page(which: str, limit: int, deadline: float | None = None) -> tuple[list[dict], str | None]:
    """(items, error) for 'home' or 'video'. One plain GET; no browser."""
    url = PAGES[which]
    try:
        with netlimit.ticket(url, deadline):
            r = requests.get(url, timeout=C.ABC_FRONT_TIMEOUT_S, allow_redirects=True,
                             headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml",
                                      "Accept-Language": "en-US,en;q=0.9"})
        if r.status_code != 200:
            log.warning("abc %s page: HTTP %s", which, r.status_code)
            last_stats[which] = {"error": f"HTTP {r.status_code}"}
            return [], f"HTTP {r.status_code}"
        body = body_of(r)
        items, stats = parse_page(body, limit, only_video=(which == "video"))
        stats["bytes"] = len(body)
        last_stats[which] = stats
        return items, None
    except TimeoutError:
        return [], "out of time"
    except requests.Timeout:
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001
        log.warning("abc %s page: %s", which, exc)
        last_stats[which] = {"error": type(exc).__name__}
        return [], type(exc).__name__
