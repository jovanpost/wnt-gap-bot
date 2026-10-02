"""v1.11.0 tests: Grok through the xAI API (plain + search-enabled), usage and cost bookkeeping.
Fake API, no network. Database tests need TEST_DATABASE_URL (a THROWAWAY local Postgres).

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json
import os

import pytest

from gap import abcfeeds, clock, config as C, netlimit, notify, pipeline, shadow, store, xai

DATE = "2026-10-02"
WORDS = [{"word": "Pardon", "market_ticker": "KX-PARD"}, {"word": "SNAP / Food Stamp", "market_ticker": "KX-SNAP"}]
NAMES = [w["word"] for w in WORDS]
FILE = "SYSTEM PROMPT ... do the MANDATORY RESEARCH PHASE ...\n\n---\n\nDate: 2026-10-02\n1. Pardon\n2. SNAP / Food Stamp\n"


def _json(probs=(41, 18)):
    return json.dumps({"date": DATE, "cycle_temp": "normal", "forecasts": [
        {"word": w, "probability": p, "reasoning": "Blind: x"} for w, p in zip(NAMES, probs)]})


def _resp(text=None, ticks=1_234_000_000, tools=0, status="completed", **usage):
    u = {"input_tokens": 15000, "output_tokens": 9000, "reasoning_tokens": 6000, "cost_in_usd_ticks": ticks}
    if tools:
        u["num_server_side_tools_used"] = tools
        u["server_side_tool_usage_details"] = {"x_posts_fetched": 120}
    u.update(usage)
    return {"id": "resp_1", "status": status, "output": [
        {"type": "reasoning", "encrypted_content": "zzz"},
        {"type": "web_search_call", "status": "completed"},
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": _json() if text is None else text}]},
    ], "usage": u, "citations": ["https://a", "https://b"]}


class R:
    def __init__(self, code, js=None):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


class FakeXAI:
    def __init__(self, answers):
        self.answers = list(answers)
        self.bodies, self.headers, self.timeouts = [], [], []

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        assert url == "https://api.x.ai/v1/responses"
        self.bodies.append(json.loads(data))
        self.headers.append(headers)
        self.timeouts.append(timeout)
        a = self.answers.pop(0)
        if isinstance(a, Exception):
            raise a
        return a


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    netlimit.reset()
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "XAI_API_KEY", "xai-secret-key")
    monkeypatch.setattr(C, "XAI_MODEL", "grok-4.7")
    monkeypatch.setattr(C, "XAI_PLAIN_ON", True)
    monkeypatch.setattr(C, "XAI_EXPERT_ON", True)
    monkeypatch.setattr(C, "XAI_EFFORT", "high")
    monkeypatch.setattr(C, "XAI_EXPERT_EFFORT", "high")
    monkeypatch.setattr(C, "XAI_EXPERT_MAX_TURNS", 40)
    monkeypatch.setattr(C, "XAI_EXPERT_MAX_PAID_TRIES", 2)
    monkeypatch.setattr(C, "XAI_NIGHTLY_BUDGET_USD", 6.0)
    for k in ("GEMINI_API_KEY", "NVIDIA_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.setattr(C, k, "")
    monkeypatch.setattr(C, "SHADOW_ON", True)
    monkeypatch.setattr(C, "BASELINE_ON", False)


def _use(monkeypatch, fake):
    monkeypatch.setattr(xai.requests, "post", fake.post)


# ---------- request shapes ----------

def test_plain_body_is_like_the_other_challengers():
    b = xai.build_body("plain", FILE, DATE, shadow.PREFACE)
    assert b["model"] == "grok-4.7" and "tools" not in b and "max_turns" not in b
    assert [m["role"] for m in b["input"]] == ["system", "user"]
    assert "NO web, X or browsing tools" in b["input"][0]["content"] and b["input"][1]["content"] == FILE
    assert b["reasoning"] == {"effort": "high"}


def test_expert_body_is_the_manual_step():
    b = xai.build_body("expert", FILE, DATE, shadow.PREFACE)
    assert b["input"] == [{"role": "user", "content": FILE}]                 # the file, alone, exactly as pasted
    assert b["tools"] == [{"type": "web_search"}, {"type": "x_search", "from_date": "2026-09-29"}]
    assert b["max_turns"] == 40 and b["reasoning"] == {"effort": "high"}
    assert xai.label("plain") == "xai:grok-4.7" and xai.label("expert") == "xai:grok-4.7+search"


def test_enabled_modes(monkeypatch):
    assert xai.enabled_modes() == ["plain", "expert"]
    monkeypatch.setattr(C, "XAI_EXPERT_ON", False)
    assert xai.enabled_modes() == ["plain"]
    monkeypatch.setattr(C, "XAI_API_KEY", "")
    assert xai.enabled_modes() == []


# ---------- reading the answer ----------

def test_answer_text_and_usage():
    r = _resp(tools=38)
    assert xai.answer_text(r).startswith('{"date"')
    u = xai.usage_of(r)
    assert u["cost_usd"] == pytest.approx(0.1234) and u["tool_calls"] == 38 and u["x_posts"] == 120
    assert (u["input_tokens"], u["output_tokens"], u["reasoning_tokens"], u["citations"]) == (15000, 9000, 6000, 2)
    nested = {"output": [{"type": "message", "content": [{"type": "output_text", "text": "x"}]}],
              "usage": {"input_tokens": 1, "output_tokens": 2, "output_tokens_details": {"reasoning_tokens": 7},
                        "input_tokens_details": {"cached_tokens": 3}}}
    u2 = xai.usage_of(nested)
    assert u2["reasoning_tokens"] == 7 and u2["cached_tokens"] == 3 and u2["cost_usd"] is None
    with pytest.raises(ValueError, match="empty answer"):
        xai.answer_text({"status": "incomplete", "output": [{"type": "reasoning"}]})
    line = xai.cost_line(u)
    assert line.startswith("$0.12, 15,000 in / 9,000 out (6,000 thinking), 38 tool calls, 120 X posts")
    assert xai.cost_line({"cost_usd": 0.0038}) == "$0.0038" and xai.cost_line(None) == ""


# ---------- the call ----------

def test_plain_success(monkeypatch):
    fake = FakeXAI([R(200, _resp())])
    _use(monkeypatch, fake)
    g = xai.forecast("plain", FILE, NAMES, DATE, shadow.PREFACE, sleep=lambda s: None)
    assert g["model"] == "xai:grok-4.7" and [f["probability"] for f in g["forecasts"]] == [41, 18]
    assert g["usage"]["cost_usd"] == pytest.approx(0.1234) and g["attempts"] == 1
    assert fake.headers[0]["Authorization"] == "Bearer xai-secret-key" and fake.timeouts[0] == C.XAI_TIMEOUT_S


def test_busy_is_retried_with_the_same_request(monkeypatch):
    fake = FakeXAI([R(429, {"error": {"message": "rate limited"}}), R(503, {"error": "overloaded"}), R(200, _resp())])
    _use(monkeypatch, fake)
    slept = []
    g = xai.forecast("expert", FILE, NAMES, DATE, shadow.PREFACE, sleep=slept.append)
    assert slept == [30, 60] and g["attempts"] == 3 and fake.bodies[0] == fake.bodies[2]
    assert g["usage"]["cost_usd"] == pytest.approx(0.1234)                   # busy answers cost nothing


def test_search_run_is_never_resent_after_a_timeout(monkeypatch):
    fake = FakeXAI([xai.requests.Timeout(), R(200, _resp())])
    _use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="NOT re-sent"):
        xai.forecast("expert", FILE, NAMES, DATE, shadow.PREFACE, sleep=lambda s: None)
    assert len(fake.bodies) == 1 and fake.timeouts[0] == C.XAI_EXPERT_TIMEOUT_S
    fake2 = FakeXAI([xai.requests.Timeout(), R(200, _resp())])               # the cheap plain run may retry
    _use(monkeypatch, fake2)
    assert xai.forecast("plain", FILE, NAMES, DATE, shadow.PREFACE, sleep=lambda s: None)["attempts"] == 2


def test_broken_answers_stop_after_two_paid_tries_and_report_the_spend(monkeypatch):
    fake = FakeXAI([R(200, _resp(text="sorry, no JSON", ticks=20_000_000_000)),
                    R(200, _resp(text="still not JSON", ticks=15_000_000_000)), R(200, _resp())])
    _use(monkeypatch, fake)
    with pytest.raises(RuntimeError, match="stopped after 2 paid") as e:
        xai.forecast("expert", FILE, NAMES, DATE, shadow.PREFACE, sleep=lambda s: None)
    assert e.value.usage["cost_usd"] == pytest.approx(3.5) and len(fake.bodies) == 2
    assert "xai-secret-key" not in str(e.value)


def test_max_turns_refused_then_sent_without(monkeypatch):
    fake = FakeXAI([R(400, {"error": {"message": "Unknown parameter: max_turns"}}), R(200, _resp())])
    _use(monkeypatch, fake)
    msgs = []
    g = xai.forecast("expert", FILE, NAMES, DATE, shadow.PREFACE, sleep=lambda s: None, on_attempt=msgs.append)
    assert "max_turns" in fake.bodies[0] and "max_turns" not in fake.bodies[1] and g["attempts"] == 2
    assert "max_turns was not accepted" in msgs[0]


@pytest.mark.parametrize("code,match", [(401, "key refused"), (403, "key refused"), (400, "HTTP 400"), (402, "HTTP 402")])
def test_hopeless_errors_fail_at_once(monkeypatch, code, match):
    _use(monkeypatch, FakeXAI([R(code, {"error": {"message": "nope"}})]))
    slept = []
    with pytest.raises(xai.Fatal, match=match):
        xai.forecast("plain", FILE, NAMES, DATE, shadow.PREFACE, sleep=slept.append)
    assert slept == []


# ---------- inside the nightly challenger run ----------

@pytest.fixture()
def db(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        for t in ("gap_llm_runs", "gap_shadow_forecasts", "gap_results"):
            conn.execute(store.text(f"delete from {t}"))
    monkeypatch.setattr(store, "grok_forecasts_for_date", lambda d: [])
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: {"at": 0, "google": ([], {}), "abc": ([], {}, {})})
    yield
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


class ByMode:
    """Answers by request shape: search requests carry tools."""

    def __init__(self, plain, expert):
        self.plain, self.expert, self.calls = list(plain), list(expert), []

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        body = json.loads(data)
        mode = "expert" if "tools" in body else "plain"
        self.calls.append(mode)
        a = (self.expert if mode == "expert" else self.plain).pop(0)
        if isinstance(a, Exception):
            raise a
        return a


def test_run_saves_both_groks_with_cost_and_asks_once(db, monkeypatch):
    fake = ByMode([R(200, _resp(ticks=900_000_000))], [R(200, _resp(text=_json((60, 25)), ticks=21_000_000_000, tools=44))])
    _use(monkeypatch, fake)
    msgs = []
    rep = shadow.run(DATE, "EVT", WORDS, FILE, on_attempt=msgs.append)
    assert sorted(m for m, r in rep.items() if r.get("ok")) == ["xai:grok-4.7", "xai:grok-4.7+search"]
    rows = store.shadow_forecasts(DATE)
    assert {(r["model"], r["word"]): r["probability"] for r in rows} == {
        ("xai:grok-4.7", "Pardon"): 41, ("xai:grok-4.7", "SNAP / Food Stamp"): 18,
        ("xai:grok-4.7+search", "Pardon"): 60, ("xai:grok-4.7+search", "SNAP / Food Stamp"): 25}
    assert all(r["prompt_version"] == C.PROMPT_VERSION for r in rows)
    assert store.llm_spend(DATE, "xai:") == pytest.approx(2.19)
    assert any(m.startswith("done: xai:grok-4.7+search") and "$2.10" in m and "44 tool calls" in m for m in msgs)
    txt = shadow.summary_text(DATE, WORDS, rep, {})
    assert "grokAPI" in txt and "grokWeb" in txt and "| $2.10" in txt
    stored = shadow.stored_summary(DATE, WORDS)
    assert "| $2.10" in stored and "| $0.09" in stored
    rep2 = shadow.run(DATE, "EVT", WORDS, FILE)                               # second run: nothing asked, nothing spent
    assert fake.calls.count("expert") == 1 and fake.calls.count("plain") == 1
    assert not any(r.get("ok") for r in rep2.values())


def test_failed_search_run_is_booked_and_the_budget_stops_the_next_one(db, monkeypatch):
    monkeypatch.setattr(C, "XAI_PLAIN_ON", False)
    bad = R(200, _resp(text="not json", ticks=35_000_000_000))
    fake = ByMode([], [bad, R(200, _resp(text="not json", ticks=35_000_000_000))])
    _use(monkeypatch, fake)
    monkeypatch.setattr(xai.time, "sleep", lambda s: None)
    rep = shadow.run(DATE, "EVT", WORDS, FILE)
    r = rep["xai:grok-4.7+search"]
    assert not r["ok"] and "stopped after 2 paid" in r["error"] and r["usage"]["cost_usd"] == pytest.approx(7.0)
    assert store.llm_spend(DATE, "xai:") == pytest.approx(7.0)               # the failed run is on the books
    assert "FAILED" in shadow.summary_text(DATE, WORDS, rep) and "spent $7.00" in shadow.summary_text(DATE, WORDS, rep)
    rep2 = shadow.run(DATE, "EVT", WORDS, FILE)                               # over the $6 nightly limit: not started
    assert "not started" in rep2["xai:grok-4.7+search"]["error"] and len(fake.calls) == 2


def test_no_key_means_no_xai_jobs(db, monkeypatch):
    monkeypatch.setattr(C, "XAI_API_KEY", "")
    monkeypatch.setattr(xai.requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not call")))
    rep = shadow.run(DATE, "EVT", WORDS, FILE)
    assert not any(k.startswith("xai:") for k in rep)


def test_cost_report_and_command(db, monkeypatch):
    store.record_llm_run(DATE, "xai:grok-4.7+search", {"cost_usd": 2.1, "input_tokens": 180000, "output_tokens": 12000,
                                                       "tool_calls": 44}, seconds=420, attempts=1)
    store.record_llm_run(DATE, "xai:grok-4.7", {"cost_usd": 0.09, "input_tokens": 15000, "output_tokens": 9000}, seconds=70)
    lines = shadow.cost_lines(DATE, DATE)
    assert lines[0] == f"MODEL COST ({DATE}): $2.19 in total"
    assert lines[2].startswith("xai:grok-4.7+search | 1 (1) | $2.10 | $2.10 | 180,000 / 12,000 | 44 | 420")
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    handlers = {}
    orig = notify.register
    notify.register = lambda name, fn: handlers.__setitem__(name, fn)
    try:
        pipeline.register_commands()
    finally:
        notify.register = orig
    assert handlers["gap_cost"]([], {}).startswith("MODEL COST tonight")
    assert handlers["gap_cost"](["7"], {}).startswith("MODEL COST last 7 days")
    assert shadow.cost_lines("2026-01-01", "2026-01-02")[0].startswith("MODEL COST: no model usage")


def test_scoring_names():
    assert shadow.short_name("xai:grok-4.7") == "grokAPI" and shadow.short_name("xai:grok-4.7+search") == "grokWeb"
