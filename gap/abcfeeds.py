"""ABC News' own RSS feeds, fetched once per Grok file and printed into it.

Why (v1.7.6): Google News search says a story EXISTS. ABC's own feeds say ABC has ALREADY
WRITTEN it -- the step Grok kept missing (Oct 1: the pardon item sat about 17th on ABC's Top
Stories while Grok scored "Pardon" 5). One pass per file (5-6 feeds), never one fetch per word.

What goes into the file:
  1. every title from each feed (Top 25, the others 15), numbered as ABC ranks them, and
  2. per word: which of those ABC titles/summaries use the word (or either side of a slash word).
A word missing from ABC's feeds is NOT dead -- the block says so, and Google News still runs.

Safe by design: shared per-site speed limit (netlimit), short timeouts, one total time budget,
a 5-minute cache (so /gap_resend does not hit ABC again), and any failure just shrinks the block.
The Grok file always goes out.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests

from . import config as C, netlimit
from .headlines import search_terms

log = logging.getLogger("gap.abcfeeds")

BASE = "https://feeds.abcnews.com/abcnews/"
UA = "Mozilla/5.0 (compatible; wnt-gap-bot abc-feeds)"

# key, label shown to Grok, feed name, how many items to read, when to fetch
FEEDS = (
    ("top", "Top Stories", "topstories", "ABC_TOP_N", "always"),
    ("us", "US", "usheadlines", "ABC_SECTION_N", "always"),
    ("politics", "Politics", "politicsheadlines", "ABC_SECTION_N", "always"),
    ("world", "World", "worldnewsheadlines", "ABC_SECTION_N", "always"),
    ("gma", "GMA", "gmaheadlines", "ABC_SECTION_N", "always"),
    ("health", "Health", "healthheadlines", "ABC_SECTION_N", "health_words"),
)

# Health feed only when tonight's list has a word like these (each side of a slash word checked).
HEALTH_HINTS = (
    "cancer", "snap", "food stamp", "medicaid", "medicare", "vaccine", "vaccin", "measles", "covid",
    "flu", "outbreak", "virus", "fda", "cdc", "drug", "opioid", "fentanyl", "overdose", "ozempic",
    "obesity", "hospital", "doctor", "nurse", "health", "rfk", "kennedy", "autism", "tylenol",
    "abortion", "heart", "alzheimer", "diabetes", "disease", "bird flu", "salmonella", "recall",
)

_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()


def feed_url(name: str) -> str:
    return BASE + name


def wanted_feeds(words: list[dict]) -> list[tuple]:
    terms = " | ".join(t.lower() for w in words for t in search_terms(w["word"]))
    health = any(h in terms for h in HEALTH_HINTS)
    out = []
    for key, label, name, n_attr, when in FEEDS:
        if key in C.ABC_SKIP_FEEDS:
            continue
        if when == "health_words" and not health:
            continue
        out.append((key, label, name, int(getattr(C, n_attr))))
    return out


def _text(el) -> str:
    return re.sub(r"\s+", " ", (el or "")).strip()


def parse_feed(xml_text: str, limit: int, now: datetime | None = None) -> list[dict]:
    """ABC RSS -> [{rank, title, summary, link, published}] in ABC's own order.
    Skips the always-on 'LIVE:' stream entries and anything older than ABC_MAX_AGE_H."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    now = now or datetime.now(timezone.utc)
    oldest = now - timedelta(hours=C.ABC_MAX_AGE_H)
    rank = 0
    for it in root.iter("item"):
        title = _text(it.findtext("title"))
        if not title:
            continue
        rank += 1                                  # ABC's own position, counting skipped items
        if rank > limit:
            break
        if title.upper().startswith("LIVE:"):
            continue
        pub = None
        raw = it.findtext("pubDate")
        if raw:
            try:
                pub = parsedate_to_datetime(raw.strip())
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub = None
        if pub is not None and pub < oldest:
            continue
        items.append({
            "rank": rank,
            "title": title,
            "summary": _text(it.findtext("description")),
            "link": _text(it.findtext("link")),
            "published": pub,
        })
    return items


def fetch_feed(name: str, limit: int, deadline: float | None = None) -> tuple[list[dict], str | None]:
    """(items, error). Uses the 5-minute cache and the shared per-site speed limit."""
    url = feed_url(name)
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < C.ABC_CACHE_S:
            return hit[1][:], None
    try:
        with netlimit.ticket(url, deadline):
            r = requests.get(url, timeout=C.ABC_TIMEOUT_S, headers={"User-Agent": UA}, allow_redirects=True)
        if r.status_code != 200:
            log.warning("abc feed %s: HTTP %s", name, r.status_code)
            return [], f"HTTP {r.status_code}"
        items = parse_feed(r.text, limit)
        with _cache_lock:
            _cache[url] = (time.time(), items)
        return items[:], None
    except TimeoutError:
        return [], "out of time"
    except requests.Timeout:
        log.warning("abc feed %s: timeout", name)
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001
        log.warning("abc feed %s: %s", name, exc)
        return [], type(exc).__name__


