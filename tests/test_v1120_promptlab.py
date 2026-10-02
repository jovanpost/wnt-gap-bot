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
from datetime import datetime

import pytest

from gap import clock, config as C, netlimit, notify, pipeline, prompt, promptlab as PL, results, shadow, store

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

    def get(self, url, headers, timeout):
        return R(200, {"models": [
            {"name": "models/gemini-3.5-pro", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]}]})

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
        if "api.x.ai" in url:
            if self.xai_status != 200:
                return R(self.xai_status, {"error": {"message": "busy"}})
            txt = self._forecast(body["input"][0]["content"], body["input"][1]["content"])
            return R(200, {"output": [{"type": "message", "content": [{"type": "output_text", "text": txt}]}],
                           "usage": {"input_tokens": 9000, "output_tokens": 3000, "cost_in_usd_ticks": 500_000_000}})
        system = body["system_instruction"]["parts"][0]["text"]
        user = body["contents"][0]["parts"][0]["text"]
        is_check = system.startswith("Reply with JSON only")
        is_writer = system.startswith("You improve a forecasting prompt") or is_check
        status = (self.pro_status if "-pro" in url else self.flash_status) if is_writer else 200
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
    monkeypatch.setattr(C, "LAB_PARALLEL", 1)
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
    assert "writer model skipped: gemini-3.5-pro: gemini-3.5-pro: HTTP 429" in "\n".join(sent)

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
    assert "gemini-3.5-pro: not usable" in out and "gemini-3.5-flash: OK" in out and "will be: gemini-3.5-flash" in out
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
