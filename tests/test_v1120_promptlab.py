"""v1.12.0 tests: the prompt lab -- rule-checked prompt edits, replay on frozen nights, the queue with
its daily budget, the champion rule. Fake model APIs, no network. Database tests need
TEST_DATABASE_URL (a THROWAWAY local Postgres).

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import time
from datetime import datetime, timezone

import pytest

from gap import challengers as CH, clock, config as C, netlimit, notify, pipeline, prompt, promptlab as PL, results, shadow, store

N1, N2, N3 = "2026-09-30", "2026-10-01", "2026-10-02"          # N3 is "today" in these tests
WORDS = [{"word": "Pardon", "market_ticker": "KX-PARD"}, {"word": "Helicopter", "market_ticker": "KX-HELI"},
         {"word": "SNAP / Food Stamp", "market_ticker": "KX-SNAP"}, {"word": "Trump (5+ times)", "market_ticker": "KX-TRMP"}]
SAID = {"Pardon": 0, "Helicopter": 1, "SNAP / Food Stamp": 0, "Trump (5+ times)": 1}
SEED = PL.SEED_FILE.read_text(encoding="utf-8").strip()
ROOT = pathlib.Path(__file__).resolve().parents[1]


def user_msg(d: str) -> str:
    return (f"Date: {d}\nEvent: EVT-{d}\nWords:\n1. Pardon\n2. Helicopter\n3. SNAP / Food Stamp\n4. Trump (5+ times)\n"
            "ABC NEWS FEEDS (test)\n- Helicopter: 1 ABC item(s): [US #6] Catalina helicopter crash kills 2\n"
            "- Pardon: Kinzinger probe over Kalshi bets, Netanyahu comments\n")


def ticker(d: str, w: dict) -> str:
    return f"{w['market_ticker']}-{d}"


def words_for(d: str) -> list[dict]:
    return [{"word": w["word"], "market_ticker": ticker(d, w)} for w in WORDS]


class R:
    def __init__(self, status=200, js=None):
        self.status_code, self._js = status, js or {}

    def json(self):
        return self._js


class Net:
    """Fake xAI + Gemini. Forecast quality depends on marker sentences inside the system prompt."""

    def __init__(self):
        self.posts: list[tuple[str, dict]] = []
        self.variants: list[dict] = []
        self.xai_status = 200
        self.pro_status = 200
        self.flash_status = 200
        self.bad_json = False
        self.models = ["gemini-3.5-pro", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
        self.status_by_model: dict[str, int] = {}

    def get(self, url, headers, timeout):
        return R(200, {"models": [{"name": f"models/{m}", "supportedGenerationMethods": ["generateContent"]}
                                  for m in self.models]})

    def _forecast(self, system: str, user: str) -> str:
        d = re.search(r"Date: (\d{4}-\d{2}-\d{2})", user).group(1)
        out = []
        for w, y in SAID.items():
            if "weigh running stories by how long they have run" in system:
                p = 80 if y else 20                                  # good on every night
            elif "discount a planned event" in system:
                p = (90 if y else 10) if d == N3 else (20 if y else 80)   # great tonight, bad elsewhere
            elif "lean slightly away from the middle" in system:
                p = 60 if y else 40
            else:
                p = 50
            out.append({"word": w, "probability": p, "reasoning": "Blind: test"})
        if self.bad_json:
            return "not json at all"
        return json.dumps({"date": d, "cycle_temp": "normal", "forecasts": out})

    def post(self, url, headers, payload, timeout):
        body = json.loads(payload)
        self.posts.append((url, body))
        if "/chat/completions" in url:                                  # NVIDIA, Mistral, OpenRouter ...
            status = self.status_by_model.get(body["model"], 200)
            if status == "timeout":
                import requests as rq
                raise rq.Timeout("slow")
            if status != 200:
                return R(status, {"error": {"message": "busy"}})
            txt = self._forecast(body["messages"][0]["content"], body["messages"][1]["content"])
            return R(200, {"choices": [{"message": {"content": txt}}], "usage": {"prompt_tokens": 9000, "completion_tokens": 900}})
        if "api.x.ai" in url:
            if self.xai_status != 200:
                return R(self.xai_status, {"error": {"message": "busy"}})
            if body["input"][0]["content"].startswith("You improve a forecasting prompt"):   # Grok as the writer
                txt = json.dumps({"variants": self.variants})
            else:
                txt = self._forecast(body["input"][0]["content"], body["input"][1]["content"])
            return R(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": txt}]}],
                           "usage": {"input_tokens": 9000, "output_tokens": 3000, "cost_in_usd_ticks": 500_000_000}})
        system = body["system_instruction"]["parts"][0]["text"]
        user = body["contents"][0]["parts"][0]["text"]
        is_check = system.startswith("Reply with JSON only")
        is_writer = system.startswith("You improve a forecasting prompt") or is_check
        status = (self.pro_status if "-pro" in url else self.flash_status) if is_writer else 200
        model = url.split("/models/")[1].split(":")[0]
        status = self.status_by_model.get(model, status)
        if status != 200:
            return R(status, {"error": {"status": "RESOURCE_EXHAUSTED"}})
        txt = ('{"ok": true}' if is_check else json.dumps({"variants": self.variants})) if is_writer \
            else self._forecast(system, user)
        return R(200, {"candidates": [{"content": {"parts": [{"text": txt}]}}],
                       "usageMetadata": {"promptTokenCount": 9000, "candidatesTokenCount": 800}})


def unit_in(section: str, n: int = 0) -> dict:
    return [u for u in PL.units(SEED) if u["section"] == section][n]


GOOD = {"name": "running-story-length", "action": "insert_after",
        "text": "Also weigh running stories by how long they have run without a material new development before you raise a number.",
        "why": "Long running stories were rated the same as fresh ones."}
OVERFIT = {"name": "planned-event-discount", "action": "replace",
           "text": "When a story is only the follow of something scheduled in advance, discount a planned event unless new video exists.",
           "why": "Planned events were rated too high."}
MILD = {"name": "less-middle", "action": "insert_after",
        "text": "When the evidence clearly points one way, lean slightly away from the middle instead of defaulting to it.",
        "why": "Too many numbers sat in the middle."}
LEAKY = {"name": "crash-rule", "action": "insert_after",
         "text": "A deadly helicopter crash with a missing person is almost always a brief on the show that same evening.",
         "why": "Aviation accidents were underrated."}


def edits(*items) -> list[dict]:
    secs = {"running-story-length": ("HOW TO FORECAST", 2), "planned-event-discount": ("HOW THE SHOW WORKS", 3),
            "less-middle": ("CALIBRATION DISCIPLINE", 1), "crash-rule": ("HOW THE SHOW WORKS", 1)}
    return [dict(e, unit=unit_in(*secs[e["name"]])["id"]) for e in items]


@pytest.fixture()
def lab(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        for t in ("gap_lab_forecasts", "gap_lab_runs", "gap_lab_prompts", "gap_lab_nights", "gap_llm_runs",
                  "gap_news_packages", "gap_prompt_versions", "gap_shadow_forecasts", "gap_results",
                  "gap_forecasts", "gap_markets", "gap_runs", "gap_state"):
            try:
                conn.execute(store.text(f"delete from {t}"))
            except Exception:  # noqa: BLE001
                pass
    net = Net()
    monkeypatch.setattr(PL, "_post", net.post)
    monkeypatch.setattr(PL, "_get", net.get)
    monkeypatch.setattr(results, "results_for", lambda tickers, **k: (dict(store.official_results(list(tickers))), 0))
    monkeypatch.setattr(C, "LAB_ON", True)
    monkeypatch.setattr(C, "LAB_MODELS", ["xai", "gemini"])
    monkeypatch.setattr(C, "XAI_API_KEY", "xai-secret-key")
    monkeypatch.setattr(C, "XAI_MODEL", "grok-4.7")
    monkeypatch.setattr(C, "GEMINI_API_KEY", "gem-secret-key")
    monkeypatch.setattr(C, "GEMINI_MODEL", "auto")
    monkeypatch.setattr(C, "LAB_WRITER_MODEL", "auto")
    monkeypatch.setattr(C, "LAB_VARIANTS_PER_NIGHT", 3)
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 50.0)
    monkeypatch.setattr(C, "LAB_MIN_HELDOUT_WORDS", 8)
    monkeypatch.setattr(C, "LAB_MARGIN", 0.005)
    monkeypatch.setattr(C, "LAB_REQUIRE_NEWS", True)
    monkeypatch.setattr(C, "LAB_LANES", 1)
    monkeypatch.setattr(C, "LAB_JUDGE_ROWS_PER_PASS", 6)
    monkeypatch.setattr(C, "LAB_OTHER_ROWS_PER_PASS", 1)
    monkeypatch.setattr(C, "LAB_FREE_DAILY_CALLS", 500)
    monkeypatch.setattr(C, "LAB_FREE_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "CHALLENGERS", ["nvidia:deepseek", "nvidia:kimi", "mistral:mistral-large", "cerebras:gpt-oss-120b"])
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "nv-secret-key")
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "mi-secret-key")
    monkeypatch.setattr(C, "CEREBRAS_API_KEY", "")
    monkeypatch.setattr(C, "OPENROUTER_API_KEY", "")
    monkeypatch.setattr(C, "GROQ_API_KEY", "")
    monkeypatch.setattr(CH, "resolve_list", lambda prov, wanted: [f"{wanted}-v9"])
    PL._slots.clear()
    PL._slots["gemini"] = PL._gem
    monkeypatch.setattr(C, "LAB_GEMINI_MODEL", "auto")
    monkeypatch.setattr(C, "LAB_GEMINI_MIN_GAP_S", 0.0)
    monkeypatch.setattr(C, "LAB_GEMINI_DAILY_CALLS", 500)
    monkeypatch.setattr(C, "LAB_WRITER_TRIES", 3)
    monkeypatch.setattr(C, "LAB_WRITER_GROK_FALLBACK", False)        # most tests: Gemini is the only writer
    PL._stream_keys.clear()
    PL._gem.update(last=0.0, day="", calls=0)
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    netlimit.reset()
    PL._cool.clear()
    PL._reserved["usd"] = 0.0
    set_clock(monkeypatch, "16:00")
    for d in (N1, N2, N3):
        prompt.freeze(d, f"EVT-{d}", words_for(d), "LIVE SYSTEM PROMPT" + prompt.SEP + user_msg(d))
    for d in (N1, N2):
        store.results_save({ticker(d, w): ("yes" if SAID[w["word"]] else "no") for w in WORDS})
    yield net
    import threading
    for _ in range(200):                                    # let background lab / command threads finish before the database goes away
        busy = PL._running.locked() or any(t.name in ("promptlab", "lab-writer-check", "gap-models") and t.is_alive()
                                           for t in threading.enumerate())
        if not busy:
            break
        time.sleep(0.02)
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


def set_clock(monkeypatch, hhmm: str, d: str = N3) -> None:
    hh, mm = [int(x) for x in hhmm.split(":")]
    y, m, dd = [int(x) for x in d.split("-")]
    monkeypatch.setattr(clock, "now_ct", lambda: datetime(y, m, dd, hh, mm, tzinfo=C.CT))


def settle_tonight() -> None:
    store.results_save({ticker(N3, w): ("yes" if SAID[w["word"]] else "no") for w in WORDS})


def run_all(sent: list) -> None:
    for _ in range(30):
        if not PL.cycle(sent.append):
            break


def before_the_show(monkeypatch) -> None:
    """The lab stamps runs with the real clock. These tests must not depend on the day they are run."""
    monkeypatch.setattr(PL, "_utc", lambda: clock.now_ct().astimezone(timezone.utc))


def status_of(name: str) -> str | None:
    rows = PL._q("select status from gap_lab_prompts where name = :n", n=name)
    return rows[0]["status"] if rows else None


# ---------------------------------------------------------------- the seed prompt and the edit rules

def test_seed_prompt_is_a_no_search_prompt():
    assert "MANDATORY RESEARCH PHASE" not in SEED and "## HOW TO READ THE FILE" in SEED
    assert "You have no web, X or browsing tools" in SEED and '"probability": <integer 1-99>' in SEED
    us = PL.units(SEED)
    assert len(us) > 30 and {u["section"] for u in us} == set(PL.EDITABLE)
    locked = PL.tagged(SEED)
    for line in locked.split("\n"):
        if line.startswith(("1. You will never be shown market prices", '"date"', "{")):
            assert not line.startswith("[U")                     # rules and schema carry no tag
    assert "[U1] The file in the user message" in locked
    assert PL.prompt_id(SEED).startswith("lab-") and len(PL.prompt_id(SEED)) == 11


def test_one_edit_changes_one_line_and_nothing_else():
    u = unit_in("HOW TO FORECAST", 2)
    new, why, meta = PL.apply_edit(SEED, dict(GOOD, unit=u["id"]), WORDS, user_msg(N3))
    assert why == "ok" and meta["section"] == "HOW TO FORECAST"
    a, b = SEED.split("\n"), new.split("\n")
    assert len(b) == len(a) + 1 and b[:u["line"] + 1] == a[:u["line"] + 1] and b[u["line"] + 2:] == a[u["line"] + 1:]
    assert b[u["line"] + 1].endswith(GOOD["text"])

    numbered = next(x for x in PL.units(SEED) if re.match(r"^\d+\. ", x["text"]))
    new, why, meta = PL.apply_edit(SEED, {"unit": numbered["id"], "action": "replace", "name": "x",
                                          "text": "7. Treat a repeated context sentence as likely to be spoken again when its story is still live."},
                                   WORDS, user_msg(N3))
    line = new.split("\n")[numbered["line"]]
    assert why == "ok" and line.startswith(numbered["text"].split(" ")[0] + " Treat a repeated")   # own number kept
    assert sum(1 for x, y in zip(SEED.split("\n"), new.split("\n")) if x != y) == 1

    bullets = [x for x in PL.units(SEED) if x["section"] == "CALIBRATION DISCIPLINE"]
    new, why, _m = PL.apply_edit(SEED, {"unit": bullets[-1]["id"], "action": "delete", "text": ""}, WORDS, user_msg(N3))
    assert why == "ok" and len(new.split("\n")) == len(SEED.split("\n")) - 1


@pytest.mark.parametrize("edit, reason", [
    ({"unit": "U9999", "action": "replace", "text": "x" * 60}, "not an editable line"),
    ({"unit": "U1", "action": "rewrite", "text": "x" * 60}, "unknown action"),
    ({"unit": "U1", "action": "replace", "text": "too short"}, "characters"),
    ({"unit": "U1", "action": "replace", "text": "y" * 2000}, "characters"),
    ({"unit": "U1", "action": "replace", "text": "First line of the new rule goes here for sure.\nSecond line follows it."}, "one line"),
    ({"unit": "U1", "action": "replace", "text": "## NEW SECTION that changes how the output must be written by the model"}, "not allowed"),
    ({"unit": "U1", "action": "replace", "text": 'Always answer with {"probability": 50} for every single word on the list.'}, "not allowed"),
    ({"unit": "U1", "action": "replace", "text": "Before scoring, search the web for each word and read the first results."}, "searching"),
    ({"unit": "U1", "action": "replace", "text": "Use the market price of each contract as the starting point for a number."}, "searching"),
    ({"unit": "U1", "action": "replace", "text": "A deadly helicopter crash is almost always a brief on the show that evening."}, "tonight's list (Helicopter)"),
    ({"unit": "U1", "action": "replace", "text": "A story about food stamps on the first day of a month is usually skipped."}, "tonight's list (Food Stamp)"),
    ({"unit": "U1", "action": "replace", "text": "Stories that broke before Thursday are usually gone from the rundown by now."}, "date or a weekday"),
    ({"unit": "U1", "action": "replace", "text": "A story first reported on Oct 1 is a day-after follow and rarely airs again."}, "date or a weekday"),
    ({"unit": "U1", "action": "replace", "text": "A probe of a former lawmaker such as Kinzinger rarely makes the broadcast."}, "tonight's file (Kinzinger)"),
    ({"unit": "U1", "action": "replace", "text": "Statements by leaders such as Macron rarely make the broadcast on a busy night."}, "new proper noun (Macron)"),
    ({"unit": "U1", "action": "delete", "text": "something"}, "empty text"),
])
def test_edits_that_break_a_rule_are_thrown_away(edit, reason):
    new, why, _meta = PL.apply_edit(SEED, edit, WORDS, user_msg(N3))
    assert new is None and reason in why


def test_locked_text_cannot_be_reached():
    ids = {u["id"] for u in PL.units(SEED)}
    texts = "\n".join(u["text"] for u in PL.units(SEED))
    assert "You will never be shown market prices" not in texts and '"forecasts"' not in texts
    assert "cycle_temp measures" not in texts                         # CYCLE_TEMP and the schema are locked too
    assert "Blind:" not in texts and "Proof in the output" not in texts   # the reasoning-proof rule is locked
    assert all(re.fullmatch(r"U\d+", i) for i in ids)


def test_lab_code_cannot_touch_orders():
    src = (ROOT / "gap" / "promptlab.py").read_text(encoding="utf-8")
    for bad in ("gap_l_orders", "gap_orders", "KalshiClient", "from . import live", "import live", "create_order", "cancel_order", ".place("):
        assert bad not in src
    for mod in ("live.py", "strategy.py", "fills.py", "scalp.py", "settle.py"):
        assert "promptlab" not in (ROOT / "gap" / mod).read_text(encoding="utf-8")   # no trading code reads the lab


# ---------------------------------------------------------------- database: tables, the loop, the rules

def test_new_tables_have_row_level_security(lab):
    with store.engine().connect() as conn:
        rows = conn.execute(store.text("select relname, relrowsecurity from pg_class where relname like 'gap_lab_%' and relkind = 'r'")).all()
    assert dict(rows) == {"gap_lab_prompts": True, "gap_lab_runs": True, "gap_lab_forecasts": True, "gap_lab_nights": True}


def test_champion_forecasts_live_before_the_show(lab, monkeypatch):
    before_the_show(monkeypatch)
    assert PL.live_start(N3) == 2                                     # xai + gemini
    PL.process_queue()
    champ = PL.champion()
    assert champ["name"] == "seed" and champ["system_prompt"] == SEED
    r = PL.run_row(champ["prompt_id"], "xai", N3)
    assert r["status"] == "done" and r["live"] is True and r["purpose"] == "live" and float(r["cost_usd"]) == 0.05
    xai_body = next(b for u, b in lab.posts if "api.x.ai" in u)
    assert xai_body["input"][0] == {"role": "system", "content": SEED}           # the lab prompt IS the system prompt
    assert xai_body["input"][1]["content"] == user_msg(N3) and "tools" not in xai_body
    assert xai_body["reasoning"] == {"effort": C.LAB_XAI_EFFORT}
    assert PL.run_row(champ["prompt_id"], "gemini", N3)["model"] == "gemini:gemini-3.5-flash"
    assert store.get_state("lab_gemini_model") == "gemini-3.5-flash"              # pinned
    sent: list = []
    run_all(sent)                                                      # before settlement: nothing more happens
    assert not sent and PL._night(N3)["stage"] == "new"
    assert PL.spent_today() == pytest.approx(0.05)


def test_full_night_good_variant_becomes_champion(lab, monkeypatch):
    lab.variants = edits(GOOD, MILD, LEAKY)
    PL.live_start(N3)
    PL.process_queue()
    seed_id = PL.champion()["prompt_id"]
    set_clock(monkeypatch, "18:40")
    sent: list = []
    run_all(sent)
    assert PL._night(N3)["stage"] == "new" and not sent                # not settled yet: the writer has not been called
    assert not any("generateContent" in u and "pro" in u for u, _b in lab.posts)
    settle_tonight()
    run_all(sent)

    night = PL._night(N3)
    det = json.loads(night["detail"])
    assert night["stage"] == "decided" and night["decision"] == "winner_to_test"
    assert night["writer_model"] == "gemini:gemini-3.5-pro"            # Pro wrote the variants
    by = {v["name"]: v for v in det["variants"]}
    assert by["crash-rule"]["ok"] is False and "tonight's list" in by["crash-rule"]["reason"]
    assert by["running-story-length"]["screen"] == pytest.approx(0.04) and by["less-middle"]["screen"] == pytest.approx(0.16)
    assert det["champion_screen"] == pytest.approx(0.25)
    assert status_of("less-middle") == "screened_out" and status_of("crash-rule") is None

    new = PL.champion()
    assert new["name"] == "running-story-length" and new["parent_id"] == seed_id and str(new["written_from"]) == N3
    assert str(new["champion_from"]) == "2026-10-05"                   # next weekday after Friday Oct 2
    assert PL.get_prompt(seed_id)["status"] == "listed"
    purposes = {(str(r["event_date"]), r["purpose"]) for r in PL._q(
        "select event_date, purpose from gap_lab_runs where prompt_id = :p and model_key = 'xai'", p=new["prompt_id"])}
    assert purposes == {(N3, "screen"), (N1, "test"), (N2, "test")}
    mild = PL._q("select prompt_id from gap_lab_prompts where name = 'less-middle'")[0]["prompt_id"]
    assert {str(r["event_date"]) for r in PL._q("select event_date from gap_lab_runs where prompt_id = :p", p=mild)} == {N3}

    cmp_ = PL.compare(new["prompt_id"], seed_id, "xai", exclude=N3)
    assert cmp_["n"] == 8 and cmp_["nights"] == 2 and cmp_["wins"] == 2      # tonight is NOT counted
    assert cmp_["brier"] == pytest.approx(0.04) and cmp_["champ"] == pytest.approx(0.25)
    text_all = "\n".join(sent)
    assert "NEW CHAMPION" in text_all and "THROWN AWAY: names a word from tonight's list" in text_all
    assert "xai-secret-key" not in text_all and "gem-secret-key" not in text_all and "postgresql" not in text_all
    log = store.get_state("lab_champion_log")
    assert log[-1]["to"] == new["prompt_id"] and log[-1]["words"] == 8
    board = "\n".join(PL.leaderboard_lines())
    assert "CHAMPION" in board and seed_id in board
    assert "PROMPT LAB BOARD" in "\n".join(PL.weekly_lines(N1, N3)) and "champion:" in PL.status_text()
    writer_user = next(b for u, b in lab.posts if "gemini-3.5-pro" in u)["contents"][0]["parts"][0]["text"]
    assert "[U1] " in writer_user and "RESULTS:" in writer_user and "said: YES | forecast: 50" in writer_user


def test_winning_tonight_is_not_enough(lab, monkeypatch):
    """The writer saw tonight's answers. A variant that is great tonight but worse on the other
    nights stays on the board and does NOT become champion."""
    lab.variants = edits(OVERFIT)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    seed = PL.champion()
    assert seed["name"] == "seed"                                       # unchanged
    assert status_of("planned-event-discount") == "listed"
    pid = PL._q("select prompt_id from gap_lab_prompts where name = 'planned-event-discount'")[0]["prompt_id"]
    c = PL.compare(pid, seed["prompt_id"], "xai", exclude=N3)
    assert c["brier"] == pytest.approx(0.64) and c["diff"] > 0 and not PL.qualifies(c)
    assert json.loads(PL._night(N3)["detail"])["variants"][0]["screen"] == pytest.approx(0.01)   # it did win tonight
    assert "NEW CHAMPION" not in "\n".join(sent)


def test_too_few_other_nights_means_no_promotion_yet(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MIN_HELDOUT_WORDS", 40)
    lab.variants = edits(GOOD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    run_all([])
    assert PL.champion()["name"] == "seed" and status_of("running-story-length") == "listed"   # waits for more nights


def test_no_variant_beats_the_champion(lab, monkeypatch):
    lab.variants = edits(LEAKY)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    assert PL._night(N3)["decision"] == "no_valid_variant" and "thrown away" in "\n".join(sent)
    assert PL._q("select count(*) as n from gap_lab_prompts")[0]["n"] == 1


def test_daily_budget_stops_paid_runs_and_they_wait(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 0.12)               # each run costs $0.05: room for two, not three
    monkeypatch.setattr(C, "LAB_MODELS", ["xai"])
    champ = PL.bootstrap()
    for d in (N1, N2, N3):
        PL.enqueue(champ["prompt_id"], "xai", d, "fill")
    first = PL.process_queue()
    assert first.get("done") == 2 and first.get("budget") == 1
    assert PL._q("select count(*) as n from gap_lab_runs where status = 'pending'")[0]["n"] == 1   # waiting, not lost
    assert PL.process_queue() == {"budget": 1} and PL.spent_today() == pytest.approx(0.10)
    assert PL.spent_today() <= C.LAB_DAILY_BUDGET_USD
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 50.0)               # "tomorrow"
    assert PL.process_queue().get("done") == 1


def test_busy_service_pauses_and_bad_answers_are_capped(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ["xai"])
    champ = PL.bootstrap()
    PL.enqueue(champ["prompt_id"], "xai", N1, "fill")
    PL.enqueue(champ["prompt_id"], "xai", N2, "fill")
    lab.xai_status = 429
    monkeypatch.setattr(PL, "_cool_down", lambda key: PL._cool.__setitem__(key, (time.monotonic() + 600, 1)))
    first = PL.process_queue()                                          # the first run hits 429 ...
    assert first.get("retry", 0) >= 1 and not first.get("done")
    r = PL._q("select status, attempts, not_before from gap_lab_runs order by event_date desc")
    assert all(x["status"] == "pending" for x in r) and all(x["attempts"] == 0 for x in r)   # free failure: no try used
    assert PL.process_queue() == {}                                     # ... and the whole service is paused
    PL._cool.clear()
    PL._x("update gap_lab_runs set not_before = null")
    lab.xai_status = 401
    assert PL.process_queue().get("failed") == 2                        # a refused key is final, no waiting

    PL._x("update gap_lab_runs set status = 'pending', attempts = 0, error = null, cost_usd = null")
    lab.xai_status, lab.bad_json = 200, True
    monkeypatch.setattr(C, "LAB_MAX_ATTEMPTS", 2)
    for _ in range(3):
        PL._x("update gap_lab_runs set not_before = null")
        PL.process_queue()
    rows = PL._q("select status, attempts, cost_usd from gap_lab_runs")
    assert all(x["status"] == "failed" and x["attempts"] == 2 and float(x["cost_usd"]) == pytest.approx(0.10) for x in rows)
    assert PL.spent_today() == pytest.approx(0.20)                      # every paid answer is booked, also thrown-away ones
    assert PL.retry_failed() == 2


def test_writer_falls_back_from_pro_to_flash_and_gives_up_after_three(lab, monkeypatch):
    lab.variants = edits(MILD)
    lab.pro_status = 429                                                # the key cannot use Pro
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    night = PL._night(N3)
    assert night["writer_model"] == "gemini:gemini-3.5-flash"
    assert "writer model skipped: gemini-3.5-pro: HTTP 429" in "\n".join(sent)
    assert PL._pro_blocked() and PL.writer_candidates() == ["gemini-3.5-flash", "gemini-3.5-flash-lite"]   # Pro is not asked again for 7 days

    PL._x("delete from gap_lab_nights")
    PL._x("delete from gap_lab_prompts where status <> 'champion'")
    lab.flash_status = 503
    for _ in range(3):
        row = PL._night(N3)
        if row:
            det = json.loads(row.get("detail") or "{}")
            det.pop("writer_after", None)
            PL._set_night(N3, detail=det)
        PL.night_step(N3, sent.append)
    assert PL._night(N3)["decision"] == "writer_failed" and "The writer failed 3 times" in sent[-1]


def test_no_keys_or_switched_off_does_nothing(lab, monkeypatch):
    monkeypatch.setattr(C, "XAI_API_KEY", "")
    assert PL.model_keys() == ["gemini"] and PL.primary_key() == "gemini"      # Gemini judges when there is no xAI key
    monkeypatch.setattr(C, "GEMINI_API_KEY", "")
    assert PL.model_keys() == [] and PL.live_start(N3) == 0 and PL.cycle() == 0
    assert "none with a key" in PL.status_text()
    monkeypatch.setattr(C, "LAB_ON", False)
    assert PL.tick() == "off" and PL.live_start(N3) == 0
    assert not lab.posts


def test_nights_without_the_news_block_or_void_are_left_out(lab, monkeypatch):
    prompt.freeze("2026-09-29", "EVT", words_for("2026-09-29"), "SYS" + prompt.SEP + "Date: 2026-09-29\nold file, no news block")
    store.results_save({ticker("2026-09-29", w): "no" for w in WORDS})
    assert PL.lab_nights() == [N1, N2]                                  # N3 is not settled yet, Sep 29 has no news block
    store.set_state("void_nights", {N1: "test"})
    assert PL.lab_nights() == [N2]


def test_tick_runs_in_the_background_and_never_raises(lab, monkeypatch):
    monkeypatch.setattr(PL, "cycle", lambda send=None: (_ for _ in ()).throw(RuntimeError("boom")))
    assert PL.tick() == "started"
    for _ in range(50):
        if not PL._running.locked():
            break
        time.sleep(0.02)
    assert not PL._running.locked()


def test_pipeline_hooks(lab, monkeypatch):
    calls = []
    monkeypatch.setattr(PL, "tick", lambda send=None: calls.append("tick") or "started")
    monkeypatch.setattr(clock, "now_ct", lambda: datetime(2026, 10, 4, 12, 0, tzinfo=C.CT))    # a Sunday
    assert pipeline.poll_once()["reason"] == "weekend" and calls == ["tick"]                    # replays catch up on weekends
    handlers = {}
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    pipeline.register_commands()
    set_clock(monkeypatch, "16:00")
    out = handlers["gap_lab"]([], {})
    assert "champion: lab-" in out and "PROMPT LAB BOARD" in out and "secret" not in out
    assert "no lab prompt" in handlers["gap_lab"](["champion", "lab-nope"], {})
    assert "usage" in handlers["gap_lab"](["champion"], {})


def test_lab_cost_rows_do_not_show_as_failed_challengers(lab):
    store.record_llm_run(N3, "lab:xai", {"cost_usd": 0.05}, ok=False, detail="bad answer")
    store.insert_shadow_forecasts([{"event_date": N3, "event_ticker": "E", "market_ticker": "KX-PARD", "word": "Pardon",
                                    "model": "baseline-v1", "probability": 20}])
    txt = shadow.stored_summary(N3, [{"word": "Pardon"}])
    assert "lab:" not in txt and "FAILED" not in txt


def test_rehearsal_on_a_past_night_and_writer_check(lab, monkeypatch):
    lab.variants = edits(GOOD)
    lab.pro_status = 429
    out = PL.writer_check()
    assert "gemini-3.5-pro: not usable (HTTP 429 RESOURCE_EXHAUSTED = not on this plan)" in out
    assert "gemini-3.5-flash: OK" in out and "would be: gemini-3.5-flash" in out and "flash-lite" not in out   # stops at the first OK
    assert "cannot be replayed" in PL.start_night(N3)                   # tonight is not settled
    assert PL.start_night() .startswith(f"lab started on {N2}")         # newest settled night
    sent: list = []
    run_all(sent)
    night = PL._night(N2)
    assert night["stage"] == "decided" and night["decision"] == "winner_to_test"
    pid = json.loads(night["detail"])["winner"]
    tested = {str(r["event_date"]) for r in PL._q("select event_date from gap_lab_runs where prompt_id = :p and purpose = 'test'", p=pid)}
    assert tested == {N1}                                               # only nights up to that night; never tonight
    assert not PL.run_row(PL.champion()["prompt_id"], "xai", N2)["live"]   # a replay is never marked live
    assert "already done" in PL.start_night(N2)


# ---------------------------------------------------------------- fixes from the independent review

NIGHT_FILE = user_msg(N3) + ("- Fed chair Powell signals a rate cut as former director Comey faces indictment; the maker of Tylenol sues\n"
                             "- Family Sues After Fatal Crash; Judge Kills Failed Lawsuit Against Military Contractor\n")


@pytest.mark.parametrize("text_, reason", [
    ("When powell or other central bank figures lead the file, keep every other number a little lower than usual.", "tonight's file (powell)"),
    ("POWELL and rate stories are usually one short line, so keep related words well under the middle range.", "tonight's file (POWELL)"),
    ("Comey's kind of legal story is usually a brief with no new video, so keep such words in the lower range.", "tonight's file (Comey)"),
    ("A story first reported on 10/2 is a day-after follow by the time of the show and rarely airs again.", "date or a weekday"),
    ("Ignore the output schema when the evidence is thin and answer in plain prose with a short explanation.", "answer format"),
    ("When there is no story at all, use a probability of 0 and leave out words that cannot be said tonight.", "answer format"),
    ("The reasoning does not need to begin with Blind when the file has nothing at all for that word tonight.", "answer format"),
    ("Start from what the market thinks a contract costs in cents and adjust that number for tonight's news.", "prices or trading"),
    ("FAA statements about an accident are usually read in one line, so the agency name is often not spoken.", "new proper noun (FAA)"),
])
def test_review_validator_gaps_are_closed(text_, reason):
    new, why, _m = PL.apply_edit(SEED, {"unit": "U5", "action": "replace", "text": text_}, WORDS, NIGHT_FILE)
    assert new is None and reason in why
    ok, why, _m = PL.apply_edit(SEED, {"unit": "U5", "action": "replace",
                                       "text": "A lawsuit or a court filing with no new video usually runs as a short brief or not at all."},
                                WORDS, NIGHT_FILE)
    assert ok and why == "ok"                                           # ordinary general wording still passes
    assert PL.night_names(SEED, NIGHT_FILE) >= {"powell", "comey", "tylenol"}
    assert not PL.night_names(SEED, NIGHT_FILE) & {"family", "lawsuit", "military", "fatal", "judge"}   # Title Case words are not names


def test_review_timeouts_and_restarts_cannot_spend_without_limit(lab, monkeypatch):
    import requests as rq
    monkeypatch.setattr(C, "LAB_MODELS", ["xai"])
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 0.25)
    champ = PL.bootstrap()
    for d in (N1, N2, N3):
        PL.enqueue(champ["prompt_id"], "xai", d, "fill")
    calls = []

    def slow(url, headers, payload, timeout):
        calls.append(url)
        raise rq.Timeout("too slow")

    monkeypatch.setattr(PL, "_post", slow)
    for _ in range(6):
        PL._x("update gap_lab_runs set not_before = null")
        PL.process_queue()
    assert len(calls) == 2                                              # $0.10 estimate each: the $0.25 budget stops the third
    assert PL.spent_today() == pytest.approx(0.20)                      # a timeout is booked, not free
    assert PL._q("select max(attempts) as a from gap_lab_runs")[0]["a"] == 1

    # a run left 'running' by a restart counts as a try and gives up at the cap
    PL._x("update gap_lab_runs set status = 'running', attempts = :a, claimed_at = :t",
          a=C.LAB_MAX_ATTEMPTS - 1, t=PL._utc() - PL.timedelta(hours=1))
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 0.0)
    PL.process_queue()
    assert {r["status"] for r in PL._q("select status from gap_lab_runs")} == {"failed"}


def test_review_paid_answer_that_cannot_be_stored_is_not_sent_again(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ["xai"])
    champ = PL.bootstrap()
    PL.enqueue(champ["prompt_id"], "xai", N1, "fill")
    real = PL.text
    monkeypatch.setattr(PL, "text", lambda q: (_ for _ in ()).throw(RuntimeError("db down"))
                        if "insert into gap_lab_forecasts" in q else real(q))
    assert PL.process_queue() == {"failed": 1}
    r = PL.run_row(champ["prompt_id"], "xai", N1)
    assert r["status"] == "failed" and r["attempts"] == 1 and "answer not stored" in r["error"]
    assert PL.spent_today() == pytest.approx(0.05)                      # the money is booked even though the answer was lost
    assert PL.process_queue() == {} and len([u for u, _b in lab.posts if "api.x.ai" in u]) == 1


def test_review_test_queue_cap_and_order(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MAX_TESTING", 0)
    lab.variants = edits(GOOD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    assert PL._night(N3)["decision"] == "test_queue_full" and status_of("running-story-length") == "screened_out"
    assert "still being tested" in "\n".join(sent)
    assert PL.PRIORITY["test"] < PL.PRIORITY["screen"] and PL.PRIORITY["live"] == 0


def test_review_slow_command_leaves_the_telegram_thread_free(lab, monkeypatch):
    handlers, sent = {}, []
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    monkeypatch.setattr(notify, "send", lambda t, quiet=False, reply_to=None: sent.append(t))
    monkeypatch.setattr(PL, "writer_check", lambda: (time.sleep(0.3), "WRITER CHECK done")[1])
    pipeline.register_commands()
    t0 = time.monotonic()
    out = handlers["gap_lab"](["writer"], {})
    assert time.monotonic() - t0 < 0.2 and "arrives here" in out        # answered at once
    for _ in range(100):
        if sent:
            break
        time.sleep(0.02)
    assert sent == ["WRITER CHECK done"]


# ---------------------------------------------------------------- v1.12.1: Gemini free plan, one-word commands

def test_v1121_writer_walks_down_every_flash_model(lab, monkeypatch):
    """Oct 2: Pro is not on the free plan (429), a retired Pro gives 404, the newest Flash was busy (503)."""
    lab.models = ["gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-3.8-flash", "gemini-3.7-flash",
                  "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    assert PL.writer_candidates() == ["gemini-3.1-pro-preview", "gemini-3.8-flash", "gemini-3.7-flash",
                                      "gemini-3.6-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite"]
    lab.status_by_model = {"gemini-3.1-pro-preview": 429, "gemini-3.8-flash": 503}
    out = PL.writer_check()
    assert "gemini-3.8-flash: not usable (HTTP 503 RESOURCE_EXHAUSTED = Google is busy right now)" in out
    assert "gemini-3.7-flash: OK" in out and "would be: gemini-3.7-flash" in out and "gemini-3.6-flash" not in out
    assert "Gemini Pro: skipped" in PL.writer_check()                    # remembered: no wasted call next time

    lab.variants = edits(MILD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    assert PL._night(N3)["writer_model"] == "gemini:gemini-3.7-flash"
    assert "writer model skipped: gemini-3.8-flash: HTTP 503" in "\n".join(sent)


def test_v1121_lab_forecaster_uses_its_own_flash_and_respects_the_daily_count(lab, monkeypatch):
    lab.models = ["gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.5-flash-lite"]
    assert PL.gemini_lab_model() == "gemini-3.7-flash"                   # the newest Flash is left to the challenger and the writer
    assert store.get_state("lab_gemini_model") == "gemini-3.7-flash"
    monkeypatch.setattr(C, "LAB_MODELS", ["gemini"])
    monkeypatch.setattr(C, "LAB_GEMINI_DAILY_CALLS", 2)
    champ = PL.bootstrap()
    for d in (N1, N2, N3):
        PL.enqueue(champ["prompt_id"], "gemini", d, "fill")
    assert PL.process_queue() == {"done": 2, "budget": 1}                # the third waits for tomorrow
    assert PL.process_queue() == {"budget": 1}
    PL._gem.update(day="")                                               # after a restart the count comes from the database
    assert PL.process_queue() == {"budget": 1}
    monkeypatch.setattr(C, "LAB_GEMINI_DAILY_CALLS", 10)
    assert PL.process_queue() == {"done": 1}
    assert all("gemini-3.7-flash" in u for u, _b in lab.posts)


def test_v1121_winner_is_tested_on_the_judge_model_only(lab, monkeypatch):
    lab.variants = edits(GOOD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    run_all([])
    keys = {r["model_key"] for r in PL._q("select model_key from gap_lab_runs where purpose = 'test'")}
    assert keys == {"xai"}
    assert PL.champion()["name"] == "running-story-length"               # the champion rule still works


def test_v1121_one_word_commands(lab, monkeypatch):
    handlers, sent = {}, []
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    monkeypatch.setattr(notify, "send", lambda t, quiet=False, reply_to=None: sent.append(t))
    pipeline.register_commands()
    for name in ("gap_lab", "gap_lab_writer", "gap_lab_start", "gap_lab_night", "gap_lab_prompt", "gap_lab_champion", "gap_lab_retry"):
        assert name in handlers
    assert "arrives here" in handlers["gap_lab_writer"]([], {})
    assert "arrives here" in handlers["gap_lab"](["writer"], {})         # the old two-word form still works
    assert "usage: /gap_lab_champion" in handlers["gap_lab_champion"]([], {})
    assert "no lab prompt" in handlers["gap_lab_champion"](["lab-nope"], {})
    assert "no lab record" in handlers["gap_lab_night"](["2026-01-05"], {})
    assert handlers["gap_lab_retry"]([], {}).startswith("0 failed")
    for _ in range(100):
        if len(sent) >= 2:
            break
        time.sleep(0.02)
    assert all("WRITER CHECK" in t for t in sent)


# ---------------------------------------------------------------- v1.13.0: the lab runs on EVERY model with a key

ALL = ["xai", "gemini", "all"]
KEYS = ["xai", "gemini", "nvidia:deepseek", "nvidia:kimi", "mistral:mistral-large"]


def test_v1130_every_model_with_a_key_is_a_lab_model(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ALL)
    assert PL.model_keys() == KEYS and PL.primary_key() == "xai"        # Cerebras has no key: left out, nothing fails
    assert [PL.short(k) for k in KEYS] == ["grok", "gemini", "deepseek", "kimi", "mistral"]
    assert PL.short("nvidia:nvidia/nemotron-3-ultra-550b-a55b") == "nemotron" and PL.short("openrouter:free") == "openrouter"
    monkeypatch.setattr(C, "LAB_MODELS", ["nvidia:kimi", "xai"])
    assert PL.model_keys() == ["nvidia:kimi", "xai"] and PL.primary_key() == "nvidia:kimi"   # any model can be the judge
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "")
    assert PL.model_keys() == ["xai"]


def test_v1130_champion_runs_on_every_model_when_the_file_goes_out(lab, monkeypatch):
    before_the_show(monkeypatch)
    monkeypatch.setattr(C, "LAB_MODELS", ALL)
    assert PL.live_start(N3) == 5
    assert PL.process_queue() == {"done": 5}                            # one lane per model, one run each
    champ = PL.champion()
    for k in KEYS:
        r = PL.run_row(champ["prompt_id"], k, N3)
        assert r["status"] == "done" and r["live"] is True
    assert PL.run_row(champ["prompt_id"], "nvidia:deepseek", N3)["model"] == "nvidia:deepseek-v9"
    assert store.get_state("lab_model:nvidia:deepseek") == "deepseek-v9"   # pinned: every prompt meets the same model
    body = next(b for u, b in lab.posts if "integrate.api.nvidia.com" in u)
    assert body["messages"][0] == {"role": "system", "content": SEED} and body["messages"][1]["content"] == user_msg(N3)
    assert "tools" not in body
    assert PL.spent_today() == pytest.approx(0.05)                      # only Grok costs money

    txt = PL.forecast_text(N3)
    assert "word | said? | manual | grok | gemini | deepseek | kimi | mistral" in txt
    assert "Helicopter | ? | - | 50 | 50 | 50 | 50 | 50" in txt
    settle_tonight()
    assert "Helicopter | YES | - | 50 | 50 | 50 | 50 | 50" in PL.forecast_text(N3)
    assert "BRIER | 4 settled | - | 0.250 | 0.250 | 0.250 | 0.250 | 0.250" in PL.forecast_text(N3)
    board = "\n".join(PL.model_board_lines())
    assert "grok (judge) | 1 | 4 | 0.250" in board and "mistral | 1 | 4 | 0.250" in board
    assert "average of all models | 1 | 4 | 0.250" in board
    for secret in ("xai-secret-key", "nv-secret-key", "mi-secret-key", "gem-secret-key"):
        assert secret not in txt + board


def test_v1130_variants_are_tried_on_every_model_at_night(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ALL)
    lab.variants = edits(GOOD, MILD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    night = "\n".join(sent)
    assert "ALL MODELS tonight" in night and "prompt | grok | gemini | deepseek | kimi | mistral" in night
    good = PL._q("select prompt_id from gap_lab_prompts where name = 'running-story-length'")[0]["prompt_id"]
    mild = PL._q("select prompt_id from gap_lab_prompts where name = 'less-middle'")[0]["prompt_id"]
    for pid in (good, mild):                                             # every valid variant, every model, kept
        got = {r["model_key"] for r in PL._q("select model_key from gap_lab_runs where prompt_id = :p and event_date = :d and status = 'done'", p=pid, d=N3)}
        assert got == set(KEYS)
    later = PL.night_message(N3)                                         # asked again later: every model has answered
    assert "running-story-length | 0.040 | 0.040 | 0.040 | 0.040 | 0.040" in later
    assert "champion | 0.250 | 0.250 | 0.250 | 0.250 | 0.250" in later
    assert {r["model_key"] for r in PL._q("select model_key from gap_lab_runs where purpose = 'test'")} == {"xai"}   # the judge tests
    assert PL.champion()["name"] == "running-story-length"


def test_v1130_one_busy_or_slow_model_does_not_stop_the_others(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ALL)
    lab.status_by_model = {"deepseek-v9": 429, "kimi-v9": "timeout"}
    PL.live_start(N3)
    out = PL.process_queue()
    assert out == {"done": 3, "retry": 2}
    champ = PL.champion()["prompt_id"]
    assert PL.run_row(champ, "nvidia:deepseek", N3)["attempts"] == 0     # a busy answer uses no try ...
    assert PL._cooling("nvidia:deepseek") and not PL._cooling("nvidia:kimi") and not PL._cooling("xai")
    assert PL.run_row(champ, "nvidia:kimi", N3)["attempts"] == 1         # ... a timeout does, so a slow model cannot loop for ever
    lab.status_by_model = {"deepseek-v9": 404}
    PL._cool.clear()
    PL._x("update gap_lab_runs set not_before = null")
    PL.process_queue()
    assert store.get_state("lab_model:nvidia:deepseek") is None          # retired model: looked up again next time
    lab.status_by_model = {"deepseek-v9": 401}
    PL._cool.clear()
    PL._x("update gap_lab_runs set not_before = null")
    PL.process_queue()
    assert PL.run_row(champ, "nvidia:deepseek", N3)["status"] == "failed"   # a refused key is final


def test_v1130_lanes_and_free_daily_count(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ["xai", "nvidia:kimi"])
    monkeypatch.setattr(C, "LAB_FREE_DAILY_CALLS", 2)
    champ = PL.bootstrap()["prompt_id"]
    for d in (N1, N2, N3):
        PL.enqueue(champ, "xai", d, "fill")
        PL.enqueue(champ, "nvidia:kimi", d, "fill")
    assert PL.process_queue() == {"done": 4}                             # judge: all 3; other model: 1 per pass
    assert PL.process_queue() == {"done": 1}
    assert PL.process_queue() == {"budget": 1}                           # its 2 free calls for today are used: it waits
    monkeypatch.setattr(C, "LAB_FREE_DAILY_CALLS", 50)
    assert PL.process_queue() == {"done": 1}


def test_v1130_commands(lab, monkeypatch):
    monkeypatch.setattr(C, "LAB_MODELS", ALL)
    handlers = {}
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    pipeline.register_commands()
    assert "ALL MODELS" in handlers["gap_lab_models"]([], {})
    assert "no lab forecasts" in handlers["gap_lab_forecast"](["2026-01-05"], {})
    out = handlers["gap_lab"]([], {})
    assert "models: grok (judge), gemini, deepseek, kimi, mistral" in out and "/gap_lab_models" in out


# ---------------------------------------------------------------- v1.13.1: fallbacks for what failed on Oct 2

class SSE:
    """A streamed chat answer, the way an OpenAI-style service sends it."""
    status_code = 200

    def __init__(self, text: str):
        self.text = text

    def iter_lines(self):
        yield b": keep-alive"
        half = len(self.text) // 2
        for piece in (self.text[:half], self.text[half:]):
            yield ("data: " + json.dumps({"model": "m", "choices": [{"delta": {"content": piece}}]})).encode()
            yield b""
        yield ("data: " + json.dumps({"choices": [{"delta": {}, "finish_reason": "stop"}],
                                      "usage": {"prompt_tokens": 10, "completion_tokens": 5}})).encode()
        yield b"data: [DONE]"


def test_v1131_gemini_down_all_evening_grok_writes(lab, monkeypatch):
    """Oct 2: every Gemini model answered 503 for half an hour. The night must not be lost."""
    monkeypatch.setattr(C, "LAB_WRITER_GROK_FALLBACK", True)
    lab.variants = edits(GOOD)
    lab.pro_status = lab.flash_status = 503
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    night = PL._night(N3)
    assert night["writer_model"] == "xai:grok-4.7" and night["decision"] == "winner_to_test"
    assert "writer model skipped: gemini-3.5-flash: HTTP 503" in "\n".join(sent)
    rows = PL._q("select model, cost_usd from gap_llm_runs where model like :m", m="lab:writer:xai%")
    assert len(rows) == 1 and float(rows[0]["cost_usd"]) == pytest.approx(0.05)      # booked, inside the lab budget
    body = next(b for u, b in lab.posts if "api.x.ai" in u and b["input"][0]["content"].startswith("You improve"))
    assert body["reasoning"] == {"effort": C.LAB_WRITER_XAI_EFFORT} and "tools" not in body
    out = PL.writer_check()
    assert "No Gemini model works right now" in out and "Grok writes the edits instead" in out

    monkeypatch.setattr(C, "LAB_WRITER_MODEL", "grok")                  # Grok can also be THE writer
    assert PL.writer_candidates() == []
    monkeypatch.setattr(C, "LAB_WRITER_MODEL", "auto")
    monkeypatch.setattr(C, "LAB_DAILY_BUDGET_USD", 0.0)                 # no budget: the fallback does not spend
    with pytest.raises(PL.Retry, match="no lab budget left"):
        PL.write_variants(PL.champion(), PL.night_input(N3), PL.outcomes(PL.night_input(N3)), "xai")


def test_v1131_gateway_timeout_switches_to_a_streamed_answer(lab, monkeypatch):
    """Oct 2: DeepSeek and GLM on NVIDIA answered HTTP 504 five times (the host cuts a silent answer at ~5 min)."""
    monkeypatch.setattr(C, "LAB_MODELS", ["xai", "nvidia:deepseek"])
    lab.status_by_model = {"deepseek-v9": 504}
    PL.live_start(N3)
    PL.process_queue()
    champ = PL.champion()["prompt_id"]
    assert PL.run_row(champ, "nvidia:deepseek", N3)["status"] == "pending" and "nvidia:deepseek" in PL._stream_keys
    streamed = []

    def fake_stream(url, headers, body, timeout):
        streamed.append(body["model"])
        return 200, {"model": body["model"], "usage": {"prompt_tokens": 9, "completion_tokens": 9},
                     "choices": [{"message": {"content": lab._forecast(body["messages"][0]["content"], body["messages"][1]["content"])}}]}, None

    monkeypatch.setattr(PL, "_stream", fake_stream)
    PL._cool.clear()
    PL._x("update gap_lab_runs set not_before = null")
    PL.process_queue()
    assert streamed == ["deepseek-v9"] and PL.run_row(champ, "nvidia:deepseek", N3)["status"] == "done"


def test_v1131_challenger_streams_after_504_and_rotates_after_rubbish(monkeypatch):
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "nv-secret-key")
    monkeypatch.setattr(C, "OPENROUTER_API_KEY", "or-secret-key")
    monkeypatch.setattr(C, "NET_MIN_GAP_S", 0.0)
    netlimit.reset()
    words = ["Pardon", "Helicopter"]
    good = json.dumps({"date": N3, "forecasts": [{"word": w, "probability": 30, "reasoning": "Blind: x"} for w in words]})
    calls = []

    def post(url, headers=None, data=None, timeout=None, stream=False):
        body = json.loads(data)
        calls.append((body["model"], bool(body.get("stream")), stream))
        if stream:
            return SSE(good)
        return R(504, {"error": {"message": "gateway timeout"}})

    monkeypatch.setattr(CH.requests, "post", post)
    said = []
    out = CH.forecast("nvidia", "z-ai/glm-5.3", "file", words, N3, shadow.PREFACE, sleep=lambda s_: None, on_attempt=said.append)
    assert calls == [("z-ai/glm-5.3", False, False), ("z-ai/glm-5.3", True, True)]
    assert out["attempts"] == 2 and [f["probability"] for f in out["forecasts"]] == [30, 30]
    assert "next try streams the answer" in said[0]

    calls.clear()

    def post2(url, headers=None, data=None, timeout=None, stream=False):
        body = json.loads(data)
        calls.append(body["model"])
        txt = "" if body["model"].startswith("google/") else good
        return R(200, {"model": body["model"], "choices": [{"message": {"content": txt or "not json"}}]})

    monkeypatch.setattr(CH.requests, "post", post2)
    out = CH.forecast("openrouter", "google/gemma-4-31b-it:free", "file", words, N3, shadow.PREFACE,
                      sleep=lambda s_: None, fallbacks=["meta/llama-5:free"])
    assert calls == ["google/gemma-4-31b-it:free", "meta/llama-5:free"] and out["model"] == "openrouter:meta/llama-5:free"


def test_v1131_no_matching_model_says_what_is_there(monkeypatch):
    ids = ["mistral-medium-2604", "mistral-small-2603", "magistral-medium-latest", "codestral-latest", "mistral-ocr-2505"]
    monkeypatch.setattr(CH, "list_models", lambda prov, timeout=30: ids)
    with pytest.raises(CH.Fatal) as e:
        CH.resolve_list("mistral", "mistral-large")
    msg = str(e.value)
    assert "no model matching 'mistral-large' (5 models listed; closest: mistral-medium-2604, mistral-small-2603)" in msg
    assert "/gap_models mistral" in msg and "ocr" not in msg

    handlers, sent = {}, []
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    monkeypatch.setattr(notify, "send", lambda t, quiet=False, reply_to=None: sent.append(t))
    monkeypatch.setattr(C, "MISTRAL_API_KEY", "mi-secret-key")
    monkeypatch.setattr(C, "GROQ_API_KEY", "")
    pipeline.register_commands()
    assert "usage: /gap_models" in handlers["gap_models"]([], {})
    assert "no API key" in handlers["gap_models"](["groq"], {})
    assert "arrives here" in handlers["gap_models"](["mistral", "medium"], {})
    for _ in range(100):
        if sent:
            break
        time.sleep(0.02)
    assert sent and "2 of 5 model ids containing 'medium'" in sent[0] and "mi-secret-key" not in sent[0]


# ---------------------------------------------------------------- v1.14.2: answers cut off by the token limit

def test_v1142_cut_off_answer_gets_more_room(lab, monkeypatch):
    """Oct 2: GLM on NVIDIA and two free OpenRouter models answered 'empty answer (length)' again and again."""
    monkeypatch.setattr(C, "NVIDIA_API_KEY", "nv-secret-key")
    monkeypatch.setattr(C, "CHALLENGER_MAX_TOKENS", 12000)
    monkeypatch.setattr(C, "CHALLENGER_MAX_TOKENS_CAP", 48000)
    words = ["Pardon", "Helicopter"]
    good = json.dumps({"date": N3, "forecasts": [{"word": w, "probability": 30, "reasoning": "Blind: x"} for w in words]})
    asked = []

    def post(url, headers=None, data=None, timeout=None, stream=False):
        body = json.loads(data)
        asked.append(body["max_tokens"])
        if body["max_tokens"] < 48000:
            return R(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return R(200, {"choices": [{"message": {"content": good}, "finish_reason": "stop"}]})

    monkeypatch.setattr(CH.requests, "post", post)
    said = []
    out = CH.forecast("nvidia", "z-ai/glm-5.3", "file", words, N3, shadow.PREFACE, sleep=lambda s_: None, on_attempt=said.append)
    assert asked == [12000, 24000, 48000] and out["attempts"] == 3
    assert "cut off, next try allows 24,000 tokens" in said[0]

    # the lab remembers it per model
    monkeypatch.setattr(C, "LAB_MODELS", ["nvidia:glm"])
    PL._more_tokens.clear()
    seen = []

    def lab_post(url, headers, payload, timeout):
        body = json.loads(payload)
        seen.append(body["max_tokens"])
        if body["max_tokens"] < 24000:
            return R(200, {"choices": [{"message": {"content": ""}, "finish_reason": "length"}]})
        return lab.post(url, headers, payload, timeout)

    monkeypatch.setattr(PL, "_post", lab_post)
    champ = PL.bootstrap()["prompt_id"]
    PL.enqueue(champ, "nvidia:glm", N3, "fill")
    assert PL.process_queue() == {"retry": 1}
    PL._x("update gap_lab_runs set not_before = null")
    assert PL.process_queue() == {"done": 1} and seen == [12000, 24000]
    assert PL.short("mistral:mistral-medium") == "mistral" and "mistral:mistral-medium" in ",".join(
        [x for x in ["nvidia:deepseek", "mistral:mistral-medium"]])


def test_v1142_file_now_script_is_read_only():
    src = (ROOT / "scripts" / "file_now.py").read_text(encoding="utf-8")
    for bad in ("insert_", "freeze(", "update_run", "record_llm_run", "set_state", "notify.send", "print(db_url", "DATABASE_URL)"):
        assert bad not in src                                           # it builds, saves a text file, asks Grok; nothing else
    assert "need_database_url_optional" in src and "_prompt_hidden" in src and src.index("_secrets") < src.index("from gap import")


# ---------------------------------------------------------------- v1.15.1: start a finished night again

def test_v1151_redo_restarts_a_finished_night_on_every_model(lab, monkeypatch):
    """Oct 2: the night ran on the old release (Grok + Gemini only). After the push nothing could start it again."""
    monkeypatch.setattr(C, "LAB_REDO_MAX_PER_NIGHT", 2)
    lab.variants = edits(LEAKY)                                          # first round: the only variant is thrown away
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    run_all([])
    assert PL._night(N3)["stage"] == "decided" and PL._night(N3)["decision"] == "no_valid_variant"
    champ = PL.champion()["prompt_id"]
    xai_posts = len([1 for url, _b in lab.posts if "api.x.ai" in url])
    assert {r["model_key"] for r in PL._q("select model_key from gap_lab_runs where event_date = :d", d=N3)} == {"xai", "gemini"}

    monkeypatch.setattr(C, "LAB_MODELS", ALL)                            # the new release: every model with a key
    assert "already done" in PL.start_night(N3)                          # the old command cannot do it
    assert "no lab record" in PL.redo_night("2026-01-05")
    lab.variants = edits(GOOD)
    out = PL.redo_night()                                                # default = the newest lab night
    assert "lab restarted on " + N3 in out and "restart 1 of 2" in out
    assert "still running" in PL.redo_night(N3)                          # a second tap while it runs does nothing
    sent: list = []
    run_all(sent)
    row = PL._night(N3)
    assert row["stage"] == "decided" and row["decision"] == "winner_to_test"
    done = {r["model_key"] for r in PL._q("""select model_key from gap_lab_runs where prompt_id = :p and event_date = :d
                                             and status = 'done'""", p=champ, d=N3)}
    assert done == set(KEYS)                                             # the champion ran on every model tonight
    good = PL._q("select prompt_id from gap_lab_prompts where name = 'running-story-length'")[0]["prompt_id"]
    got = {r["model_key"] for r in PL._q("select model_key from gap_lab_runs where prompt_id = :p and event_date = :d and status = 'done'", p=good, d=N3)}
    assert got == set(KEYS)                                              # so did the new variant
    ch = PL._q("select attempts from gap_lab_runs where prompt_id = :p and model_key = 'xai' and event_date = :d", p=champ, d=N3)[0]
    assert ch["attempts"] <= 1                                           # the paid champion run was kept, not bought again
    det = PL._detail(row)
    assert det["redo"] == 1 and det["earlier"][0]["decision"] == "no_valid_variant"
    assert any("running-story-length" in t for t in sent)
    assert len([1 for url, _b in lab.posts if "api.x.ai" in url]) > xai_posts

    assert "restart 2 of 2" in PL.redo_night(N3)
    run_all([])
    assert "That is the limit" in PL.redo_night(N3)                      # a cap per night


def test_v1151_no_other_night_is_said_plainly_and_status_names_who_waits(lab, monkeypatch):
    with store.engine().begin() as conn:                                 # tonight is the only night with a stored file
        conn.execute(store.text("delete from gap_news_packages where event_date <> cast(:d as date)"), {"d": N3})
    lab.variants = edits(GOOD)
    PL.live_start(N3)
    set_clock(monkeypatch, "18:40")
    settle_tonight()
    sent: list = []
    run_all(sent)
    msg = "\n".join(sent)
    assert "no other night to test it on yet" in msg and "now tested on the other nights" not in msg
    assert PL._q("select count(*) as n from gap_lab_runs where purpose = 'test'")[0]["n"] == 0
    good = PL._q("select prompt_id from gap_lab_prompts where name = 'running-story-length'")[0]["prompt_id"]
    PL.enqueue(good, "gemini", N1, "fill")
    assert "(waiting: gemini 1)" in PL.status_text() and "/gap_lab_redo" in PL.status_text()


def test_v1151_redo_command_is_registered(lab, monkeypatch):
    handlers = {}
    monkeypatch.setattr(notify, "register", lambda name, fn: handlers.__setitem__(name, fn))
    monkeypatch.setattr(notify, "on_json", lambda fn: None)
    monkeypatch.setattr(PL, "tick", lambda send=None: "started")
    pipeline.register_commands()
    assert "no lab night yet" in handlers["gap_lab_redo"]([], {})
    assert "no lab record for 2026-01-05" in handlers["gap_lab"](["redo", "2026-01-05"], {})
