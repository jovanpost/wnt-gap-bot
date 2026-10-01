"""Other networks and wires, fetched ONCE per Grok file (v1.9.1). Spec tested by Jovan with Grok, Oct 1.

Rules (from that spec):
  - fetch once per run, never per word; 8-second timeout per feed;
  - keep the first 20 items of each feed;
  - drop a feed that is non-200, empty, or whose NEWEST item is older than 36 hours
    (e.g. the Yahoo feed's newest item was Sep 22 when checked on Oct 1 -- it drops itself);
  - a title is a hit only if the contract word is IN THE TITLE (no summary matches here);
  - this does NOT replace the per-word Google News search, which still runs.
Reuters and AP have no public RSS, so they come through Google News site: searches.
Google News US top stories is fetched once as homepage context.

Never added (Jovan's do-not-add list): CBS latest/rss (empty), Politico politics-news.xml (stale mix),
Flipboard Reuters (frozen 2021), Bluesky profiles, RSS.app, Nitter.
Safe by design: shared per-site speed limit (netlimit), one total time budget, 5-minute cache,
any failure only shrinks this block. The Grok file always goes out.
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
from .headlines import body_of, search_terms

log = logging.getLogger("gap.morefeeds")
UA = "Mozilla/5.0 (compatible; wnt-gap-bot news-feeds)"

GN = "https://news.google.com/rss"
# key, label, url, kind ("net" = network feed, "wire" = wire via Google News, "home" = homepage context)
FEEDS = (
    ("nbc", "NBC", "https://feeds.nbcnews.com/nbcnews/public/news", "net"),
    ("npr", "NPR", "https://feeds.npr.org/1001/rss.xml", "net"),
    ("pbs", "PBS NewsHour", "https://www.pbs.org/newshour/feeds/rss/headlines", "net"),
    ("yahoo", "Yahoo", "https://news.yahoo.com/rss", "net"),
    ("hill", "The Hill", "https://thehill.com/feed/", "net"),
    ("axios", "Axios", "https://api.axios.com/feed/", "net"),
    ("ap", "AP (via Google News)", f"{GN}/search?q=when:1d+site:apnews.com&hl=en-US&gl=US&ceid=US:en", "wire"),
    ("reuters", "Reuters (via Google News)", f"{GN}/search?q=when:1d+site:reuters.com&hl=en-US&gl=US&ceid=US:en", "wire"),
    ("gtop", "Google News US top stories", f"{GN}?hl=en-US&gl=US&ceid=US:en", "home"),
)
ATOM = "{http://www.w3.org/2005/Atom}"
DC = "{http://purl.org/dc/elements/1.1/}"

_cache: dict[str, tuple[float, tuple]] = {}
_cache_lock = threading.Lock()


def _when(raw: str | None) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    try:
        d = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def parse(xml_text: str, limit: int, strip_source: bool = False) -> list[dict]:
    """RSS 2.0 or Atom -> [{rank, title, source, published}] in the feed's own order, first `limit` items.
    strip_source: Google News titles end with ' - Source'; that is moved into 'source'."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    nodes = list(root.iter("item")) or list(root.iter(f"{ATOM}entry"))
    out = []
    for n in nodes[:limit]:
        title = (n.findtext("title") or n.findtext(f"{ATOM}title") or "").strip()
        title = re.sub(r"\s+", " ", title)
        if not title:
            continue
        src = (n.findtext("source") or "").strip()
        if strip_source:
            if src and title.endswith(" - " + src):
                title = title[: -(len(src) + 3)].strip()
            elif " - " in title and not src:
                title, src = title.rsplit(" - ", 1)
        pub = _when(n.findtext("pubDate") or n.findtext(f"{DC}date") or n.findtext(f"{ATOM}updated")
                    or n.findtext(f"{ATOM}published"))
        out.append({"rank": len(out) + 1, "title": title, "source": src, "published": pub})
    return out


def check(items: list[dict], now: datetime | None = None) -> str | None:
    """Why a feed is dropped, or None if it is usable."""
    if not items:
        return "empty"
    dated = [i["published"] for i in items if i.get("published")]
    if dated:
        now = now or datetime.now(timezone.utc)
        if max(dated) < now - timedelta(hours=C.MORE_FEEDS_MAX_AGE_H):
            return f"stale (newest {max(dated).astimezone(C.CT):%b %d})"
    return None


