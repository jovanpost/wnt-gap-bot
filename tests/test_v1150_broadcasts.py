"""v1.15.0 tests: the PREVIOUS BROADCASTS block (ABC's segment list from the YouTube descriptions).
Fake YouTube API, no network.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import pytest

from gap import abcfeeds, broadcasts as B, config as C, netlimit, prompt

# The real description of the Oct 1, 2026 full broadcast, as Jovan pasted it.
DESC_OCT1 = """Whit Johnson reports on the failed execution of death row inmate Christa Pike, who’s in critical condition after multiple lethal injections by Tennessee authorities; Tom Soufi Burridge has the latest on the near disaster on a FlyDubai flight after authorities say the co-pilot stabbed the captain midair and attempted to crash the passenger plane – and the harrowing new video showing the immediate aftermath; Aaron Katersky reports on newly obtained documents revealing what Jane Doe told Cornell University investigators as student voice outrage at public hearing about sexual violence on campus; and more on tonight’s broadcast of World News Tonight with David Muir.

00:00 Intro
02:33 Death row inmate Christa Pike in critical condition after surviving botched execution
05:52 Witnesses chase suspect after fatal stabbing in New York City subway station
07:20 New video shows harrowing moments after midair stabbing
09:58 Cornell students hold public hearing over school's handling of alleged gang rape
11:41 Multi-day severe weather outbreak wreaks havoc in parts of the South and Midwest
13:06 30 million from Texas to Midwest brace for flash flooding
13:42 Medevac helicopter crashes in Pacific Ocean near Catalina Island, killing at least 2
14:57 Pres. Trump launches first major campaign swing, with plans to hit 5 red states
17:32 Michigan parents charged after 3-year-old son brings loaded gun to preschool: Police
17:49 Sean “Diddy” Combs heading back to Special Housing Unit in Fort Dix: Sources
18:20 Chad Lowe’s daughter dead at age 13, family says
18:38 Kylie Kelce issues apology to Kate Middleton
19:07 America Strong: Oregon fisherman pinned under boulder saved thanks to his quick thinking

ABC World News Tonight with David Muir delivers the news that matters most. Watch to get the latest news stories and headlines from around the world.

Follow ABC World News Tonight on...
Instagram:   / abcworldnewstonight
#WorldNewsTonight #DavidMuir #News #ABCNews"""

DESC_SEP30 = "Short lead.\n\n0:00 Intro\n1:50 FlyDubai flight bound for Tel Aviv diverted after cockpit attack\n6:10 Index: storm update\n"
KEY = "yt-secret-key-123"


class R:
    def __init__(self, js, status=200):
        self.status_code, self._js = status, js

    def json(self):
        return self._js


def item(vid, title, channel="ABC News"):
    return {"id": {"videoId": vid}, "snippet": {"title": title, "channelTitle": channel}}


class YT:
    def __init__(self):
        self.calls: list[tuple[str, dict, dict]] = []
        self.search_items = [
            item("live1", "LIVE: ABC News Live - Friday, October 2"),
            item("v2", "ABC World News Tonight with David Muir Full Broadcast - Oct. 2, 2026"),      # tonight: must be ignored
            item("v1", "ABC World News Tonight with David Muir Full Broadcast - Oct. 1, 2026"),
            item("clip", "Trump launches campaign swing | World News Tonight"),
            item("v30", "ABC World News Tonight with David Muir Full Broadcast - Sept. 30, 2026"),
            item("v29", "ABC World News Tonight with David Muir Full Broadcast - Sept. 29, 2026"),
            item("wk", "ABC World News Tonight Full Broadcast - Sept. 27, 2026"),                    # weekend edition: other anchor
        ]
        self.status = 200
        self.empty_with_channel = False

    def get(self, url, params=None, timeout=None, headers=None):
        self.calls.append((url, dict(params or {}), dict(headers or {})))
        if self.status != 200:
            return R({"error": {"errors": [{"reason": "quotaExceeded"}]}}, self.status)
        if url.endswith("/search"):
            if self.empty_with_channel and (params or {}).get("channelId"):
                return R({"items": []})
            return R({"items": self.search_items})
        ids = (params or {}).get("id", "").split(",")
        desc = {"v1": DESC_OCT1, "v30": DESC_SEP30, "v29": "no list here"}
        return R({"items": [{"id": i, "snippet": {"description": desc.get(i, "")},
                             "contentDetails": {"duration": "PT20M40S" if i == "v1" else "PT19M5S"}} for i in ids]})


@pytest.fixture()
def yt(monkeypatch):
    fake = YT()
    netlimit.reset()
    B._cache.clear()
    B.last_error["why"] = None
    monkeypatch.setattr(B.requests, "get", fake.get)
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "YOUTUBE_API_KEY", KEY)
    monkeypatch.setattr(C, "BROADCASTS_ON", True)
    monkeypatch.setattr(C, "BROADCASTS_N", 2)
    monkeypatch.setattr(C, "BROADCASTS_CHANNEL_ID", "UCtest")
    yield fake
    netlimit.reset()


