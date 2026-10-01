"""Google News headlines for every word, fetched by the bot and printed into the Grok file.

Why (Oct 1): Grok's own web search for "pardon" returned a Nigerian governor's clemency and its
X search on "Latest" returned idioms, so it scored the word 5% while ABC, NBC and the NY Post
all had Trump's pardon interview at the top of Google News. The bot now reads Google News'
search RSS for each word (each side of a slash word separately) and puts the top titles in
front of Grok, so the obvious story can never be missed again.

Safe by design: a slow or failed fetch is skipped (short timeout), and the block is only extra
context -- the Grok file always goes out, with or without it.
"""
from __future__ import annotations

import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

from concurrent.futures import ThreadPoolExecutor, wait

import requests

from . import config as C, netlimit

log = logging.getLogger("gap.headlines")

RSS_SEARCH = "https://news.google.com/rss/search?q={q}&hl=en-US&gl=US&ceid=US:en"
UA = "Mozilla/5.0 (compatible; wnt-gap-bot headlines)"


def body_of(r):
    """The raw bytes of a feed answer, so the XML's own encoding line decides how letters are read.
    v1.9.2: r.text guessed Latin-1 for feeds without a charset header, turning he'd into heâ\x80\x99d."""
    raw = getattr(r, "content", None)
    return raw if isinstance(raw, (bytes, bytearray)) and raw else r.text


def search_terms(word: str) -> list[str]:
    """'Trump (5+ times)' -> ['Trump']; 'SpaceX / NASA' -> ['SpaceX', 'NASA']."""
    base = re.sub(r"\([^)]*\)", " ", word or "")
    parts = [re.sub(r"\s+", " ", p).strip(" .,-") for p in base.split("/")]
    out: list[str] = []
    for p in parts:
        if p and p.lower() not in {x.lower() for x in out}:
            out.append(p)
    return out


def parse_rss(xml_text: str, limit: int) -> list[dict]:
    """Google News RSS -> [{title, source, published}] (newest feed order kept)."""
    items: list[dict] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return items
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        source = (it.findtext("source") or "").strip()
        if source and title.endswith(" - " + source):
            title = title[: -(len(source) + 3)].strip()
        pub = None
        raw = it.findtext("pubDate")
        if raw:
            try:
                pub = parsedate_to_datetime(raw)
                if pub.tzinfo is None:
                    pub = pub.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pub = None
        if title:
            items.append({"title": title, "source": source, "published": pub})
        if len(items) >= limit:
            break
    return items


def fetch_one(term: str, limit: int | None = None, deadline: float | None = None) -> tuple[list[dict], str | None]:
    """(items, error). error is None on success, else a short reason like 'HTTP 429' or 'timeout'.
    v1.7.6: every request takes a ticket from the shared per-site speed limit first (netlimit)."""
    limit = limit or C.HEADLINES_PER_TERM
    q = quote_plus(f'"{term}" when:1d')
    url = RSS_SEARCH.format(q=q)
    try:
        with netlimit.ticket(url, deadline):
            r = requests.get(url, timeout=C.HEADLINES_TIMEOUT_S, headers={"User-Agent": UA})
        if r.status_code != 200:
            log.warning("headlines %s: HTTP %s", term, r.status_code)
            return [], f"HTTP {r.status_code}"
        return parse_rss(body_of(r), limit), None
    except TimeoutError:
        return [], "out of time"
    except requests.Timeout:
        log.warning("headlines %s: timeout", term)
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001
        log.warning("headlines %s: %s", term, exc)
        return [], type(exc).__name__


def fetch(term: str, limit: int | None = None) -> list[dict]:
    return fetch_one(term, limit)[0]


def _ct(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(C.CT).strftime("%b %d %-I:%M %p CT")


def fetch_all(words: list[dict], fetcher=None) -> tuple[list[tuple], dict]:
    """Fetch every term in parallel (HEADLINES_WORKERS at a time, spacing by netlimit) inside one
    HEADLINES_BUDGET_S time limit. Returns (jobs, results): jobs = [(word, term)], results =
    {term: (items, error)}. v1.7.7: split from the text so the ABC block can reuse ABC-sourced items."""
    jobs = [(w["word"], term) for w in words for term in search_terms(w["word"])]
    if not C.HEADLINES_ON:
        return jobs, {}
    deadline = time.monotonic() + C.HEADLINES_BUDGET_S

    def run(term: str):
        if fetcher is None:
            return fetch_one(term, C.HEADLINES_PER_TERM, deadline)
        out = fetcher(term)
        return (out, None) if isinstance(out, list) else out

    results: dict = {}
    pool = ThreadPoolExecutor(max_workers=max(1, C.HEADLINES_WORKERS))
    futures = {}
    for _word, term in jobs:
        if term not in futures:
            futures[term] = pool.submit(run, term)  # spacing between starts is done by netlimit
    done, _not_done = wait(list(futures.values()), timeout=C.HEADLINES_BUDGET_S)
    pool.shutdown(wait=False, cancel_futures=True)
    for term, fut in futures.items():
        try:
            results[term] = fut.result() if fut in done else ([], "out of time")
        except Exception as exc:  # noqa: BLE001
            results[term] = ([], type(exc).__name__)
    return jobs, results


def render(jobs: list[tuple], results: dict) -> str:
    """The Google News text block. "" when headlines are off or nothing came back."""
    if not C.HEADLINES_ON:
        return ""
    now = datetime.now(timezone.utc).astimezone(C.CT).strftime("%-I:%M %p CT")
    lines = [
        "",
        f"GOOGLE NEWS HEADLINES (fetched by the bot at {now}; Google News search, last 24 hours, raw).",
        "Use these as part of your blind pass. They can include foreign or unrelated uses of a word; judge each one.",
        "They do not replace your own searches, and an empty list is not proof that a word has no story.",
    ]
    got_any = False
    errors: list[str] = []
    for word, term in jobs:
        items, err = results.get(term, ([], "missing"))
        label = word if term == word else f"{word} -> {term}"
        if err:
            errors.append(err)
            lines.append(f"- {label}: (not fetched: {err})")
            continue
        if not items:
            lines.append(f"- {label}: (no headlines in the last 24 hours)")
            continue
        got_any = True
        lines.append(f"- {label}:")
        for it in items:
            src = f" — {it['source']}" if it.get("source") else ""
            when = f" ({_ct(it.get('published'))})" if it.get("published") else ""
            lines.append(f"    • {it['title']}{src}{when}")
    if not got_any and errors:
        common = max(set(errors), key=errors.count)
        return (f"\nGOOGLE NEWS HEADLINES: unavailable today ({common}). "
                "Do every search step yourself; nothing here means anything about any word.")
    return "\n".join(lines) if got_any else ""


def headlines_block(words: list[dict], fetcher=None) -> str:
    """Fetch + text in one call (kept for older callers and tests)."""
    if not C.HEADLINES_ON:
        return ""
    return render(*fetch_all(words, fetcher))


def _staggered(fn, term: str, delay: float):
    if delay > 0:
        time.sleep(delay)
    return fn(term)
