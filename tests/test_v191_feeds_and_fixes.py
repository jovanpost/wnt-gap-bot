"""v1.9.1 tests: other networks + wires block, and the challenger fixes found on the first live run
(Mistral 429 with Retry-After, OpenRouter upstream 429 -> fallback models, slow answers). No network.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import pytest

from gap import abcfeeds, challengers as CH, config as C, morefeeds, netlimit, prompt, shadow

NOW = datetime.now(timezone.utc)


def _rss(titles, age_h=1, source=None):
    items = "".join(
        f"<item><title><![CDATA[{t}{' - ' + source if source else ''}]]></title>"
        f"<pubDate>{format_datetime(NOW - timedelta(hours=age_h))}</pubDate>"
        + (f"<source>{source}</source>" if source else "") + "</item>" for t in titles)
    return f"<rss version='2.0'><channel>{items}</channel></rss>"


ATOM = ("<feed xmlns='http://www.w3.org/2005/Atom'>"
        + "".join(f"<entry><title>{t}</title><updated>{NOW.isoformat()}</updated></entry>"
                  for t in ("Hegseth cuts generals", "Axios AM: Iran talks"))
        + "</feed>")


class R:
    def __init__(self, code, text="", js=None, headers=None):
        self.status_code, self.text, self._js, self.headers = code, text, js, headers or {}

    def json(self):
        return self._js


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    netlimit.reset()
    morefeeds._cache.clear()
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "MORE_FEEDS_ON", True)
    monkeypatch.setattr(C, "MORE_FEEDS_SKIP", set())


# ---------- feeds ----------

def test_parse_rss_atom_and_google_source():
    items = morefeeds.parse(_rss(["A", "B", "C"]), 2)
    assert [i["title"] for i in items] == ["A", "B"] and items[0]["published"] is not None
    atom = morefeeds.parse(ATOM, 20)
    assert [i["title"] for i in atom] == ["Hegseth cuts generals", "Axios AM: Iran talks"]
    g = morefeeds.parse(_rss(["Judge tosses Reflecting Pool case"], source="AP News"), 20, strip_source=True)
    assert g[0]["title"] == "Judge tosses Reflecting Pool case" and g[0]["source"] == "AP News"


def test_drop_rules():
    assert morefeeds.check([]) == "empty"
    stale = morefeeds.parse(_rss(["old"], age_h=24 * 9), 20)          # like Yahoo's Sep 22 feed
    assert morefeeds.check(stale).startswith("stale")
    assert morefeeds.check(morefeeds.parse(_rss(["fresh"]), 20)) is None


def test_fetch_all_drops_bad_feeds(monkeypatch):
    def get(url, **kw):
        if "nbcnews" in url:
            return R(200, _rss(["Renee Good's family sues ICE officer", "Supreme Court takes ICE detention case"]))
        if "npr" in url:
            return R(503)
        if "yahoo" in url:
            return R(200, _rss(["Old Iran story"], age_h=200))
        if "axios" in url:
            return R(200, ATOM)
        if "apnews" in url:
            return R(200, _rss(["Helicopter crash off Catalina kills 2"], source="AP News"))
        if "news.google.com/rss?" in url:
            return R(200, _rss(["Pike survives botched execution", "Crew-13 launches"], source="CBS News"))
        return R(200, "<rss><channel></channel></rss>")
    monkeypatch.setattr(morefeeds.requests, "get", get)
    got = morefeeds.fetch_all()
    assert got["npr"] == ([], "HTTP 503") and got["yahoo"][1].startswith("stale") and got["pbs"] == ([], "empty")
    assert len(got["nbc"][0]) == 2 and got["axios"][1] is None
    out = morefeeds.render([{"word": "ICE"}, {"word": "Helicopter"}, {"word": "Hegseth"}, {"word": "Pardon"}], got)
    assert "- ICE: 2 headline(s) in 1 outlet(s): [NBC] Renee Good's family sues ICE officer" in out
    assert "- Helicopter: 1 headline(s) in 1 outlet(s): [AP (via Google News)] Helicopter crash off Catalina kills 2" in out
    assert "- Hegseth: 1 headline(s) in 1 outlet(s): [Axios]" in out
    assert "- Pardon: none" in out
    assert "Dropped: " in out and "NPR: HTTP 503" in out and "Yahoo: stale" in out
    assert "GOOGLE NEWS US TOP STORIES" in out and "1. Pike survives botched execution — CBS News" in out


def test_title_only_never_summary():
    got = {"nbc": ([{"rank": 1, "title": "Big storm news", "source": "", "published": NOW,
                     "summary": "Hurricane Polo remnants"}], None)}
    assert morefeeds.title_hits("Hurricane / Polo", got) == []


def test_same_headline_two_feeds_listed_once():
    it = {"rank": 1, "title": "Trump says he would consider pardoning members", "source": "", "published": NOW}
    got = {"nbc": ([it], None), "gtop": ([dict(it, source="ABC News")], None)}
    assert len(morefeeds.title_hits("Pardon", got)) == 1


def test_off_switch_and_all_dead(monkeypatch):
    monkeypatch.setattr(C, "MORE_FEEDS_ON", False)
    assert morefeeds.fetch_all() == {} and morefeeds.render([{"word": "ICE"}], {"nbc": ([], "x")}) == ""
    monkeypatch.setattr(C, "MORE_FEEDS_ON", True)
    assert "unavailable today" in morefeeds.render([{"word": "ICE"}], {"nbc": ([], "HTTP 500")})


def test_grok_file_order_abc_other_google(monkeypatch):
    monkeypatch.setattr(C, "WORD_HISTORY_NIGHTS", 0)
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: {
        "at": 0, "google": ([("Pardon", "Pardon")], {"Pardon": ([{"title": "Pardon story", "source": "AP"}], None)}),
        "abc": ([("top", "Top Stories", "topstories", 25)], {"top": [{"rank": 1, "title": "Pardon item", "summary": "",
                                                                       "link": "l", "published": None}]}, {}),
        "more": {"nbc": ([{"rank": 1, "title": "Pardon talk at NBC", "source": "", "published": NOW}], None)}})
    out = prompt.build_paste_file("2026-10-02", "EVT", [{"word": "Pardon"}])
    a, m, g = (out.index("ABC NEWS FEEDS (ABC"), out.index("OTHER NETWORKS AND WIRES (fetched"),
               out.index("GOOGLE NEWS HEADLINES (fetched"))
    assert a < m < g and "[NBC] Pardon talk at NBC" in out


# ---------- challenger fixes ----------

WORDS = ["Pardon", "SNAP / Food Stamp"]


def _ok(model="x"):
    js = json.dumps({"date": "2026-10-01", "forecasts": [{"word": w, "probability": p, "reasoning": "Blind: x"}
                                                          for w, p in zip(WORDS, (40, 15))]})
    return R(200, js={"model": model, "choices": [{"message": {"content": js}}]})


def test_retry_after_is_obeyed_and_limits_shown(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "k")
    answers = [R(429, js={"message": "Rate limit exceeded"},
                 headers={"Retry-After": "75", "x-ratelimit-limit-tokens-minute": "50000",
                          "x-ratelimit-remaining-tokens-minute": "0", "Set-Cookie": "nope"}), _ok()]
    monkeypatch.setattr(CH.requests, "post", lambda url, **kw: answers.pop(0))
    slept, msgs = [], []
    out = CH.forecast("mistral", "mistral-small-latest", "file", WORDS, "2026-10-01", shadow.PREFACE,
                      sleep=slept.append, on_attempt=msgs.append)
    assert slept == [75] and out["attempts"] == 2                       # waited what Mistral asked, not 30
    assert "x-ratelimit-limit-tokens-minute=50000" in msgs[0] and "Set-Cookie" not in msgs[0]


def test_openrouter_sends_fallbacks_and_records_who_answered(monkeypatch):
    monkeypatch.setattr(C, "OPENROUTER_API_KEY", "k")
    sent = []

    def post(url, data=None, **kw):
        sent.append(json.loads(data))
        return _ok(model="meta-llama/llama-4-maverick:free")
    monkeypatch.setattr(CH.requests, "post", post)
    out = CH.forecast("openrouter", "google/gemma-4-31b-it:free", "file", WORDS, "2026-10-01", shadow.PREFACE,
                      sleep=lambda s: None,
                      fallbacks=["meta-llama/llama-4-maverick:free", "inclusionai/ling-3.0-flash-sante:free"])
    assert sent[0]["models"] == ["google/gemma-4-31b-it:free", "meta-llama/llama-4-maverick:free",
                                 "inclusionai/ling-3.0-flash-sante:free"]
    assert out["model"] == "openrouter:meta-llama/llama-4-maverick:free"


def test_openrouter_picks_different_makers():
    ids = ["google/gemma-4-31b-it:free", "google/gemma-4-12b-it:free", "meta-llama/llama-4-maverick:free",
           "inclusionai/ling-3.0-flash-sante:free", "deepseek/deepseek-v4"]
    got = CH.pick_models("openrouter", "free", ids, 3)
    assert len(got) == 3 and len({g.split("/")[0] for g in got}) == 3 and "deepseek/deepseek-v4" not in got


def test_slow_answer_message_and_timeout_setting(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "k")
    seen = {}
    answers = [CH.requests.Timeout(), _ok()]

    def post(url, timeout=None, **kw):
        seen["timeout"] = timeout
        a = answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a
    monkeypatch.setattr(CH.requests, "post", post)
    msgs = []
    CH.forecast("nvidia", "z-ai/glm-5.3", "file", WORDS, "2026-10-01", shadow.PREFACE,
                sleep=lambda s: None, on_attempt=msgs.append)
    assert seen["timeout"] == C.CHALLENGER_TIMEOUT_S == 600
    assert "no answer within 600s" in msgs[0]


def test_default_lineup():
    assert "mistral:mistral-medium" in C.CHALLENGERS       # v1.14.2: this key's Mistral list has no "large" model (Oct 2)
    assert "nvidia:nvidia/nemotron-3-ultra-550b-a55b" in C.CHALLENGERS
    assert not any(c.startswith("nvidia:qwen") for c in C.CHALLENGERS)   # NVIDIA lists no Qwen today


# ---------- v1.9.2 ----------

def test_feed_bytes_keep_apostrophes(monkeypatch):
    xml = ("<?xml version='1.0' encoding='utf-8'?><rss><channel><item><title>Trump says he’d consider pardoning"
           f"</title><pubDate>{format_datetime(NOW)}</pubDate></item></channel></rss>").encode("utf-8")

    class Raw:
        status_code, content = 200, xml

        @property
        def text(self):                      # what requests guesses without a charset header
            return xml.decode("latin-1")
    monkeypatch.setattr(morefeeds.requests, "get", lambda url, **kw: Raw())
    items, err = morefeeds.fetch_feed("yahoo", "https://news.yahoo.com/rss", "net")
    assert err is None and items[0]["title"] == "Trump says he’d consider pardoning"


def test_req_minute_zero_is_not_blocked(monkeypatch):
    """v1.9.4: Mistral's x-ratelimit-limit-req-minute=0 means 'no per-minute request limit' -> retry, not stop."""
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "k")
    answers = [R(429, js={"message": "Rate limit exceeded"},
                 headers={"x-ratelimit-limit-req-minute": "0", "x-ratelimit-remaining-req-minute": "0"}), _ok()]
    monkeypatch.setattr(CH.requests, "post", lambda url, **kw: answers.pop(0))
    slept = []
    out = CH.forecast("mistral", "mistral-large-2512", "file", WORDS, "2026-10-01", shadow.PREFACE, sleep=slept.append)
    assert slept == [30] and out["attempts"] == 2


