"""v1.7.6 tests: ABC feeds block, shared per-site speed limit, Google headlines through the
limit, and the Grok file builder. No network: every outside call is faked.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from gap import abcfeeds, config as C, headlines, netlimit, prompt


def _rss(items):
    body = []
    for title, desc, link, pub in items:
        body.append(
            f"<item><title><![CDATA[ {title}]]></title><link><![CDATA[{link}]]></link>"
            f"<pubDate>{format_datetime(pub)}</pubDate><description><![CDATA[{desc}]]></description>"
            f"<category>US</category></item>"
        )
    return ('<?xml version="1.0"?><rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">'
            "<channel><title>ABC News: Top Stories</title>" + "".join(body) + "</channel></rss>")


NOW = datetime.now(timezone.utc)
TOP = _rss(
    [("LIVE:  ABC News Live", "24/7", "https://abcnews.com/video/1/", NOW - timedelta(days=7))]
    + [(f"Filler story {i}", "nothing", f"https://abcnews.com/s/{i}", NOW) for i in range(2, 17)]
    + [("Trump says he would consider pardoning administration members: 'I'd do that'",
        "The president spoke in an interview.", "https://abcnews.com/s/pardon", NOW)]
    + [("Crew-13 launches to the space station", "SpaceX Falcon 9 lifted off.", "https://abcnews.com/s/crew", NOW)]
    + [("Very old story about Iraq", "", "https://abcnews.com/s/old", NOW - timedelta(days=5))]
    + [(f"Late filler {i}", "", f"https://abcnews.com/s/l{i}", NOW) for i in range(20, 30)]
)
WORLD = _rss([
    ("Iranian officials respond to new sanctions", "", "https://abcnews.com/s/iran", NOW),
    ("Crew-13 launches to the space station", "SpaceX Falcon 9 lifted off.", "https://abcnews.com/s/crew", NOW),
])


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    netlimit.reset()
    abcfeeds._cache.clear()
    monkeypatch.setattr(C, "ABC_FEEDS_ON", True)
    monkeypatch.setattr(C, "HEADLINES_ON", True)
    monkeypatch.setattr(C, "ABC_SKIP_FEEDS", set())
    yield
    netlimit.reset()


# ---------- parsing ----------

def test_parse_feed_rank_live_and_age():
    items = abcfeeds.parse_feed(TOP, 25)
    titles = [i["title"] for i in items]
    assert not any(t.startswith("LIVE:") for t in titles)          # live stream skipped
    assert "Very old story about Iraq" not in titles                # older than 36 h skipped
    pardon = next(i for i in items if "pardoning" in i["title"])
    assert pardon["rank"] == 17                                     # ABC's own position kept
    assert max(i["rank"] for i in items) <= 25                      # stops at the limit
    assert abcfeeds.parse_feed("not xml <<<", 25) == []


def test_term_matching_rules():
    p = abcfeeds.term_pattern
    assert p("Pardon").search("would consider pardoning members")
    assert p("Iran").search("Iranian officials")
    assert p("Food Stamp").search("cuts to food stamps")
    assert p("AI").search("New AI rules")
    assert not p("AI").search("officials said")                     # 'ai' inside a word never counts
    assert not p("AI").search("ai lowercase")                       # short caps terms need capitals
    assert not p("Steel").search("Steelers win")


def test_wanted_feeds_health_only_when_needed():
    keys = [f[0] for f in abcfeeds.wanted_feeds([{"word": "Pardon"}, {"word": "Iran / Iranian"}])]
    assert keys == ["top", "us", "politics", "intl"]
    keys = [f[0] for f in abcfeeds.wanted_feeds([{"word": "SNAP / Food Stamp"}])]
    assert "health" in keys
    keys = [f[0] for f in abcfeeds.wanted_feeds([{"word": "Cancer"}])]
    assert "health" in keys


# ---------- the block ----------

def _fake_fetch(name, n):
    xml = {"topstories": TOP, "internationalheadlines": WORLD}.get(name)
    if xml is None:
        return [], "HTTP 404"
    return abcfeeds.parse_feed(xml, n), None


def test_abc_block_matches_and_dedupes():
    words = [{"word": "Pardon"}, {"word": "SpaceX / NASA"}, {"word": "Iran / Iranian"}, {"word": "Hurricane"}]
    out = abcfeeds.abc_block(words, fetcher=_fake_fetch)
    assert "ABC NEWS FEEDS" in out
    assert "- Pardon: 1 ABC item(s): [Top Stories #17]" in out
    assert "- SpaceX / NASA: 1 ABC item(s)" in out                 # same story in 2 feeds counted once
    assert "- Iran / Iranian: 1 ABC item(s): [International #1]" in out
    assert "- Hurricane: none in ABC feeds" in out
    assert "ABC US: (not fetched: HTTP 404)" in out
    assert "  17. Trump says he would consider pardoning" in out


def test_abc_block_all_failed_and_off(monkeypatch):
    out = abcfeeds.abc_block([{"word": "Pardon"}], fetcher=lambda name, n: ([], "HTTP 429"))
    assert "ABC NEWS FEEDS: unavailable today (HTTP 429)" in out
    monkeypatch.setattr(C, "ABC_FEEDS_ON", False)
    assert abcfeeds.abc_block([{"word": "Pardon"}], fetcher=_fake_fetch) == ""


def test_fetch_feed_uses_cache(monkeypatch):
    calls = []

    class R:
        status_code = 200
        text = TOP

    def fake_get(url, **kw):
        calls.append(url)
        return R()

    monkeypatch.setattr(abcfeeds.requests, "get", fake_get)
    a, err = abcfeeds.fetch_feed("topstories", 25)
    b, err2 = abcfeeds.fetch_feed("topstories", 25)
    assert err is None and err2 is None and a == b
    assert len(calls) == 1                                          # second call served from cache


# ---------- speed limit ----------

def test_netlimit_spacing_and_parallel_cap(monkeypatch):
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.05)
    monkeypatch.setattr(C, "NET_MAX_PARALLEL", 2)
    starts, live, peak = [], [0], [0]
    lock = threading.Lock()

    def worker():
        with netlimit.ticket("https://news.google.com/rss/search?q=x"):
            with lock:
                starts.append(time.monotonic())
                live[0] += 1
                peak[0] = max(peak[0], live[0])
            time.sleep(0.02)
            with lock:
                live[0] -= 1

    ts = [threading.Thread(target=worker) for _ in range(12)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    starts.sort()
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    assert len(starts) == 12
    assert min(gaps) >= 0.04                                        # never two starts in the same moment
    assert peak[0] <= 2


def test_netlimit_sites_do_not_block_each_other(monkeypatch):
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 1.0)
    assert netlimit.reserve("news.google.com") == 0
    assert netlimit.reserve("feeds.abcnews.com") == 0             # other site: no wait
    assert netlimit.reserve("news.google.com") > 0.9               # same site: waits its turn


def test_netlimit_deadline(monkeypatch):
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 5.0)
    netlimit.reserve("feeds.abcnews.com")
    with pytest.raises(TimeoutError):
        with netlimit.ticket("https://feeds.abcnews.com/abcnews/topstories", time.monotonic() + 0.5):
            pass


def test_google_headlines_go_through_the_limit(monkeypatch):
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.05)
    monkeypatch.setattr(C, "HEADLINES_WORKERS", 4)
    starts = []
    lock = threading.Lock()

    class R:
        status_code = 200
        text = "<rss><channel></channel></rss>"

    def fake_get(url, **kw):
        with lock:
            starts.append(time.monotonic())
        return R()

    monkeypatch.setattr(headlines.requests, "get", fake_get)
    words = [{"word": w} for w in ("Pardon", "SpaceX / NASA", "Iran / Iranian", "Cancer", "Steel", "UN")]
    headlines.headlines_block(words)
    starts.sort()
    assert len(starts) == 8
    assert min(b - a for a, b in zip(starts, starts[1:])) >= 0.045


# ---------- Grok file ----------

def test_paste_file_has_both_blocks(monkeypatch):
    monkeypatch.setattr(C, "WORD_HISTORY_NIGHTS", 0)
    monkeypatch.setattr(abcfeeds, "news_blocks", lambda words: ("\nGOOGLE NEWS HEADLINES (test)", "\nABC NEWS FEEDS (test)"))
    out = prompt.build_paste_file("2026-10-01", "KXWORLDNEWSMENTION-26OCT01", [{"word": "Pardon"}])
    assert out.index("ABC NEWS FEEDS (test)") < out.index("GOOGLE NEWS HEADLINES (test)")
    assert "read its ABC NEWS FEEDS matches" in out


def test_paste_file_survives_news_crash(monkeypatch):
    monkeypatch.setattr(C, "WORD_HISTORY_NIGHTS", 0)

    def boom(words):
        raise RuntimeError("down")

    monkeypatch.setattr(abcfeeds, "news_blocks", boom)
    out = prompt.build_paste_file("2026-10-01", "KXWORLDNEWSMENTION-26OCT01", [{"word": "Pardon"}])
    assert "1. Pardon" in out and "ABC NEWS FEEDS (" not in out


# ---------- read-only preview script ----------

def test_preview_script_runs_offline(monkeypatch, capsys):
    import importlib.util
    import pathlib

    spec = importlib.util.spec_from_file_location(
        "preview_news", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "preview_news.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.01)

    class R:
        def __init__(self, text):
            self.status_code, self.text = 200, text

    def fake_get(url, **kw):
        if "news.google.com" in url:
            return R("<rss><channel><item><title>Pardon story - ABC News</title><source>ABC News</source></item></channel></rss>")
        if url.endswith("topstories"):
            return R(TOP)
        return R(WORLD)

    monkeypatch.setattr(abcfeeds.requests, "get", fake_get)
    monkeypatch.setattr(headlines.requests, "get", fake_get)
    assert mod.main([], words=[{"word": "Pardon"}, {"word": "Iran / Iranian"}]) == 0
    out = capsys.readouterr().out
    assert "ABC MATCHES PER WORD" in out and "GOOGLE NEWS HEADLINES" in out
    assert "SUMMARY:" in out and "postgres" not in out.lower()
