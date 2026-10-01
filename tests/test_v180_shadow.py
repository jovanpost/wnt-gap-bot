"""v1.8.0 tests: challenger forecasts (Gemini + no-AI baseline), paper only.
Local SQLite database and a fake Gemini; no network.

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json

import pytest

from gap import abcfeeds, config as C, netlimit, shadow, store

WORDS = [
    {"word": "Pardon", "market_ticker": "KXWNM-26OCT01-PARD"},
    {"word": "SNAP / Food Stamp", "market_ticker": "KXWNM-26OCT01-SNAP"},
    {"word": "Trump (5+ times)", "market_ticker": "KXWNM-26OCT01-TRUM"},
]
PASTE = """SYSTEM PROMPT ...
---
Date: 2026-10-01
WORD HISTORY (official Kalshi results, newest night first, last 10 nights)
- Pardon: N N N N N N N N - -   (said 0 of 8 listed nights)
- Trump (5+ times): Y Y N Y Y N Y Y Y Y   (said 8 of 10 listed nights)
ABC NEWS FEEDS ...
"""
NEWS = {
    "at": 0,
    "google": ([("Pardon", "Pardon"), ("SNAP / Food Stamp", "SNAP"), ("SNAP / Food Stamp", "Food Stamp"),
                ("Trump (5+ times)", "Trump")], {
        "Pardon": ([{"title": "Trump says he would consider pardoning members", "source": "ABC News"},
                    {"title": "Kinzinger probed over pardon bets", "source": "NYT"}], None),
        "SNAP": ([{"title": "Maxx Crosby doesn't need every snap", "source": "raiders.com"}], None),
        "Food Stamp": ([], None),
        "Trump": ([{"title": f"Trump story {i}", "source": "AP"} for i in range(6)], None),
    }),
    "abc": ([("top", "Top Stories", "topstories", 25)],
            {"top": [{"rank": 17, "title": "Trump says he would consider pardoning members", "summary": "", "link": "l1",
                      "published": None}]}, {}),
}


def _grok_json(words, probs):
    return json.dumps({"date": "2026-10-01", "cycle_temp": "normal", "forecasts": [
        {"word": w, "probability": p, "reasoning": "Blind: test."} for w, p in zip(words, probs)]})


@pytest.fixture()
def db(monkeypatch):
    """Real Postgres (this repo's SQL is Postgres-only). Set TEST_DATABASE_URL to a THROWAWAY
    local database; tests that need it are skipped otherwise. Never point it at Supabase."""
    import os
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        conn.execute(store.text("delete from gap_shadow_forecasts"))
        conn.execute(store.text("delete from gap_results"))
    yield
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    netlimit.reset()
    monkeypatch.setattr(C, "SHADOW_ON", True)
    monkeypatch.setattr(C, "BASELINE_ON", True)
    monkeypatch.setattr(C, "GEMINI_MODEL", "auto")
    monkeypatch.setattr(C, "GEMINI_SEARCH", False)
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: NEWS)
    monkeypatch.setattr(store, "grok_forecasts_for_date", lambda d: [
        {"word": "Pardon", "market_ticker": "KXWNM-26OCT01-PARD", "probability": 41},
        {"word": "SNAP / Food Stamp", "market_ticker": "KXWNM-26OCT01-SNAP", "probability": 18},
        {"word": "Trump (5+ times)", "market_ticker": "KXWNM-26OCT01-TRUM", "probability": 56}])


class FakeGemini:
    """Model list + generateContent. `refuse` = models that answer 429."""

    def __init__(self, probs=(30, 20, 60), refuse=(), refuse_404=(), busy=0):
        self.probs, self.refuse, self.refuse_404 = probs, set(refuse), set(refuse_404)
        self.busy = busy                      # first N generate calls answer 503
        self.calls, self.headers, self.bodies = [], [], []

    def get(self, url, headers=None, timeout=None, **kw):
        self.headers.append(headers or {})
        assert "key=" not in url
        return _R(200, {"models": [
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-flash-lite", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.8-flash-live", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-9", "supportedGenerationMethods": ["embedContent"]},
        ]})

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        self.headers.append(headers or {})
        assert "key=" not in url
        model = url.split("/models/")[1].split(":")[0]
        self.calls.append(model)
        self.bodies.append(data)
        body = json.loads(data)
        if model in self.refuse_404:
            return _R(404, {"error": {"status": "NOT_FOUND"}})
        if self.busy > 0:
            self.busy -= 1
            return _R(503, {"error": {"status": "UNAVAILABLE"}})
        assert "NO web, X or browsing tools" in body["system_instruction"]["parts"][0]["text"]
        if model in self.refuse:
            return _R(429, {"error": {"status": "RESOURCE_EXHAUSTED"}})
        txt = _grok_json([w["word"] for w in WORDS], self.probs)
        return _R(200, {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True}, {"text": txt}]}}]})


class _R:
    def __init__(self, code, js):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


def _use(monkeypatch, fake, key="test-key"):
    monkeypatch.setattr(C, "GEMINI_API_KEY", key)
    monkeypatch.setattr(shadow.requests, "get", fake.get)
    monkeypatch.setattr(shadow.requests, "post", fake.post)


# ---------- baseline ----------

def test_history_counts_from_paste():
    h = shadow.history_counts(PASTE)
    assert h == {"Pardon": (0, 8), "Trump (5+ times)": (8, 10)}


def test_baseline_prob_directions():
    hi = shadow.baseline_prob("Iran", (8, 10), "title", 6)
    lo = shadow.baseline_prob("Iraq", (0, 8), None, 0)
    mid = shadow.baseline_prob("Cancer", None, "summary", 3)
    assert hi > mid > lo and 2 <= lo and hi <= 97
    assert shadow.baseline_prob("Trump (5+ times)", (8, 10), "title", 6) < hi   # count words cut


def test_baseline_forecast_uses_news_and_history():
    rows = {r["word"]: r for r in shadow.baseline_forecast(WORDS, NEWS, PASTE)}
    assert rows["Pardon"]["reasoning"] == "history 0/8; ABC title; Google title hits 2"
    assert "no history; ABC none; Google title hits 1" == rows["SNAP / Food Stamp"]["reasoning"]  # 'snap' idiom counts once
    assert rows["Trump (5+ times)"]["probability"] > rows["SNAP / Food Stamp"]["probability"]


# ---------- Gemini ----------

def test_gemini_404_model_is_skipped_at_once(monkeypatch):
    fake = FakeGemini(refuse_404={"gemini-3.8-flash"})
    _use(monkeypatch, fake)
    assert shadow.gemini_candidates() == ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-flash-lite"]
    slept = []
    out = shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01", sleep=slept.append)
    assert fake.calls == ["gemini-3.8-flash", "gemini-3.7-flash"] and slept == []
    assert out["model"] == "gemini:gemini-3.7-flash"
    assert [f["probability"] for f in out["forecasts"]] == [30, 20, 60]
    assert all(h.get("x-goog-api-key") == "test-key" for h in fake.headers)   # key in header, never URL


def test_gemini_fixed_model_setting(monkeypatch):
    fake = FakeGemini()
    _use(monkeypatch, fake)
    monkeypatch.setattr(C, "GEMINI_MODEL", "gemini-3.5-flash")
    out = shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01")
    assert out["model"] == "gemini:gemini-3.5-flash"


def test_gemini_bad_json_is_an_error_not_a_crash(monkeypatch):
    fake = FakeGemini(probs=(30, 20, 150))          # 150 is not a valid probability
    _use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="bad answer"):
        shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01", budget_s=100, sleep=lambda s: None)
    assert len(fake.calls) == 3                     # retried (30 + 60 fit in 100s, the third wait would not)


def test_gemini_search_mode_body(monkeypatch):
    fake = FakeGemini()
    seen = {}
    orig = fake.post

    def post(url, headers=None, data=None, timeout=None, **kw):
        seen.update(json.loads(data))
        body = json.loads(data)
        body["system_instruction"]["parts"][0]["text"] += " NO web, X or browsing tools"
        return orig(url, headers=headers, data=json.dumps(body), timeout=timeout)

    _use(monkeypatch, fake)
    monkeypatch.setattr(shadow.requests, "post", post)
    monkeypatch.setattr(C, "GEMINI_SEARCH", True)
    shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01")
    assert seen["tools"] == [{"google_search": {}}]
    assert "responseMimeType" not in seen["generationConfig"]


# ---------- run + store ----------

def test_run_saves_once_per_night(db, monkeypatch):
    fake = FakeGemini()
    _use(monkeypatch, fake)
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    assert rep["baseline-v1"]["n"] == 3 and rep["gemini:gemini-3.8-flash"]["n"] == 3
    rows = store.shadow_forecasts("2026-10-01")
    assert len(rows) == 6 and {r["market_ticker"] for r in rows} == {w["market_ticker"] for w in WORDS}
    rep2 = shadow.run("2026-10-01", "EVT", WORDS, PASTE)          # second run: nothing asked again
    assert rep2 == {} and len(fake.calls) == 1
    assert len(store.shadow_forecasts("2026-10-01")) == 6


def test_run_without_save_writes_nothing(db, monkeypatch):
    _use(monkeypatch, FakeGemini())
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE, save=False)
    assert rep["baseline-v1"]["ok"] and store.shadow_forecasts("2026-10-01") == []


def test_run_without_key_still_runs_baseline(db, monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "")
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    assert rep["baseline-v1"]["ok"] and rep["gemini"] == {"ok": False, "error": "GEMINI_API_KEY not set"}


def test_gemini_failure_does_not_stop_baseline(db, monkeypatch):
    _use(monkeypatch, FakeGemini(refuse={"gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.1-flash-lite"}))
    monkeypatch.setattr(C, "GEMINI_RETRY_BUDGET_S", 100)
    monkeypatch.setattr(shadow.time, "sleep", lambda s: None)
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    assert rep["baseline-v1"]["ok"] and not rep["gemini"]["ok"]
    assert "RESOURCE_EXHAUSTED" in rep["gemini"]["error"]
    assert "test-key" not in rep["gemini"]["error"]


def test_summary_and_stored_summary(db, monkeypatch):
    _use(monkeypatch, FakeGemini())
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    txt = shadow.summary_text("2026-10-01", WORDS, rep, {"Pardon": 41})
    assert "word | Grok | base | Gemini" in txt or "word | Grok | Gemini | base" in txt
    assert "Pardon | 41 |" in txt
    st = shadow.stored_summary("2026-10-01", WORDS)
    assert "Pardon | 41 |" in st and "test-key" not in st


def test_weekly_block_scores_against_results(db, monkeypatch):
    _use(monkeypatch, FakeGemini(probs=(80, 10, 60)))
    shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    with store.engine().begin() as conn:
        for t, r in (("KXWNM-26OCT01-PARD", "yes"), ("KXWNM-26OCT01-SNAP", "no"), ("KXWNM-26OCT01-TRUM", "yes")):
            conn.execute(store.text("insert into gap_results (market_ticker, result) values (:t, :r)"), {"t": t, "r": r})
    lines = shadow.weekly_block("2026-09-28", "2026-10-02")
    gem = next(l for l in lines if l.startswith("gemini:"))
    # Gemini (0.8, 0.1, 0.6) vs Grok (0.41, 0.18, 0.56) on yes/no/yes -> Gemini lower Brier
    assert gem.split(" | ")[1] == "3" and gem.endswith("challenger")
    assert any(l.startswith("baseline-v1 |") for l in lines)


def test_weekly_block_empty(db):
    assert shadow.weekly_block("2026-09-28", "2026-10-02") == ["No challenger forecasts this week."]


def test_pipeline_starts_challengers_after_file(monkeypatch):
    from gap import pipeline
    called = {}
    monkeypatch.setattr(shadow, "run_async", lambda *a, **k: called.setdefault("args", a) or True)
    pipeline._start_challengers("2026-10-01", "EVT", WORDS, PASTE)
    assert called["args"][:2] == ("2026-10-01", "EVT")
    monkeypatch.setattr(C, "SHADOW_ON", False)
    called.clear()
    pipeline._start_challengers("2026-10-01", "EVT", WORDS, PASTE)
    assert called == {}


def test_challenger_code_never_touches_orders():
    import inspect
    src = inspect.getsource(shadow)
    code = src.split('"""', 2)[2].lower()            # skip the module docstring
    for bad in ("kalshi", "place_order", "cancel_order", "gap_l_orders", "import live", " live."):
        assert bad not in code


# ---------- v1.8.1: retry rule ----------

class Clock:
    def __init__(self):
        self.t = 0.0
        self.slept = []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


def test_busy_retries_same_model_same_request(monkeypatch):
    fake = FakeGemini(busy=3)
    _use(monkeypatch, fake)
    clk = Clock()
    msgs = []
    out = shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01",
                                 sleep=clk.sleep, now=clk.now, on_attempt=msgs.append)
    assert fake.calls == ["gemini-3.8-flash"] * 4               # never downgraded
    assert clk.slept == [30, 60, 120]
    assert len(set(fake.bodies)) == 1                            # the SAME request every time
    assert out["model"] == "gemini:gemini-3.8-flash" and out["attempts"] == 4 and out["waited_s"] == 210
    assert "same request again in 30s" in msgs[0]


