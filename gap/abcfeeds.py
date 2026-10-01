"""ABC News' own RSS feeds, fetched once per Grok file and printed into it.

Why (v1.7.6): Google News search says a story EXISTS. ABC's own feeds say ABC has ALREADY
WRITTEN it -- the step Grok kept missing (Oct 1: the pardon item sat about 17th on ABC's Top
Stories while Grok scored "Pardon" 5). One pass per file (5-6 feeds), never one fetch per word.

What goes into the file:
  1. every title from each feed (Top 25, the others 15), numbered as ABC ranks them, and
  2. per word: which of those ABC titles/summaries use the word (or either side of a slash word).
  3. (v1.7.7) Google News items whose source IS ABC News count as ABC hits too: ABC rotates its
     feeds during the day, so a story ABC wrote (Reflecting Pool, Oct 1) can be missing from them.
Each hit says where the word is: in the title, or only in the summary / article text.
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
    # v1.7.7: ABC's "worldnewsheadlines" and "gmaheadlines" feeds are empty at ABC itself (0 items
    # on Oct 1). "internationalheadlines" is ABC's live world feed (25 items). GMA dropped.
    ("intl", "International", "internationalheadlines", "ABC_SECTION_N", "always"),
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
    inside normal text is not counted; longer terms ignore case and allow plural/possessive and
    -ed/-ing/-ian (Pardon -> pardoning, Iran -> Iranian). v1.7.7: a term of 5+ letters also counts
    at the END of a joined word (Dubai -> FlyDubai), never in the middle of one."""
    raw = term.strip()
    t = re.escape(raw).replace(r"\ ", r"\s+")
    if len(raw) <= 3 and raw.isupper():
        return re.compile(rf"\b{t}(?:s|'s|’s)?\b")
    start = r"\b" if len(raw) < 5 else ""  # 5+ letters: may be the tail of a joined word
    return re.compile(rf"{start}{t}(?:s|es|ed|ing|ian|ians|'s|’s)?\b", re.IGNORECASE)


def _where(pats: list[re.Pattern], title: str, summary: str) -> str | None:
    """'title' if the word is in the title, 'summary' if only in the summary, None if neither."""
    if any(p.search(title) for p in pats):
        return "title"
    if any(p.search(summary or "") for p in pats):
        return "summary"
    return None


