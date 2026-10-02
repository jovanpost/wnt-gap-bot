"""v1.10.0 tests: prompt lab phase A -- frozen nightly packages, prompt-tagged forecasts, the general
scorer and the nightly scorecard. Needs TEST_DATABASE_URL (a THROWAWAY local Postgres).

Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest

from gap import clock, config as C, notify, pipeline, prompt, results, scoring, shadow, store

DATE = "2026-10-02"
WORDS = [{"word": "Pardon", "market_ticker": "KX-PARD"}, {"word": "Helicopter", "market_ticker": "KX-HELI"},
         {"word": "SNAP / Food Stamp", "market_ticker": "KX-SNAP"}]
FILE = "SYSTEM PROMPT TEXT v-test" + prompt.SEP + "Date: 2026-10-02\n1. Pardon\nABC NEWS FEEDS ..."


@pytest.fixture()
def db(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        for t in ("gap_news_packages", "gap_prompt_versions", "gap_shadow_forecasts", "gap_results",
                  "gap_l_orders", "gap_forecasts", "gap_markets", "gap_orders", "gap_runs", "gap_state"):
            try:
                conn.execute(store.text(f"delete from {t}"))
            except Exception:  # noqa: BLE001
                pass
    yield
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


# ---------- frozen packages ----------

def test_package_is_frozen_once_and_cannot_be_edited(db):
    first = prompt.freeze(DATE, "EVT", WORDS, FILE)
    assert first["user_message"].startswith("Date: 2026-10-02") and first["prompt_version"] == C.PROMPT_VERSION
    second = prompt.freeze(DATE, "EVT", WORDS, "SYSTEM" + prompt.SEP + "a DIFFERENT later file")
    assert second["id"] == first["id"] and second["user_message"] == first["user_message"]
    with pytest.raises(Exception, match="cannot be edited"):    # even a direct UPDATE is refused
        with store.engine().begin() as conn:
            conn.execute(store.text("update gap_news_packages set user_message = 'hacked'"))
    assert store.get_package(DATE)["user_message"] == first["user_message"]
    with store.engine().connect() as conn:
        sp = conn.execute(store.text("select system_prompt from gap_prompt_versions")).scalar()
    assert sp == "SYSTEM PROMPT TEXT v-test"
    manual = prompt.freeze(DATE, "EVT", WORDS, FILE, kind="manual")   # other kinds live side by side
    assert manual["id"] != first["id"]


def test_rls_on_new_tables(db):
    with store.engine().connect() as conn:
        rows = conn.execute(store.text(
            "select relname, relrowsecurity from pg_class where relname in ('gap_news_packages','gap_prompt_versions')")).all()
    assert dict(rows) == {"gap_news_packages": True, "gap_prompt_versions": True}


def test_freeze_never_raises(monkeypatch):
    monkeypatch.setattr(store, "freeze_package", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    monkeypatch.setattr(store, "save_prompt_version", lambda *a, **k: None)
    assert prompt.freeze(DATE, "EVT", WORDS, FILE) is None


def test_challenger_rows_carry_prompt_and_package(db, monkeypatch):
    pkg = prompt.freeze(DATE, "EVT", WORDS, FILE)
    monkeypatch.setattr(C, "SHADOW_ON", True)
    monkeypatch.setattr(C, "BASELINE_ON", True)
    monkeypatch.setattr(C, "GEMINI_API_KEY", "")
    monkeypatch.setattr(C, "CHALLENGERS", [])
    from gap import abcfeeds
    monkeypatch.setattr(abcfeeds, "news_data", lambda words, max_age_s=None: {"at": 0, "google": ([], {}), "abc": ([], {}, {})})
    shadow.run(DATE, "EVT", WORDS, FILE, package_id=pkg["id"])
    rows = store.shadow_forecasts(DATE)
    assert rows and all(r["package_id"] == pkg["id"] and r["prompt_version"] == C.PROMPT_VERSION for r in rows)


# ---------- the scorer ----------

def test_stats_math():
    st = scoring.stats([(0.8, 1.0), (0.2, 0.0), (0.5, 1.0), (0.02, 0.0)])
    assert st["n"] == 4
    assert st["brier"] == pytest.approx((0.04 + 0.04 + 0.25 + 0.0004) / 4)
    assert st["said"] == 0.5 and st["mean_p"] == pytest.approx(0.38)
    assert st["calib"] == pytest.approx(-0.12) and st["extreme"] == 0.25
    worse = scoring.stats([(0.02, 1.0)])["logloss"]
    assert worse > scoring.stats([(0.30, 1.0)])["logloss"] * 3        # confident miss costs much more


def _seed_night(grok=(5, 8, 6), gemini=(71, 71, 20), said=("no", "yes", "no")):
    run = store.insert_run({"event_date": DATE, "event_ticker": "EVT", "status": "parsed", "prompt_version": "gap-old",
                            "harness": C.HARNESS, "word_list": WORDS, "prompt_text": FILE, "markets_n": 3})
    store.replace_forecasts(run["id"], DATE, "EVT", C.HARNESS, "gap-old", [
        {"word": w["word"], "market_ticker": w["market_ticker"], "probability": p} for w, p in zip(WORDS, grok)])
    store.insert_shadow_forecasts([{"event_date": DATE, "event_ticker": "EVT", "market_ticker": w["market_ticker"],
                                    "word": w["word"], "model": "gemini:gemini-3.5-flash", "probability": p,
                                    "prompt_version": "gap-new"} for w, p in zip(WORDS, gemini)])
    if said:
        store.results_save({w["market_ticker"]: r for w, r in zip(WORDS, said)})
    return run


def test_board_and_report(db):
    _seed_night()
    lines = scoring.report_lines(DATE, DATE, "SCORE tonight", fetch=False, per_word=True)
    text = "\n".join(lines)
    assert "settled words: 3 of 3" in text and "said: 1 of 3 (33%)" in text
    grok = next(l for l in lines if l.startswith("Grok | gap-old"))
    gem = next(l for l in lines if l.startswith("Gemini | gap-new"))
    g_brier = (0.05 ** 2 + 0.92 ** 2 + 0.06 ** 2) / 3
    m_brier = (0.71 ** 2 + 0.29 ** 2 + 0.20 ** 2) / 3
    assert f"{g_brier:.3f}" in grok and f"{m_brier:.3f}" in gem
    assert gem.split(" | ")[5] == f"{g_brier:.3f}"                 # Grok on the same words
    assert "Helicopter | YES | 8 | 71" in text
    assert "Small sample" in text


def test_report_before_settlement(db):
    _seed_night(said=None)
    text = "\n".join(scoring.report_lines(DATE, DATE, "SCORE tonight", fetch=False))
    assert "settled words: 0 of 3" in text


# ---------- nightly scorecard ----------

def _at(h, m):
    return datetime(2026, 10, 2, h, m, tzinfo=C.CT)


def test_scorecard_waits_then_sends_once(db, monkeypatch):
    _seed_night(said=None)
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    monkeypatch.setattr(clock, "weekday_ct", lambda *a: True)
    monkeypatch.setattr(results, "results_for", lambda t, budget_s=60, **k: (store.official_results(t), 0))
    sent = []
    monkeypatch.setattr(clock, "now_ct", lambda: _at(18, 0))
    assert scoring.scorecard_if_due(sent.append) == "not_due"
    monkeypatch.setattr(clock, "now_ct", lambda: _at(18, 40))
    assert scoring.scorecard_if_due(sent.append) == "waiting_results"
    store.results_save({"KX-PARD": "no", "KX-HELI": "yes", "KX-SNAP": "no"})
    assert scoring.scorecard_if_due(sent.append) == "sent"
    assert scoring.scorecard_if_due(sent.append) == "not_due"      # only once a night
    assert len(sent) == 1 and "SCORECARD tonight" in sent[0] and "LAST 4 WEEKS" in sent[0]


def test_scorecard_sends_partial_at_latest_time(db, monkeypatch):
    _seed_night(said=None)
    store.results_save({"KX-PARD": "no"})
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    monkeypatch.setattr(clock, "weekday_ct", lambda *a: True)
    monkeypatch.setattr(results, "results_for", lambda t, budget_s=60, **k: (store.official_results(t), 0))
    monkeypatch.setattr(clock, "now_ct", lambda: _at(22, 5))
    sent = []
    assert scoring.scorecard_if_due(sent.append) == "sent"
    assert "settled words: 1 of 3" in sent[0]


def test_gap_score_command(db, monkeypatch):
    _seed_night()
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    monkeypatch.setattr(results, "results_for", lambda t, budget_s=60, **k: (store.official_results(t), 0))
    handlers = {}
    orig = notify.register
    notify.register = lambda name, fn: handlers.__setitem__(name, fn)
    try:
        pipeline.register_commands()
    finally:
        notify.register = orig
    tonight = handlers["gap_score"]([], {})
    assert tonight.startswith("SCORE tonight") and "Helicopter | YES" in tonight
    week = handlers["gap_score"](["7"], {})
    assert week.startswith("SCORE last 7 days") and "Helicopter | YES" not in week


# ---------- the real send path freezes the package ----------

class FakeKalshi:
    def get_events(self, series, status="open"):
        return [{"event_ticker": "KXWORLDNEWSMENTION-26OCT02", "status": "open", "title": "Oct 2"}]

    def get_markets(self, event_ticker):
        return [{"ticker": w["market_ticker"], "status": "active", "yes_sub_title": w["word"], "title": w["word"]}
                for w in WORDS]


def test_dispatch_freezes_and_tags_challengers(db, monkeypatch):
    monkeypatch.setattr(clock, "today_ct", lambda: DATE)
    run = store.insert_run({"event_date": DATE, "event_ticker": "KXWORLDNEWSMENTION-26OCT02", "status": "detected",
                            "prompt_version": C.PROMPT_VERSION, "harness": C.HARNESS, "word_list": WORDS,
                            "prompt_text": "", "markets_n": 3})
    store.update_run(run["id"], decision_at=datetime.now(timezone.utc) - timedelta(minutes=1))
    monkeypatch.setattr(prompt, "build_paste_file", lambda d, e, w: FILE)
    monkeypatch.setattr(notify, "send_document", lambda *a, **k: 11)
    got = {}
    monkeypatch.setattr(pipeline, "_start_challengers", lambda d, e, w, p, package_id=None: got.update(pkg=package_id))
    out = pipeline.dispatch_prompt(client=FakeKalshi())
    assert out["reason"] == "sent"
    pkg = store.get_package(DATE)
    assert pkg and got["pkg"] == pkg["id"] and pkg["user_message"].startswith("Date: 2026-10-02")