def test_zero_token_allowance_fails_at_once(monkeypatch):
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "k")
    monkeypatch.setattr(CH.requests, "post", lambda url, **kw: R(
        429, js={"message": "Rate limit exceeded"}, headers={"x-ratelimit-limit-tokens-minute": "0"}))
    slept = []
    with pytest.raises(CH.Fatal, match="0 tokens a minute"):
        CH.forecast("mistral", "mistral-large-2512", "file", WORDS, "2026-10-01", shadow.PREFACE, sleep=slept.append)
    assert slept == []


def test_request_bigger_than_token_limit_fails_at_once(monkeypatch):
    """Oct 1: Mistral Small allows 20,000 tokens a minute; our file + answer room is bigger."""
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "k")
    monkeypatch.setattr(CH.requests, "post", lambda url, **kw: R(
        429, js={"message": "Rate limit exceeded"}, headers={"x-ratelimit-limit-tokens-minute": "20000"}))
    slept = []
    big_file = "x" * 60000                      # ~15k tokens + 12k answer room > 20k
    with pytest.raises(CH.Fatal, match="can never fit"):
        CH.forecast("mistral", "mistral-small-2603", big_file, WORDS, "2026-10-01", shadow.PREFACE, sleep=slept.append)
    assert slept == []


def test_openrouter_skips_small_context_models(monkeypatch):
    monkeypatch.setattr(C, "OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(CH.requests, "get", lambda url, **kw: R(200, js={"data": [
        {"id": "google/gemma-4-31b-it:free", "context_length": 8192},
        {"id": "meta-llama/llama-4-maverick:free", "context_length": 256000},
        {"id": "some/model-without-info:free"}]}))
    ids = CH.list_models("openrouter")
    assert "google/gemma-4-31b-it:free" not in ids
    assert set(ids) == {"meta-llama/llama-4-maverick:free", "some/model-without-info:free"}


