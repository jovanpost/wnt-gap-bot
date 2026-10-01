"""v1.9.0 tests: more free challengers (NVIDIA, Cerebras, Mistral, OpenRouter) via one OpenAI-compatible
client. Fake services, no network. Database tests need TEST_DATABASE_URL (throwaway local Postgres).

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json
import os

import pytest

from gap import abcfeeds, challengers as CH, config as C, netlimit, shadow, store

WORDS = [{"word": "Pardon", "market_ticker": "T-PARD"}, {"word": "SNAP / Food Stamp", "market_ticker": "T-SNAP"}]
NAMES = [w["word"] for w in WORDS]
NVIDIA_IDS = ["deepseek-ai/deepseek-coder-6.7b-instruct", "deepseek-ai/deepseek-v4.1-flash", "moonshotai/kimi-k2.6",
              "moonshotai/kimi-k3", "z-ai/glm-5.3", "z-ai/glm-5.3-flash", "nvidia/nemotron-3.5-content-safety",
              "nvidia/llama-3.1-nemotron-safety-guard-8b-v3", "qwen/qwen2.5-coder-32b-instruct", "qwen/qwen3.5-397b-a17b"]
OR_IDS = ["deepseek/deepseek-v4", "inclusionai/ling-3.0-flash-sante:free", "meta-llama/llama-4-maverick:free"]


def _answer(probs, think=False, parts=False):
    js = json.dumps({"date": "2026-10-01", "forecasts": [
        {"word": w, "probability": p, "reasoning": "Blind: x"} for w, p in zip(NAMES, probs)]})
    if think:
        js = "<think>{not json} let me think</think>\n" + js
    content = [{"type": "text", "text": js}] if parts else js
    return {"choices": [{"message": {"content": content}, "finish_reason": "stop"}]}


class R:
    def __init__(self, code, js):
        self.status_code, self._js = code, js

    def json(self):
        return self._js


class FakeAPI:
    """Fake OpenAI-compatible services. script[model] = list of status codes to answer before 200."""

    def __init__(self, script=None, probs=(40, 15), think=False):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.probs, self.think = probs, think
        self.posts, self.bodies, self.auth = [], [], []

    def get(self, url, headers=None, timeout=None, **kw):
        self.auth.append((headers or {}).get("Authorization", ""))
        if "generativelanguage" in url:
            return R(200, {"models": [{"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]}]})
        ids = OR_IDS if "openrouter" in url else (["gpt-oss-120b", "qwen-3.8-27b"] if "cerebras" in url else NVIDIA_IDS)
        return R(200, {"data": [{"id": i} for i in ids]})

    def post(self, url, headers=None, data=None, timeout=None, **kw):
        if "generativelanguage" in url:
            js = json.dumps({"date": "2026-10-01", "forecasts": [
                {"word": w, "probability": p, "reasoning": "Blind: g"} for w, p in zip(NAMES, (50, 25))]})
            return R(200, {"candidates": [{"content": {"parts": [{"text": js}]}}]})
        body = json.loads(data)
        model = body["model"]
        self.posts.append(model)
        self.bodies.append(data)
        self.auth.append((headers or {}).get("Authorization", ""))
        assert "NO web, X or browsing tools" in body["messages"][0]["content"]
        queue = self.script.get(model, [])
        if queue:
            code = queue.pop(0)
            return R(code, {"error": {"message": f"fake {code}"}})
        return R(200, _answer(self.probs, think=self.think))


class Clock:
    def __init__(self):
        self.t, self.slept = 0.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture(autouse=True)
def _base(monkeypatch):
    netlimit.reset()
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    for k in ("NVIDIA_API_KEY", "CEREBRAS_API_KEY", "MISTRAL_API_KEY", "OPENROUTER_API_KEY", "GROQ_API_KEY", "GEMINI_API_KEY"):
        monkeypatch.setattr(C, k, "")
    monkeypatch.setattr(C, "GEMINI_MODEL", "auto")
    monkeypatch.setattr(C, "GEMINI_SEARCH", False)
    monkeypatch.setattr(C, "SHADOW_ON", True)
    monkeypatch.setattr(C, "BASELINE_ON", True)


def _use(monkeypatch, fake):
    monkeypatch.setattr(CH.requests, "get", fake.get)
    monkeypatch.setattr(CH.requests, "post", fake.post)


# ---------- picking models ----------

def test_pick_model_rules():
    assert CH.pick_model("nvidia", "deepseek", NVIDIA_IDS) == "deepseek-ai/deepseek-v4.1-flash"   # coder never
    assert CH.pick_model("nvidia", "kimi", NVIDIA_IDS) == "moonshotai/kimi-k3"                     # newest
    assert CH.pick_model("nvidia", "glm", NVIDIA_IDS) == "z-ai/glm-5.3"
    assert CH.pick_model("nvidia", "qwen", NVIDIA_IDS) == "qwen/qwen3.5-397b-a17b"
    assert CH.pick_model("nvidia", "nemotron", NVIDIA_IDS) is None                                 # safety/guard never
    assert CH.pick_model("nvidia", "z-ai/glm-5.3-flash", NVIDIA_IDS) == "z-ai/glm-5.3-flash"       # exact id wins
    assert CH.pick_model("openrouter", "deepseek", OR_IDS) is None                                 # paid id never on OR
    assert CH.pick_model("openrouter", "free", OR_IDS) in ("inclusionai/ling-3.0-flash-sante:free", "meta-llama/llama-4-maverick:free")
    assert CH.pick_model("openrouter", "llama", OR_IDS) == "meta-llama/llama-4-maverick:free"


def test_enabled_only_with_keys(monkeypatch):
    monkeypatch.setattr(C, "CHALLENGERS", ["nvidia:deepseek", "cerebras:gpt-oss-120b", "mistral:mistral-medium", "bogus:x"])
    assert CH.enabled_specs() == []
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "k1")
    assert CH.enabled_specs() == [("nvidia", "deepseek")]


# ---------- one call ----------

def test_busy_then_ok_same_request(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "nv-key")
    fake = FakeAPI(script={"moonshotai/kimi-k3": [503, 429]})
    _use(monkeypatch, fake)
    clk = Clock()
    out = CH.forecast("nvidia", "moonshotai/kimi-k3", "file", NAMES, "2026-10-01", shadow.PREFACE,
                      sleep=clk.sleep, now=clk.now)
    assert out["model"] == "nvidia:moonshotai/kimi-k3" and out["attempts"] == 3 and out["waited_s"] == 90
    assert clk.slept == [30, 60] and len(set(fake.bodies)) == 1
    assert [f["probability"] for f in out["forecasts"]] == [40, 15]
    assert all(a == "Bearer nv-key" for a in fake.auth)


@pytest.mark.parametrize("code,match", [(401, "key refused"), (403, "key refused"), (404, "model not found"),
                                        (400, "HTTP 400"), (413, "HTTP 413")])
def test_hopeless_errors_fail_fast(monkeypatch, code, match):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "k")
    _use(monkeypatch, FakeAPI(script={"m": [code]}))
    clk = Clock()
    with pytest.raises(CH.Fatal, match=match):
        CH.forecast("nvidia", "m", "file", NAMES, "2026-10-01", shadow.PREFACE, sleep=clk.sleep, now=clk.now)
    assert clk.slept == []


def test_gives_up_inside_budget(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "k")
    _use(monkeypatch, FakeAPI(script={"m": [503] * 1000}))
    clk = Clock()
    with pytest.raises(RuntimeError, match="gave up after"):
        CH.forecast("nvidia", "m", "file", NAMES, "2026-10-01", shadow.PREFACE, sleep=clk.sleep, now=clk.now)
    assert sum(clk.slept) <= 1800


def test_think_tags_and_parts_are_handled(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "k")
    _use(monkeypatch, FakeAPI(think=True))
    out = CH.forecast("nvidia", "m", "file", NAMES, "2026-10-01", shadow.PREFACE, sleep=lambda s: None)
    assert [f["probability"] for f in out["forecasts"]] == [40, 15]
    assert CH._answer_text(_answer((1, 2), parts=True)).startswith("{")


def test_key_never_in_error_text(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "super-secret-key")
    _use(monkeypatch, FakeAPI(script={"m": [401]}))
    with pytest.raises(CH.Fatal) as e:
        CH.forecast("nvidia", "m", "file", NAMES, "2026-10-01", shadow.PREFACE, sleep=lambda s: None)
    assert "super-secret-key" not in str(e.value)


# ---------- run: everything side by side ----------

@pytest.fixture()
def db(monkeypatch):
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


NEWS = {"at": 0, "google": ([], {}), "abc": ([], {}, {})}


def test_run_all_challengers_side_by_side(db, monkeypatch):
    monkeypatch.setattr(C, "GEMINI_API_KEY", "g")
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "n")
    monkeypatch.setattr(C, "CEREBRAS_API_KEY", "c")
    monkeypatch.setattr(C, "CHALLENGERS", ["nvidia:deepseek", "nvidia:kimi", "cerebras:gpt-oss-120b", "mistral:x"])
    fake = FakeAPI(script={"moonshotai/kimi-k3": [404]})       # kimi gone tonight: others must still finish
    _use(monkeypatch, fake)
    monkeypatch.setattr(shadow.requests, "get", fake.get)
    monkeypatch.setattr(shadow.requests, "post", fake.post)
    calls = []
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: calls.append(1) or NEWS)
    monkeypatch.setattr(store, "grok_forecasts_for_date", lambda d: [])
    rep = shadow.run("2026-10-01", "EVT", WORDS, "file")
    assert calls == [1]                                           # news once, for the baseline only
    ok = sorted(m for m, r in rep.items() if r.get("ok"))
    assert ok == ["baseline-v1", "cerebras:gpt-oss-120b", "gemini:gemini-3.8-flash", "nvidia:deepseek-ai/deepseek-v4.1-flash"]
    assert not rep["nvidia:kimi"]["ok"] and "model not found" in rep["nvidia:kimi"]["error"]
    assert "mistral:x" not in rep                                  # no Mistral key -> not run at all
    txt = shadow.summary_text("2026-10-01", WORDS, rep, {})
    head = next(l for l in txt.splitlines() if l.startswith("word | Grok"))
    assert set(head.split(" | ")[2:]) == {"base", "gpt-oss", "Gemini", "deepseek"}
    rows = store.shadow_forecasts("2026-10-01")
    assert len(rows) == 8                                          # 4 models x 2 words
    # second run (e.g. /gap_shadow): models that answered are NOT asked again; the one that failed is
    fake.posts.clear()
    rep2 = shadow.run("2026-10-01", "EVT", WORDS, "file")
    assert fake.posts == ["moonshotai/kimi-k3"]
    assert [m for m, r in rep2.items() if r.get("ok")] == ["nvidia:moonshotai/kimi-k3"]


def test_weekly_has_average_of_ai_challengers(db, monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "n")
    monkeypatch.setattr(C, "CHALLENGERS", ["nvidia:deepseek", "nvidia:glm"])
    fake = FakeAPI()
    _use(monkeypatch, fake)
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: NEWS)
    monkeypatch.setattr(store, "grok_forecasts_for_date", lambda d: [
        {"word": "Pardon", "market_ticker": "T-PARD", "probability": 5},
        {"word": "SNAP / Food Stamp", "market_ticker": "T-SNAP", "probability": 6}])
    shadow.run("2026-10-01", "EVT", WORDS, "file")
    with store.engine().begin() as conn:
        for t, r in (("T-PARD", "yes"), ("T-SNAP", "no")):
            conn.execute(store.text("insert into gap_results (market_ticker, result) values (:t, :r)"), {"t": t, "r": r})
    lines = shadow.weekly_block("2026-09-28", "2026-10-02")
    avg = next(l for l in lines if l.startswith("avg-of-AI-challengers"))
    assert avg.split(" | ")[1] == "2"
    assert any(l.startswith("nvidia:z-ai/glm-5.3 |") for l in lines)
