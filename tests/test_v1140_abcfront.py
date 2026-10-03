"""v1.14.0 tests: ABC's homepage and video page as two more ABC "feeds". No network: fake HTML.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import pytest

from gap import abcfeeds, abcfront, config as C, netlimit

HOME = """<html><head><script>window['__abcnews__']={"page":{"headline":"Ignore me: this is page data, not a card"}}</script></head>
<body><nav><a href="/US" aria-label="Open profile menu">US</a><a href="https://abcnews.com/Live">Live TV</a></nav>
<section>
 <a class="AnchorLink" href="https://abcnews.com/US/cornells-jane-doe-felt/story?id=136944644"><img alt="x"></a>
 <a class="AnchorLink" tabindex="0" href="https://abcnews.com/US/cornells-jane-doe-felt/story?id=136944644"
    aria-label="Cornell frat brother apologized to Jane Doe after alleged rape, texts show 2026-10-02T15:01:22Z"><h2>Cornell frat brother apologized</h2></a>
 <a href="/Sports/tony-romo-parting-ways-cbs-sports-dwi-arrest/story?id=136952211"><h3>Tony Romo parting ways with CBS Sports
    following DWI arrest, network says</h3></a>
 <a href="https://abcnews.com/Business/wireStory/amazon-invest-1b-data-center-communities-amid-backlash-136948782"><span>Amazon to invest $1B into data center communities</span></a>
 <a href="https://abcnews.com/video/136953426/" aria-label="What to know about state execution protocols"></a>
 <a href="https://abcnews.com/US/2-dead-helicopter-crashes-californias-catalina-island/story?id=136911014"><img></a>
 <a href="https://abcnews.com/Politics/kennedy-center-honors-held-dcs-capital-arena/story?id=136953118">Kennedy Center Honors to be held at DC&#x27;s Capital One Arena</a>
 <a href="https://example.com/ad/story?id=1234567">Buy this thing right now today</a>
 <a href="/Privacy">Privacy Policy and other legal words</a>
</section></body></html>"""

VIDEO = """<html><body>
 <a href="https://abcnews.com/US/cornells-jane-doe-felt/story?id=136944644"><img></a>
 <a href="/Business/video/september-jobs-report-shows-hiring-slowdown-136951111" aria-label="September jobs report shows hiring slowdown"></a>
 <a href="/International/video/flydubai-pilot-recounts-cockpit-attack-136950222"><span>1:56</span><span>FlyDubai pilot recounts cockpit attack</span> <span>Oct 01, 2026</span></a>
 <a href="/US/video/judge-dismisses-reflecting-pool-case-136950333"><span>12:05</span> Judge permanently dismisses former Olympian's Reflecting Pool vandalism case <i>2 hours ago</i></a>