def test_done_and_stopped_lines(monkeypatch):
    from gap import abcfeeds, store
    monkeypatch.setattr(C, "SHADOW_ON", True)
    monkeypatch.setattr(C, "BASELINE_ON", False)
    monkeypatch.setattr(C, "GEMINI_API_KEY", "")
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "n")
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "m")
    monkeypatch.setattr(C, "CHALLENGERS", ["nvidia:deepseek", "mistral:mistral-large"])
    monkeypatch.setattr(store, "shadow_models_for", lambda d: set())
    monkeypatch.setattr(store, "insert_shadow_forecasts", lambda rows: len(rows))

    def get(url, **kw):
        if "nvidia" in url:
            return R(200, js={"data": [{"id": "deepseek-ai/deepseek-v4.1-flash"}]})
        return R(200, js={"data": [{"id": "mistral-large-2512"}]})

    def post(url, data=None, **kw):
        if "mistral" in url:
            return R(401, js={"message": "Unauthorized"})
        return _ok()
    monkeypatch.setattr(CH.requests, "get", get)
    monkeypatch.setattr(CH.requests, "post", post)
    msgs = []
    words = [{"word": w, "market_ticker": f"T{i}"} for i, w in enumerate(WORDS)]
    shadow.run("2026-10-01", "EVT", words, "file", save=False, on_attempt=msgs.append)
    assert any(m.startswith("done: nvidia:deepseek-ai/deepseek-v4.1-flash, 2 words") for m in msgs)
    assert any(m.startswith("stopped: mistral:mistral-large") and "key refused" in m for m in msgs)


def test_missing_key_service_is_simply_skipped(monkeypatch):
    monkeypatch.setattr(C, "CEREBRAS_API_KEY", "")
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "n")
    monkeypatch.setattr(C, "CHALLENGERS", ["cerebras:gpt-oss-120b", "nvidia:deepseek"])
    assert CH.enabled_specs() == [("nvidia", "deepseek")]