def test_title_dates_and_description_parsing():
    assert B.title_date("ABC World News Tonight with David Muir Full Broadcast - Oct. 1, 2026") == "2026-10-01"
    assert B.title_date("ABC World News Tonight with David Muir Full Broadcast - Sept. 29, 2026") == "2026-09-29"
    assert B.title_date("ABC World News Tonight with David Muir Full Broadcast - May 20, 2026") == "2026-05-20"
    assert B.title_date("ABC World News Tonight Full Broadcast - Sept. 27, 2026") is None      # weekend show
    assert B.title_date("Trump launches campaign swing | World News Tonight") is None
    lead, ch = B.parse_description(DESC_OCT1)
    assert lead.startswith("Whit Johnson reports on the failed execution") and len(ch) == 14
    assert ch[0] == (0, "Intro") and ch[1] == (153, "Death row inmate Christa Pike in critical condition after surviving botched execution")
    assert ch[-1][0] == 19 * 60 + 7
    assert B.parse_description("1:02:03 Long show part\nno stamp") == ("", [(3723, "Long show part")])
    assert B.parse_description("") == ("", [])


def test_block_shows_what_aired_in_order_with_lengths(yt):
    out = B.block("2026-10-02")
    assert out.startswith("\nPREVIOUS BROADCASTS (the last 2 World News Tonight show(s)")
    assert "Thu Oct 1, 2026:\n  (opening headlines: 2:33)\n  1. Death row inmate Christa Pike in critical condition after surviving botched execution (3:19)" in out
    assert "  3. New video shows harrowing moments after midair stabbing (2:38)" in out
    assert "  7. Medevac helicopter crashes in Pacific Ocean near Catalina Island, killing at least 2 (1:15)" in out
    assert "  9. Michigan parents charged after 3-year-old son brings loaded gun to preschool: Police (0:17)" in out
    assert "  13. America Strong: Oregon fisherman pinned under boulder saved thanks to his quick thinking (1:33)" in out   # to the video's end
    assert "Wed Sep 30, 2026:\n  (opening headlines: 1:50)\n  1. FlyDubai flight bound for Tel Aviv diverted after cockpit attack (4:20)" in out
    assert "Oct 2, 2026" not in out and "Sep 27" not in out and "Sep 29" not in out      # not tonight, not the weekend, only the last 2
    assert out.index("Thu Oct 1") < out.index("Wed Sep 30")                                # newest first


def test_key_travels_in_a_header_and_is_never_shown(yt, caplog):
    B.block("2026-10-02")
    for url, params, headers in yt.calls:
        assert KEY not in url and KEY not in str(params) and headers.get("X-Goog-Api-Key") == KEY
    assert yt.calls[0][1]["channelId"] == "UCtest" and yt.calls[0][1]["publishedAfter"] == "2026-09-20T00:00:00Z"
    assert len(yt.calls) == 2                                           # one search, one video lookup
    B.block("2026-10-02")
    assert len(yt.calls) == 2                                           # served from the 6-hour cache
    yt.status = 403
    B._cache.clear()
    assert B.block("2026-10-02") == "" and B.last_error["why"] == "YouTube HTTP 403 quotaExceeded"
    assert KEY not in caplog.text and KEY not in str(B.last_error)


def test_no_key_switched_off_or_broken_never_stops_the_file(yt, monkeypatch):
    monkeypatch.setattr(C, "YOUTUBE_API_KEY", "")
    assert B.block("2026-10-02") == "" and B.last_error["why"] == "no YOUTUBE_API_KEY" and not yt.calls
    monkeypatch.setattr(C, "YOUTUBE_API_KEY", KEY)
    monkeypatch.setattr(C, "BROADCASTS_ON", False)
    assert B.block("2026-10-02") == "" and not yt.calls
    monkeypatch.setattr(C, "BROADCASTS_ON", True)

    def boom(*a, **k):
        raise ConnectionError("https://www.googleapis.com/youtube/v3/search?key=should-never-appear")
    monkeypatch.setattr(B.requests, "get", boom)
    assert B.fetch("2026-10-02") == [] and B.last_error["why"] == "ConnectionError"      # the type only, never the message


def test_wrong_channel_id_falls_back_to_abc_news_by_name(yt):
    yt.empty_with_channel = True
    yt.search_items.append(item("fake", "ABC World News Tonight with David Muir Full Broadcast - Oct. 1, 2026", channel="News Reuploads"))
    shows = B.fetch("2026-10-02")
    assert [s["video_id"] for s in shows] == ["v1", "v30"]              # ABC News's own uploads only
    assert "channelId" in yt.calls[0][1] and "channelId" not in yt.calls[1][1]


def test_a_show_without_a_segment_list_is_left_out(yt):
    yt.search_items = [i for i in yt.search_items if i["id"]["videoId"] in ("v1", "v29")]
    shows = B.fetch("2026-10-02")
    assert [s["date"] for s in shows] == ["2026-10-01"]


def test_the_block_sits_between_word_history_and_the_abc_feeds(yt, monkeypatch):
    monkeypatch.setattr(C, "WORD_HISTORY_NIGHTS", 0)
    monkeypatch.setattr(abcfeeds, "all_blocks", lambda words: ("\nGOOGLE NEWS HEADLINES (test)", "\nABC NEWS FEEDS (test)",
                                                               "\nOTHER NETWORKS AND WIRES (test)"))
    out = prompt.build_paste_file("2026-10-02", "KXWORLDNEWSMENTION-26OCT02", [{"word": "Helicopter"}])
    assert out.index("1. Helicopter") < out.index("PREVIOUS BROADCASTS (the last") < out.index("ABC NEWS FEEDS (test)")
    monkeypatch.setattr(C, "YOUTUBE_API_KEY", "")
    B._cache.clear()
    out = prompt.build_paste_file("2026-10-02", "KXWORLDNEWSMENTION-26OCT02", [{"word": "Helicopter"}])
    assert "PREVIOUS BROADCASTS (the last" not in out and "ABC NEWS FEEDS (test)" in out   # no key: the file is as before