def test_downgrade_only_after_20_minutes_and_stop_at_30(monkeypatch):
    fake = FakeGemini(busy=10_000)
    _use(monkeypatch, fake)
    clk = Clock()
    with pytest.raises(RuntimeError, match="gave up after"):
        shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01", sleep=clk.sleep, now=clk.now)
    assert sum(clk.slept) <= 1800                                # never past 30 minutes
    # first tries all on the best model; a lower model only once 20 min (1200s) had passed
    t, first_lower = 0, None
    for i, model in enumerate(fake.calls):
        if model != "gemini-3.8-flash":
            first_lower = t
            break
        t += clk.slept[i] if i < len(clk.slept) else 0
    assert first_lower is not None and first_lower >= 1200


def test_never_downgrade_when_setting_says_so(monkeypatch):
    fake = FakeGemini(busy=10_000)
    _use(monkeypatch, fake)
    monkeypatch.setattr(C, "GEMINI_DOWNGRADE_AFTER_S", 99_999)
    clk = Clock()
    with pytest.raises(RuntimeError):
        shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01", sleep=clk.sleep, now=clk.now)
    assert set(fake.calls) == {"gemini-3.8-flash"}


def test_wrong_key_fails_fast(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "bad")
    monkeypatch.setattr(shadow.requests, "get", lambda url, **k: _R(403, {"error": {"status": "PERMISSION_DENIED"}}))
    clk = Clock()
    with pytest.raises(RuntimeError, match="HTTP 403"):
        shadow.gemini_forecast("file", ["Pardon"], "2026-10-01", sleep=clk.sleep, now=clk.now)
    assert clk.slept == []