def term_pattern(term: str) -> re.Pattern:
    """Whole-word match. Short all-caps terms (AI, UN, ICE) must match in capitals so 'ai'
    inside normal text is not counted; longer terms ignore case and allow plural/possessive and -ed/-ing/-ian
    (Pardon -> pardoning, Iran -> Iranian)."""
    t = re.escape(term.strip()).replace(r"\ ", r"\s+")
    if len(term.strip()) <= 3 and term.strip().isupper():
        return re.compile(rf"\b{t}(?:s|'s|’s)?\b")
    return re.compile(rf"\b{t}(?:s|es|ed|ing|ian|ians|'s|’s)?\b", re.IGNORECASE)


def word_matches(word: str, feeds: dict[str, list[dict]], labels: dict[str, str]) -> list[tuple]:
    """[(label, rank, title)] for ABC items whose title or summary uses any side of the word.
    Same story in several feeds is listed once (first feed wins, by link or title)."""
    pats = [term_pattern(t) for t in search_terms(word)]
    seen: set[str] = set()
    out: list[tuple] = []
    for key, items in feeds.items():
        for it in items:
            hay = f"{it['title']} {it['summary']}"
            if any(p.search(hay) for p in pats):
                k = it["link"] or it["title"].lower()
                if k in seen:
                    continue
                seen.add(k)
                out.append((labels[key], it["rank"], it["title"]))
    return out


def abc_block(words: list[dict], fetcher=None) -> str:
    """The ABC text block for the Grok file. "" only when switched off."""
    if not C.ABC_FEEDS_ON:
        return ""
    feeds = wanted_feeds(words)
    deadline = time.monotonic() + C.ABC_BUDGET_S
    get = fetcher or (lambda name, n: fetch_feed(name, n, deadline))

    pool = ThreadPoolExecutor(max_workers=max(1, min(len(feeds), C.NET_MAX_PARALLEL)))
    futs = {key: pool.submit(get, name, n) for key, _label, name, n in feeds}
    done, _ = wait(list(futs.values()), timeout=C.ABC_BUDGET_S + 1)
    pool.shutdown(wait=False, cancel_futures=True)

    got: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for key, fut in futs.items():
        items, err = fut.result() if fut in done else ([], "out of time")
        if err:
            errors[key] = err
        got[key] = items
    labels = {key: label for key, label, _n, _c in feeds}

    if not any(got.values()):
        common = max(errors.values(), key=list(errors.values()).count) if errors else "empty"
        return (f"\nABC NEWS FEEDS: unavailable today ({common}). "
                "Run your from:ABC and ABC follow-up searches yourself; nothing here means anything about any word.")

    now = datetime.now(timezone.utc).astimezone(C.CT).strftime("%-I:%M %p CT")
    lines = [
        "",
        f"ABC NEWS FEEDS (ABC's own RSS, fetched by the bot at {now}; numbered in ABC's order, last {C.ABC_MAX_AGE_H}h).",
        "A word in these titles means ABC has ALREADY WRITTEN that story today: treat it as a strong candidate for tonight.",
        "A word NOT in these feeds is NOT dead: the feeds are short, and Google News and your own searches still count.",
    ]
    lines.append("")
    lines.append("ABC MATCHES PER WORD:")
    for w in words:
        hits = word_matches(w["word"], got, labels)
        if not hits:
            lines.append(f"- {w['word']}: none in ABC feeds")
            continue
        shown = "; ".join(f"[{lab} #{rank}] {title}" for lab, rank, title in hits[: C.ABC_MATCHES_PER_WORD])
        more = f" (+{len(hits) - C.ABC_MATCHES_PER_WORD} more)" if len(hits) > C.ABC_MATCHES_PER_WORD else ""
        lines.append(f"- {w['word']}: {len(hits)} ABC item(s): {shown}{more}")
    for key, label, _name, _n in feeds:
        lines.append("")
        if key in errors:
            lines.append(f"ABC {label}: (not fetched: {errors[key]})")
            continue
        lines.append(f"ABC {label}:")
        for it in got[key]:
            lines.append(f"  {it['rank']}. {it['title']}")
    return "\n".join(lines)