def _norm(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()


ABC_SOURCE_RE = re.compile(r"^(ABC News\b|Good Morning America\b|World News Tonight\b|ABCNews)", re.I)


def is_abc_source(source: str) -> bool:
    """Network ABC News only. Local affiliates (ABC7 Chicago, ABC7 Los Angeles) are NOT ABC News."""
    return bool(ABC_SOURCE_RE.match((source or "").strip()))


def google_abc_items(google_results: dict | None) -> dict[str, list[dict]]:
    """{term: [items whose Google News source is ABC News]} from headlines.fetch_all results."""
    out: dict[str, list[dict]] = {}
    for term, (items, _err) in (google_results or {}).items():
        abc = [it for it in (items or []) if is_abc_source(it.get("source", ""))]
        if abc:
            out[term] = abc
    return out


def word_matches(word: str, feeds: dict[str, list[dict]], labels: dict[str, str],
                 google_abc: dict[str, list[dict]] | None = None) -> list[tuple]:
    """[(where_label, title, in)] for ABC items using any side of the word. `in` is 'title' or
    'summary' (word only in the summary / article text). Same story listed once."""
    terms = search_terms(word)
    pats = [term_pattern(t) for t in terms]
    seen: set[str] = set()
    out: list[tuple] = []
    for key, items in feeds.items():
        for it in items:
            where = _where(pats, it["title"], it["summary"])
            if not where:
                continue
            k = _norm(it["title"])
            if k in seen or (it["link"] and it["link"] in seen):
                continue
            seen.update({k, it["link"]} - {""})
            out.append((f"{labels[key]} #{it['rank']}", it["title"], where))
    for term in terms:
        for it in (google_abc or {}).get(term, []):
            k = _norm(it["title"])
            if k in seen:
                continue
            seen.add(k)
            # Google searched this exact term, so the word is in the article even if not the title.
            where = "title" if _where(pats, it["title"], "") else "summary"
            out.append(("ABC via Google News", it["title"], where))
    # Title hits first: they are the clearest evidence.
    return sorted(out, key=lambda h: 0 if h[2] == "title" else 1)


def fetch_all(words: list[dict], fetcher=None) -> tuple[list[tuple], dict, dict]:
    """(feeds, got, errors). feeds = wanted_feeds(words); got = {key: items}; errors = {key: reason}."""
    feeds = wanted_feeds(words)
    if not C.ABC_FEEDS_ON or not feeds:
        return feeds, {}, {}
    deadline = time.monotonic() + C.ABC_BUDGET_S
    get = fetcher or (lambda name, n: fetch_feed(name, n, deadline))
    pool = ThreadPoolExecutor(max_workers=max(1, min(len(feeds), C.NET_MAX_PARALLEL)))
    futs = {key: pool.submit(get, name, n) for key, _label, name, n in feeds}
    done, _ = wait(list(futs.values()), timeout=C.ABC_BUDGET_S + 1)
    pool.shutdown(wait=False, cancel_futures=True)
    got: dict[str, list[dict]] = {}
    errors: dict[str, str] = {}
    for key, fut in futs.items():
        try:
            items, err = fut.result() if fut in done else ([], "out of time")
        except Exception as exc:  # noqa: BLE001
            items, err = [], type(exc).__name__
        if err:
            errors[key] = err
        got[key] = items
    return feeds, got, errors


def render(words: list[dict], feeds: list[tuple], got: dict, errors: dict,
           google_abc: dict[str, list[dict]] | None = None) -> str:
    """The ABC text block. "" only when switched off."""
    if not C.ABC_FEEDS_ON:
        return ""
    labels = {key: label for key, label, _n, _c in feeds}
    if not any(got.values()) and not google_abc:
        vals = list(errors.values())
        common = max(vals, key=vals.count) if vals else "empty"
        return (f"\nABC NEWS FEEDS: unavailable today ({common}). "
                "Run your from:ABC and ABC follow-up searches yourself; nothing here means anything about any word.")

    now = datetime.now(timezone.utc).astimezone(C.CT).strftime("%-I:%M %p CT")
    lines = [
        "",
        f"ABC NEWS FEEDS (ABC's own RSS, fetched by the bot at {now}; numbered in ABC's order, last {C.ABC_MAX_AGE_H}h;",
        "plus Google News items whose source is ABC News).",
        "A word in these items means ABC has ALREADY WRITTEN that story today: treat it as a strong candidate for tonight.",
        "[title] = the word is in the headline. [summary only] = the word is only in the summary or article text,",
        "so check that story is really about it. A word NOT found here is NOT dead: the feeds are short and change",
        "during the day, and Google News and your own searches still count.",
        "",
        "ABC MATCHES PER WORD:",
    ]
    n = C.ABC_MATCHES_PER_WORD
    for w in words:
        hits = word_matches(w["word"], got, labels, google_abc)
        if not hits:
            lines.append(f"- {w['word']}: none in ABC feeds")
            continue
        shown = "; ".join(
            f"[{where}{'' if tag == 'title' else ', summary only'}] {title}" for where, title, tag in hits[:n])
        more = f" (+{len(hits) - n} more)" if len(hits) > n else ""
        lines.append(f"- {w['word']}: {len(hits)} ABC item(s): {shown}{more}")
    for key, label, _name, _n in feeds:
        lines.append("")
        if key in errors:
            lines.append(f"ABC {label}: (not fetched: {errors[key]})")
            continue
        if not got.get(key):
            lines.append(f"ABC {label}: (empty right now)")
            continue
        lines.append(f"ABC {label}:")
        for it in got[key]:
            lines.append(f"  {it['rank']}. {it['title']}")
    return "\n".join(lines)


def abc_block(words: list[dict], fetcher=None, google_results: dict | None = None) -> str:
    """Fetch + text in one call. google_results = headlines.fetch_all(...)[1], optional."""
    if not C.ABC_FEEDS_ON:
        return ""
    feeds, got, errors = fetch_all(words, fetcher)
    return render(words, feeds, got, errors, google_abc_items(google_results))


_last_news: dict = {}
_last_news_lock = threading.Lock()


def _words_key(words: list[dict]) -> tuple:
    return tuple(sorted(w["word"] for w in words))


def news_data(words: list[dict], max_age_s: float | None = None) -> dict:
    """Raw news for these words: {"at", "google": (jobs, results) | None, "abc": (feeds, got, errors) | None}.
    Google News and ABC are fetched AT THE SAME TIME (separate sites, separate speed limits).
    v1.8.0: the last result is kept for NEWS_REUSE_S seconds, so the challenger forecasters
    (gap/shadow.py) use exactly the news the Grok file got, without fetching again."""
    from . import headlines

    max_age = C.NEWS_REUSE_S if max_age_s is None else max_age_s
    key = _words_key(words)
    with _last_news_lock:
        hit = _last_news.get(key)
        if hit and time.time() - hit["at"] <= max_age:
            return hit

    def safe(fn, *a):
        try:
            return fn(*a)
        except Exception:
            # Never let a news problem stop tonight's file. Grok still searches itself.
            log.exception("news fetch skipped: %s", getattr(fn, "__name__", fn))
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        g = pool.submit(safe, headlines.fetch_all, words)
        a = pool.submit(safe, fetch_all, words)
        out = {"at": time.time(), "google": g.result(), "abc": a.result()}
    with _last_news_lock:
        _last_news.clear()
        _last_news[key] = out
    return out


def blocks_from(words: list[dict], data: dict) -> tuple[str, str]:
    """(google_block, abc_block) text from news_data(). Any failure empties only its own block."""
    from . import headlines

    g_out, a_out = data.get("google"), data.get("abc")
    google_txt = ""
    if g_out is not None:
        try:
            google_txt = headlines.render(*g_out) or ""
        except Exception:
            log.exception("google render skipped")
    abc_txt = ""
    if C.ABC_FEEDS_ON:
        if a_out is None:
            fl = wanted_feeds(words)
            feeds, got, errors = fl, {}, {key: "error" for key, *_ in fl}
        else:
            feeds, got, errors = a_out
        gabc = google_abc_items(g_out[1]) if g_out is not None else {}
        try:
            abc_txt = render(words, feeds, got, errors, gabc) or ""
        except Exception:
            log.exception("abc render skipped")
    return google_txt, abc_txt


def news_blocks(words: list[dict]) -> tuple[str, str]:
    """(google_block, abc_block) for the Grok file: fetch (or reuse) the news, then render it."""
    return blocks_from(words, news_data(words, max_age_s=0))