def test_model_list_busy_is_retried(monkeypatch):
    fake = FakeGemini()
    _use(monkeypatch, fake)
    n = {"get": 0}
    real_get = fake.get

    def get(url, **k):
        n["get"] += 1
        if n["get"] == 1:
            return _R(503, {"error": {"status": "UNAVAILABLE"}})
        return real_get(url, **k)

    monkeypatch.setattr(shadow.requests, "get", get)
    clk = Clock()
    out = shadow.gemini_forecast("file", [w["word"] for w in WORDS], "2026-10-01", sleep=clk.sleep, now=clk.now)
    assert clk.slept == [30] and out["model"] == "gemini:gemini-3.8-flash"


def test_gemini_retry_never_refetches_news(db, monkeypatch):
    fake = FakeGemini(busy=2)
    _use(monkeypatch, fake)
    monkeypatch.setattr(shadow.time, "sleep", lambda s: None)
    calls = []
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: calls.append(1) or NEWS)
    rep = shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    assert len(calls) == 1                                       # once, for the baseline only
    g = rep["gemini:gemini-3.8-flash"]
    assert g["attempts"] == 3 and g["waited_s"] == 90
    txt = shadow.summary_text("2026-10-01", WORDS, rep)
    assert "3 tries, waited 1 min 30s" in txt
    # baseline already stored -> a later run for the missing Gemini does not touch news at all
    calls.clear()
    with store.engine().begin() as conn:
        conn.execute(store.text("delete from gap_shadow_forecasts where model like 'gemini:%'"))
    shadow.run("2026-10-01", "EVT", WORDS, PASTE)
    assert calls == []


def test_is_running_flag():
    assert shadow.is_running() is False