def fetch_feed(key: str, url: str, kind: str, deadline: float | None = None) -> tuple[list[dict], str | None]:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < C.MORE_FEEDS_CACHE_S:
            return hit[1]
    try:
        with netlimit.ticket(url, deadline):
            r = requests.get(url, timeout=C.MORE_FEEDS_TIMEOUT_S, headers={"User-Agent": UA}, allow_redirects=True)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}"
        items = parse(body_of(r), C.MORE_FEEDS_N, strip_source=(kind != "net"))
        why = check(items)
        out = ([], why) if why else (items, None)
    except TimeoutError:
        return [], "out of time"
    except requests.Timeout:
        return [], "timeout"
    except Exception as exc:  # noqa: BLE001
        return [], type(exc).__name__
    with _cache_lock:
        _cache[url] = (time.time(), out)
    return out


def fetch_all(fetcher=None) -> dict:
    """{key: (items, error)} for every feed, fetched in parallel inside MORE_FEEDS_BUDGET_S."""
    if not C.MORE_FEEDS_ON:
        return {}
    deadline = time.monotonic() + C.MORE_FEEDS_BUDGET_S
    get = fetcher or (lambda key, url, kind: fetch_feed(key, url, kind, deadline))
    feeds = [f for f in FEEDS if f[0] not in C.MORE_FEEDS_SKIP]
    pool = ThreadPoolExecutor(max_workers=max(1, min(len(feeds), 6)))
    futs = {key: pool.submit(get, key, url, kind) for key, _l, url, kind in feeds}
    done, _ = wait(list(futs.values()), timeout=C.MORE_FEEDS_BUDGET_S + 1)
    pool.shutdown(wait=False, cancel_futures=True)
    out = {}
    for key, fut in futs.items():
        try:
            out[key] = fut.result() if fut in done else ([], "out of time")
        except Exception as exc:  # noqa: BLE001
            out[key] = ([], type(exc).__name__)
    return out


def title_hits(word: str, got: dict) -> list[tuple[str, str]]:
    """[(label, title)] for items whose TITLE has any side of the word (homepage list included).
    The same headline in two feeds is listed once."""
    from .abcfeeds import term_pattern
    pats = [term_pattern(t) for t in search_terms(word)]
    labels = {k: l for k, l, _u, _kind in FEEDS}
    seen, out = set(), []
    for key, (items, _err) in got.items():
        for it in items:
            if any(p.search(it["title"]) for p in pats):
                norm = re.sub(r"[^a-z0-9]+", " ", it["title"].lower()).strip()
                if norm in seen:
                    continue
                seen.add(norm)
                out.append((labels.get(key, key), it["title"]))
    return out


def render(words: list[dict], got: dict) -> str:
    if not C.MORE_FEEDS_ON or not got:
        return ""
    labels = {k: l for k, l, _u, _kind in FEEDS}
    ok = [k for k, (items, err) in got.items() if items and not err]
    if not ok:
        return "\nOTHER NETWORKS AND WIRES: unavailable today. Do your own searches; nothing here means anything."
    now = datetime.now(timezone.utc).astimezone(C.CT).strftime("%-I:%M %p CT")
    used = ", ".join(labels[k] for k in ok)
    dropped = "; ".join(f"{labels.get(k, k)}: {err}" for k, (items, err) in got.items() if err)
    lines = [
        "",
        f"OTHER NETWORKS AND WIRES (fetched once by the bot at {now}; first {C.MORE_FEEDS_N} items per feed, last "
        f"{C.MORE_FEEDS_MAX_AGE_H}h).",
        f"Feeds used: {used}." + (f" Dropped: {dropped}." if dropped else ""),
        "A hit means the word is IN THE HEADLINE of that outlet's current feed. These show how widely a story is",
        "running beyond ABC; they do not replace the per-word Google News search below, and no hit is not proof of no story.",
        "",
        "HEADLINE HITS PER WORD:",
    ]
    n = C.MORE_FEEDS_MATCHES_PER_WORD
    for w in words:
        hits = title_hits(w["word"], got)
        if not hits:
            lines.append(f"- {w['word']}: none")
            continue
        outlets = sorted({lab for lab, _t in hits})
        shown = "; ".join(f"[{lab}] {t}" for lab, t in hits[:n])
        more = f" (+{len(hits) - n} more)" if len(hits) > n else ""
        lines.append(f"- {w['word']}: {len(hits)} headline(s) in {len(outlets)} outlet(s): {shown}{more}")
    home = got.get("gtop", ([], None))[0]
    if home:
        lines += ["", "GOOGLE NEWS US TOP STORIES (homepage context, once; not a search of the word list):"]
        lines += [f"  {it['rank']}. {it['title']}" + (f" — {it['source']}" if it.get("source") else "") for it in home]
    return "\n".join(lines)
