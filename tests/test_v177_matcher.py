"""v1.7.7 tests: ABC matcher fixes. No network: every outside call is faked.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from gap import abcfeeds, config as C, headlines, netlimit

NOW = datetime.now(timezone.utc)


def _it(rank, title, summary="", link=""):
    return {"rank": rank, "title": title, "summary": summary, "link": link or f"https://abcnews.com/s/{rank}", "published": NOW}


FEEDS = [("top", "Top Stories", "topstories", 25), ("us", "US", "usheadlines", 15),
         ("intl", "International", "internationalheadlines", 15)]
LABELS = {k: l for k, l, _n, _c in FEEDS}
GOT = {
    "top": [
        _it(5, "The rise and fall of America's deadly love affair with the cigarette",
            "Smoking still causes about a third of cancer deaths."),
        _it(17, "Trump says he would consider pardoning administration members: 'I'd do that'"),
    ],
    "us": [_it(14, "Cockpit stabbing on FlyDubai plane to Tel Aviv puts crew-related hijackings in focus")],
    "intl": [],
}
GOOGLE = {
    "Reflecting Pool": ([
        {"title": "Judge permanently dismisses Reflecting Pool vandalism case against former Olympian David Hearn",
         "source": "ABC News - Breaking News, Latest News and Videos", "published": NOW},
        {"title": "Judge kills failed Reflecting Pool case", "source": "Axios", "published": NOW},
    ], None),
    "Pardon": ([
        {"title": "Trump says he would consider pardoning administration members: 'I'd do that'",
         "source": "ABC News - Breaking News, Latest News and Videos", "published": NOW},
        {"title": "Federal regulator probing Adam Kinzinger for betting on his own pardon on Kalshi",
         "source": "ABC7 Chicago", "published": NOW},
    ], None),
    "Hurricane": ([
        {"title": "Millions under flood alerts as storm remnants move east",
         "source": "ABC News", "published": NOW},
    ], None),
}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    netlimit.reset()
    abcfeeds._cache.clear()
    monkeypatch.setattr(C, "ABC_FEEDS_ON", True)
    monkeypatch.setattr(C, "HEADLINES_ON", True)
    monkeypatch.setattr(C, "ABC_SKIP_FEEDS", set())
    yield


def test_joined_word_flydubai_counts_for_dubai():
    hits = abcfeeds.word_matches("Dubai / Tel Aviv", GOT, LABELS)
    assert len(hits) == 1 and hits[0][0] == "US #14" and hits[0][2] == "title"


def test_summary_only_is_tagged():
    hits = abcfeeds.word_matches("Cancer", GOT, LABELS)
    assert hits == [("Top Stories #5", "The rise and fall of America's deadly love affair with the cigarette", "summary")]
    out = abcfeeds.render([{"word": "Cancer"}], FEEDS, GOT, {})
    assert "[Top Stories #5, summary only] The rise and fall" in out


def test_google_abc_items_count_and_affiliates_do_not():
    gabc = abcfeeds.google_abc_items(GOOGLE)
    assert set(gabc) == {"Reflecting Pool", "Pardon", "Hurricane"}
    assert all(abcfeeds.is_abc_source(i["source"]) for v in gabc.values() for i in v)
    assert not abcfeeds.is_abc_source("ABC7 Chicago")
    assert not abcfeeds.is_abc_source("ABC7 Los Angeles")

    hits = abcfeeds.word_matches("Reflecting Pool / Ballroom", GOT, LABELS, gabc)
    assert hits == [("ABC via Google News",
                     "Judge permanently dismisses Reflecting Pool vandalism case against former Olympian David Hearn", "title")]


def test_same_story_in_feed_and_google_listed_once():
    gabc = abcfeeds.google_abc_items(GOOGLE)
    hits = abcfeeds.word_matches("Pardon", GOT, LABELS, gabc)
    assert len(hits) == 1 and hits[0][0] == "Top Stories #17"


def test_google_item_without_word_in_title_is_summary_only_and_sorted_last():
    gabc = abcfeeds.google_abc_items(GOOGLE)
    got = dict(GOT, top=GOT["top"] + [_it(3, "Hurricane Polo remnants flood Kansas")])
    hits = abcfeeds.word_matches("Hurricane / Polo", got, LABELS, gabc)
    assert [h[2] for h in hits] == ["title", "summary"]
    assert hits[1][0] == "ABC via Google News"


def test_render_shows_empty_feed_and_not_found():
    out = abcfeeds.render([{"word": "SNAP / Food Stamp"}, {"word": "Dubai / Tel Aviv"}], FEEDS, GOT, {})
    assert "- SNAP / Food Stamp: none in ABC feeds" in out
    assert "- Dubai / Tel Aviv: 1 ABC item(s): [US #14] Cockpit stabbing on FlyDubai" in out
    assert "ABC International: (empty right now)" in out
    assert "[title] = the word is in the headline" in out


def test_render_google_only_when_feeds_all_failed():
    gabc = abcfeeds.google_abc_items(GOOGLE)
    out = abcfeeds.render([{"word": "Reflecting Pool / Ballroom"}], FEEDS, {k: [] for k in LABELS},
                          {k: "HTTP 503" for k in LABELS}, gabc)
    assert "ABC via Google News" in out and "ABC Top Stories: (not fetched: HTTP 503)" in out
    out2 = abcfeeds.render([{"word": "Pardon"}], FEEDS, {k: [] for k in LABELS}, {k: "HTTP 503" for k in LABELS}, {})
    assert "ABC NEWS FEEDS: unavailable today (HTTP 503)" in out2


def test_news_blocks_shares_google_abc_items(monkeypatch):
    monkeypatch.setattr(headlines, "fetch_all", lambda words: (
        [(w["word"], t) for w in words for t in headlines.search_terms(w["word"])], GOOGLE))
    monkeypatch.setattr(abcfeeds, "fetch_all", lambda words: (FEEDS, GOT, {}))
    g, a = abcfeeds.news_blocks([{"word": "Reflecting Pool / Ballroom"}, {"word": "Pardon"}])
    assert "GOOGLE NEWS HEADLINES" in g
    assert "- Reflecting Pool / Ballroom: 1 ABC item(s): [ABC via Google News]" in a
    assert "- Pardon: 1 ABC item(s): [Top Stories #17]" in a


def test_news_blocks_google_crash_keeps_abc(monkeypatch):
    def boom(words):
        raise RuntimeError("google down")

    monkeypatch.setattr(headlines, "fetch_all", boom)
    monkeypatch.setattr(abcfeeds, "fetch_all", lambda words: (FEEDS, GOT, {}))
    g, a = abcfeeds.news_blocks([{"word": "Pardon"}])
    assert g == "" and "- Pardon: 1 ABC item(s): [Top Stories #17]" in a


def test_news_blocks_abc_crash_keeps_google(monkeypatch):
    def boom(words):
        raise RuntimeError("abc down")

    monkeypatch.setattr(headlines, "fetch_all", lambda words: ([("Pardon", "Pardon")], GOOGLE))
    monkeypatch.setattr(abcfeeds, "fetch_all", boom)
    g, a = abcfeeds.news_blocks([{"word": "Pardon"}])
    assert "GOOGLE NEWS HEADLINES" in g
    assert "[ABC via Google News]" in a                       # Google's ABC items still show
    assert "ABC Top Stories: (not fetched: error)" in a


def test_term_rules_v177():
    p = abcfeeds.term_pattern
    assert p("Dubai").search("FlyDubai")
    assert not p("Steel").search("Steelers")
    assert not p("Pardon").search("unpardonable")
    assert not p("Iran").search("Tirana")                     # under 5 letters: no joined-word match
    assert p("Iran").search("Iranian")
    assert not p("AI").search("said")