</body></html>"""

CARDS_ONLY = """<html><body>""" + "".join(
    f'<div role="link" aria-label="Story number {i} about the weather in a city"></div>' for i in range(10)) + \
    '<button aria-label="Open dropdown menu">x</button></body></html>'

DATA_ONLY = '<html><script>{"items":[' + ",".join(
    '{"headline":"Embedded headline number %d for a \\"quoted\\" story"}' % i for i in range(9)) + "]}</script></html>"


class R:
    def __init__(self, text, status=200):
        self.status_code, self.text, self.content = status, text, text.encode("utf-8")


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    netlimit.reset()
    abcfeeds._cache.clear()
    abcfront.last_stats.clear()
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "ABC_FEEDS_ON", True)
    monkeypatch.setattr(C, "ABC_FRONT_ON", True)
    monkeypatch.setattr(C, "ABC_SKIP_FEEDS", set())
    yield
    netlimit.reset()


def test_story_links_are_read_in_page_order():
    items, stats = abcfront.parse_page(HOME, 45)
    titles = [i["title"] for i in items]
    assert titles == [
        "Cornell frat brother apologized to Jane Doe after alleged rape, texts show",   # the label wins over the picture link; the time stamp is cut
        "Tony Romo parting ways with CBS Sports following DWI arrest, network says",     # visible text, line break joined
        "Amazon to invest $1B into data center communities",                             # wire story address
        "What to know about state execution protocols",                                  # video link
        "2 dead helicopter crashes californias catalina island",                         # no text at all: the words of the address
        "Kennedy Center Honors to be held at DC's Capital One Arena",                    # &#x27; turned into '
    ]
    assert [i["rank"] for i in items] == [1, 2, 3, 4, 5, 6]
    assert items[1]["link"] == "https://abcnews.com/Sports/tony-romo-parting-ways-cbs-sports-dwi-arrest/story?id=136952211"
    assert stats["links"] == 6 and stats["aria"] == 0 and stats["embedded"] == 0     # menus, ads and other sites are never read
    assert len(abcfront.parse_page(HOME, 2)[0]) == 2
    assert abcfront.parse_page(HOME.encode("utf-8"), 45)[0][0]["title"].startswith("Cornell")


def test_layout_change_falls_back_to_card_labels_then_page_data():
    items, stats = abcfront.parse_page(CARDS_ONLY, 45)
    assert len(items) == 10 and stats["links"] == 0 and stats["aria"] == 10
    assert not any("dropdown" in i["title"].lower() for i in items)
    items, stats = abcfront.parse_page(DATA_ONLY, 45)
    assert len(items) == 9 and stats["embedded"] == 9 and items[0]["title"] == 'Embedded headline number 0 for a "quoted" story'
    assert abcfront.parse_page("", 45)[0] == [] and abcfront.parse_page("<html>nothing here</html>", 45)[0] == []


def _get(monkeypatch, pages: dict, calls: list):
    def fake(url, **kw):
        calls.append(url)
        if url in pages:
            return pages[url]
        return R('<?xml version="1.0"?><rss><channel></channel></rss>')          # every RSS feed: empty
    monkeypatch.setattr(abcfront.requests, "get", fake)
    monkeypatch.setattr(abcfeeds.requests, "get", fake)


def test_front_page_fills_the_hole_the_rss_left(monkeypatch):
    """Oct 2: 'Romo: none in ABC feeds' and 'Amazon: none in ABC feeds' while both were on abcnews.com."""
    calls: list = []
    _get(monkeypatch, {"https://abcnews.com/": R(HOME), "https://abcnews.com/video": R(VIDEO)}, calls)
    words = [{"word": "Romo"}, {"word": "Amazon"}, {"word": "Unemployment"}, {"word": "Helicopter"}, {"word": "Ebola"}]
    out = abcfeeds.abc_block(words)
    assert "- Romo: 1 ABC item(s): [Front Page #2] Tony Romo parting ways with CBS Sports following DWI arrest, network says" in out
    assert "- Amazon: 1 ABC item(s): [Front Page #3] Amazon to invest $1B into data center communities" in out
    assert "- Helicopter: 1 ABC item(s): [Front Page #5] 2 dead helicopter crashes californias catalina island" in out
    assert "- Ebola: none in ABC feeds" in out
    assert "ABC Front Page:\n  1. Cornell frat brother apologized" in out
    assert ("ABC Video:\n  1. September jobs report shows hiring slowdown\n  2. FlyDubai pilot recounts cockpit attack [Oct 01]\n"
            "  3. Judge permanently dismisses former Olympian's Reflecting Pool vandalism case [2 hours ago]") in out
    assert "Cornell" not in out.split("ABC Video:")[1]                   # the video page's copy of the homepage stories is left out
    assert "a jobs report carries 'unemployment'" in out
    assert "plus ABC's homepage and video page as they are right now" in out and "[Front Page #n] = the headline is on abcnews.com" in out
    assert calls.count("https://abcnews.com/") == 1 and calls.count("https://abcnews.com/video") == 1
    assert abcfront.last_stats["home"]["kept"] == 6 and abcfront.last_stats["video"]["kept"] == 3
    abcfeeds.abc_block(words)                                             # within 5 minutes: served from the cache
    assert calls.count("https://abcnews.com/") == 1


def test_same_story_on_the_homepage_and_in_a_feed_counts_once(monkeypatch):
    rss = ('<?xml version="1.0"?><rss><channel><item><title>California helicopter crash kills 2 people and leaves 1 missing</title>'
           '<link>https://abcnews.com/US/2-dead-helicopter-crashes-californias-catalina-island/story?id=136911014&amp;cid=rss</link>'
           '</item></channel></rss>')
    calls: list = []

    def fake(url, **kw):
        calls.append(url)
        if url == "https://abcnews.com/":
            return R(HOME)
        if url.endswith("usheadlines"):
            return R(rss)
        return R('<?xml version="1.0"?><rss><channel></channel></rss>')
    monkeypatch.setattr(abcfront.requests, "get", fake)
    monkeypatch.setattr(abcfeeds.requests, "get", fake)
    out = abcfeeds.abc_block([{"word": "Helicopter"}])
    assert "- Helicopter: 1 ABC item(s): [US #1] California helicopter crash kills 2 people" in out   # same story number: once


def test_blocked_or_changed_page_never_breaks_the_file(monkeypatch):
    calls: list = []
    _get(monkeypatch, {"https://abcnews.com/": R("Access Denied", 403), "https://abcnews.com/video": R("<html></html>")}, calls)
    out = abcfeeds.abc_block([{"word": "Romo"}])
    assert "ABC NEWS FEEDS: unavailable today" in out or "ABC Front Page: (not fetched: HTTP 403)" in out
    assert abcfront.last_stats["home"] == {"error": "HTTP 403"}

    def boom(url, **kw):
        raise RuntimeError("network down")
    monkeypatch.setattr(abcfront.requests, "get", boom)
    abcfeeds._cache.clear()
    assert abcfront.fetch_page("home", 45) == ([], "RuntimeError")


def test_front_page_can_be_switched_off(monkeypatch):
    monkeypatch.setattr(C, "ABC_FRONT_ON", False)
    keys = [f[0] for f in abcfeeds.wanted_feeds([{"word": "Romo"}])]
    assert "front" not in keys and "video" not in keys
    calls: list = []
    _get(monkeypatch, {}, calls)
    abcfeeds.abc_block([{"word": "Romo"}])
    assert not any(u.startswith("https://abcnews.com") for u in calls)
