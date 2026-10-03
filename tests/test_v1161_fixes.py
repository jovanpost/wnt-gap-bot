"""v1.16.1 tests: the SCALP status counts every round trip; the 2x2 replay (prompt x file) and its
script; the lab's name check no longer treats outlet names as story names.
Database tests need TEST_DATABASE_URL (a THROWAWAY local Postgres). No network, no real model.
Run:  python3 -m pytest -q tests
"""
from __future__ import annotations

import io
import json
import os
import pathlib
import runpy
import sys
from contextlib import redirect_stdout

import pytest

from gap import config as C, lab, prompt, promptlab as PL, replay, store, xai

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- SCALP status

def batch(word: str, status: str, buy: int, sell: int, n: float = 100, buy_fee: int = 100, sell_fee: int = 100) -> dict:
    cost, proceeds = int(buy * n), int(sell * n)
    return {"market_ticker": "KX-" + word, "word": word, "status": status, "event_date": "2026-09-29",
            "buy_at": "2026-09-29T18:00:00+00:00", "buy_price_cents": buy, "buy_contracts": n, "buy_cost_cents": cost,
            "buy_fee_cents": buy_fee, "sell_price_cents": sell, "sell_contracts": n, "sell_proceeds_cents": proceeds,
            "sell_fee_cents": sell_fee, "net_cents": proceeds - sell_fee - cost - buy_fee}


def test_v1161_scalp_status_counts_fallback_sells_and_every_fee(monkeypatch):
    """Oct 3: SCALP read "PASSING" with +$7 on $856. The margin looked only at the sells that hit the target."""
    monkeypatch.setattr(lab, "SCALP_MIN_FILLED", 30)
    hits = [batch(f"W{i % 10}", "scalp_hit", 80, 90) for i in range(20)]            # +10c each before fees
    falls = [batch(f"W{i % 10}", "fallback_sold", 80, 62) for i in range(10)]       # -18c each before fees
    cum = lab.scalp_stats(hits + falls)
    assert cum["done"] == 30 and cum["scalp_hit"] == 20 and cum["fallback_sold"] == 10
    assert cum["avg_hit_px"] == pytest.approx(90.0)                      # the old "avg sell" (winners only)
    assert cum["avg_sell_px"] == pytest.approx((20 * 90 + 10 * 62) / 30)  # every exit: 80.67
    assert cum["break_even_px"] == pytest.approx(82.0)                   # 80 + 1c buy fee + 1c sell fee
    assert cum["margin"] == pytest.approx(cum["avg_sell_px"] - cum["break_even_px"])
    assert cum["margin"] == pytest.approx(cum["net"] * 100 / 3000)       # = cents kept per contract
    status = lab.scalp_status(cum)
    assert status.startswith("FAILING") and "all 30 round trips" in status   # old math: 90 - 81 = +9 -> "PASSING"

    even = lab.scalp_stats([batch(f"W{i % 10}", "scalp_hit", 80, 90) for i in range(24)]
                           + [batch(f"W{i % 10}", "fallback_sold", 80, 58) for i in range(6)])
    assert 0 < even["margin"] < 5 and lab.scalp_status(even).startswith("UNCLEAR") and "needs +5c" in lab.scalp_status(even)

    good = lab.scalp_stats([batch(f"W{i % 10}", "scalp_hit", 80, 92) for i in range(30)])
    assert good["margin"] == pytest.approx(10.0) and lab.scalp_status(good).startswith("PASSING")

    lucky = lab.scalp_stats([batch("AI", "scalp_hit", 40, 90) for _ in range(8)]
                            + [batch(f"W{i % 8}", "fallback_sold", 80, 76) for i in range(22)])
    s = lab.scalp_status(lucky)
    assert lucky["margin"] >= 5 and lucky["best_word"] == "AI" and lucky["net_ex_best"] < 0
    assert s.startswith("UNCLEAR") and "one word carries it: without AI" in s

    early = lab.scalp_stats(hits[:5])
    assert lab.scalp_status(early).startswith("TOO EARLY")
    assert lab.scalp_stats([])["margin"] is None and lab.scalp_status(lab.scalp_stats([])).startswith("TOO EARLY")
    week = lab.scalp_by_week(hits + falls)                                # the weekly rows use the same numbers
    assert week[-1]["week"] == "CUMULATIVE" and week[-1]["margin"] == pytest.approx(cum["margin"])


# ---------------------------------------------------------------- the lab's name check

def test_v1161_outlet_names_are_not_story_names():
    """Oct 2: a good variant was thrown away for 'uses a name from tonight's file (local)'."""
    night = ("Date: 2026-10-02\nABC NEWS FEEDS (test)\n"
             "    • Flood warning extended for the river valley — WKRC Local 12 (Oct 02 10:27 AM CT)\n"
             "    • Divers find the last body near Catalina island — Savannah Morning News (Oct 02 8:18 AM CT)\n"
             "    • A new Regional rule for hospitals starts this week — Reuters (Oct 02 9:00 AM CT)\n")
    seed = PL.SEED_FILE.read_text(encoding="utf-8")
    names = PL.night_names(seed, night)
    assert "catalina" in names                                           # a real story name is still a name
    assert not ({"local", "morning", "savannah", "regional"} & names)    # outlets and ordinary words are not
    words = [{"word": "Helicopter", "market_ticker": "KX-H"}]
    ok = "Treat a local story with no national angle as an index item at best, however dramatic its pictures are."
    assert PL._leak(ok, seed, words, night) is None
    assert "catalina" in (PL._leak("A crash near catalina is always a lead story on the same evening it happens.", seed, words, night) or "")
    assert "Savannah" in (PL._leak("Stories first reported by Savannah outlets usually stay regional and do not air.", seed, words, night) or "")


# ---------------------------------------------------------------- 2x2 replay helpers

WORDS = [{"word": "Pardon", "market_ticker": "KX-PARD"}, {"word": "Helicopter", "market_ticker": "KX-HELI"},
         {"word": "SNAP / Food Stamp", "market_ticker": "KX-SNAP"}, {"word": "Romo", "market_ticker": "KX-ROMO"}]
SAID = {"Pardon": 0, "Helicopter": 1, "SNAP / Food Stamp": 0, "Romo": 1}
HIST = "\nWORD HISTORY (official Kalshi results, newest night first, last 2 nights)\n- Pardon: N N"
SHOWS = "\nPREVIOUS BROADCASTS (the last 2 World News Tonight show(s), from ABC's own segment list on YouTube;\n  1. A story (2:10)"
ABC = ("\nABC NEWS FEEDS (ABC's own RSS, fetched by the bot at 12:14 PM CT; numbered)\n- Romo: 1 ABC item(s)\n"
       "ABC Front Page:\n  13. Tony Romo parting ways with CBS Sports")
MORE = ("\nOTHER NETWORKS AND WIRES (fetched once by the bot at 12:14 PM CT; first 20 items per feed, last 36h).\n- Romo: 3 headline(s)\n\n"
        "GOOGLE NEWS US TOP STORIES (homepage context, once; not a search of the word list):\n  1. Something — NBC News")
HEADS = "\nGOOGLE NEWS HEADLINES (fetched by the bot at 12:14 PM CT; Google News search, last 24 hours, raw).\n- Romo:\n    • Romo out at CBS — USA Today"


def user_for(d: str, full: bool = True) -> str:
    words = [{"word": w["word"], "market_ticker": f"{w['market_ticker']}-{d}"} for w in WORDS]
    return prompt.build_user_message(d, f"EVT-{d}", words, HIST, HEADS, ABC if full else "", MORE if full else "", SHOWS if full else "")


def test_v1161_old_style_file_drops_only_the_new_blocks():
    full = user_for("2026-10-02")
    assert replay.blocks_in(full) == ["history", "broadcasts", "ABC feeds", "other networks", "top stories", "Google headlines"]
    light = replay.old_style(full)
    assert replay.blocks_in(light) == ["history", "Google headlines"]
    for gone in ("PREVIOUS BROADCASTS (", "ABC NEWS FEEDS (", "ABC Front Page:", "OTHER NETWORKS AND WIRES (", "GOOGLE NEWS US TOP STORIES"):
        assert gone not in light
    for kept in ("Date: 2026-10-02", "1. Pardon", "4. Romo", "WORD HISTORY (", "- Pardon: N N", "GOOGLE NEWS HEADLINES (",
                 "Romo out at CBS", "First do the MANDATORY RESEARCH PHASE", "every reasoning starting with 'Blind: ...'."):
        assert kept in light
    assert replay.old_style(light) == light                              # nothing more to drop
    old_era = user_for("2026-09-25", full=False)
    assert replay.old_style(old_era).strip() == old_era.strip()          # an old file is left as it is
    assert replay.file_date(full) == "2026-10-02" and replay.file_date("no date here") is None
    assert replay.old_style("plain text") == "plain text"


def test_v1161_cell_numbers():
    truth = {"A": 1.0, "B": 0.0, "C": 0.0, "D": 0.0}
    st = replay.cell_stats({"A": 80, "B": 20, "C": 40, "D": 30, "E": 99}, truth)       # E has no result: left out
    assert st["n"] == 4 and st["said"] == 1
    assert st["brier"] == pytest.approx((0.04 + 0.04 + 0.16 + 0.09) / 4)
    assert st["avg_said"] == 80 and st["avg_not_said"] == pytest.approx(30.0)
    assert st["low"] == 2 and st["low_said"] == 0                         # B and D are at 30 or below; neither was said
    assert replay.cell_stats({"A": 25}, truth)["low_said"] == 1           # a said word at 25 = a Book L loss
    assert replay.mean_forecast([{"A": 80, "B": 20}, {"A": 60}]) == {"A": 70, "B": 20}
    both = replay.pooled([({"A": 80}, {"A": 1.0}), ({"A": 80}, {"A": 0.0})])
    assert both["n"] == 2 and both["brier"] == pytest.approx((0.04 + 0.64) / 2)   # the same word on two nights counts twice
    assert replay.fmt(None) == "-" and replay.fmt(0.1234) == "0.123"


# ---------------------------------------------------------------- the script, end to end, with a fake Grok

OLD_PROMPT = "OLD PROMPT: weigh a running story by how long it has run."
N1, N2 = "2026-10-01", "2026-10-02"


@pytest.fixture()
def db(monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    monkeypatch.setattr(C, "DATABASE_URL", url)
    monkeypatch.setattr(store, "_engine", None)
    store.init_db()
    with store.engine().begin() as conn:
        for t in ("gap_lab_forecasts", "gap_lab_runs", "gap_lab_prompts", "gap_lab_nights", "gap_llm_runs", "gap_news_packages",
                  "gap_prompt_versions", "gap_shadow_forecasts", "gap_results", "gap_forecasts", "gap_markets", "gap_runs", "gap_state"):
            try:
                conn.execute(store.text(f"delete from {t}"))
            except Exception:  # noqa: BLE001
                pass
    yield url
    store.engine().dispose()
    monkeypatch.setattr(store, "_engine", None)


def seed_nights() -> None:
    for d, full in ((N1, True), (N2, True), ("2026-09-25", False)):
        words = [{"word": w["word"], "market_ticker": f"{w['market_ticker']}-{d}"} for w in WORDS]
        store.insert_run({"event_date": d, "event_ticker": f"EVT-{d}", "status": "scored",
                          "prompt_version": "gap-old1234" if d != N2 else "gap-new5678", "harness": "grok-web-expert",
                          "word_list": words, "prompt_text": (OLD_PROMPT if d != N2 else "NEW PROMPT") + prompt.SEP + user_for(d, full),
                          "markets_n": len(words)})
        store.results_save({w["market_ticker"]: ("yes" if SAID[w["word"]] else "no") for w in words})


def fake_forecast(mode, paste, words, event_date, preface, **_k):
    """Old prompt: good. New prompt: every number 25 higher. Full file: finds Romo (said) but lifts SNAP (not said)."""
    assert mode == "plain"
    old = paste.startswith("OLD PROMPT")
    full = "ABC NEWS FEEDS (" in paste
    out = []
    for w in words:
        p = 70 if SAID[w] else 20
        if w == "Romo" and not full:
            p = 20
        if w == "SNAP / Food Stamp" and full:
            p = 45
        if not old:
            p = min(99, p + 25)
        out.append({"word": w, "probability": p, "reasoning": "Blind: test"})
    return {"model": "xai:grok-test", "forecasts": out, "seconds": 1.0, "attempts": 1, "waited_s": 0.0,
            "usage": {"cost_usd": 0.17, "input_tokens": 100, "output_tokens": 50}}


def run_script(monkeypatch, tmp_path, *args) -> str:
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("XAI_API_KEY", "xai-secret-key")
    monkeypatch.setattr(C, "XAI_API_KEY", "xai-secret-key")
    monkeypatch.setattr(C, "XAI_EFFORT", "high")
    monkeypatch.setattr(xai, "forecast", fake_forecast)
    monkeypatch.setattr(sys, "argv", ["replay_2x2.py", "--out", str(tmp_path), *args])
    buf = io.StringIO()
    with redirect_stdout(buf):
        try:
            runpy.run_path(str(ROOT / "scripts" / "replay_2x2.py"), run_name="__main__")
        except SystemExit as exc:
            buf.write(f"\nEXIT {exc.code}")
    return buf.getvalue()


def counts() -> dict:
    out = {}
    with store.engine().connect() as conn:
        for t in ("gap_runs", "gap_results", "gap_news_packages", "gap_prompt_versions", "gap_shadow_forecasts",
                  "gap_llm_runs", "gap_lab_runs", "gap_lab_forecasts", "gap_state", "gap_forecasts"):
            out[t] = conn.execute(store.text(f"select count(*) from {t}")).scalar()
    return out


def test_v1161_replay_script_end_to_end(db, monkeypatch, tmp_path):
    seed_nights()
    before = counts()
    monkeypatch.setattr(C, "prompt_text", lambda: "NEW PROMPT: lean on the news blocks.")
    monkeypatch.setattr(C, "prompt_version", lambda: "gap-new5678")

    listing = run_script(monkeypatch, tmp_path, "--list")
    assert "EXIT 0" in listing and f"{N2} | 4 | 2 | gap-new5678" in listing and "ABC feeds" in listing
    assert "2026-09-25 | 4 | 2 | gap-old1234" in listing and "2 of these have the new news blocks" in listing
    assert "about $" not in listing                                       # --list never offers to spend

    out = run_script(monkeypatch, tmp_path, "--yes", "--old", "gap-old1234")
    assert "EXIT 0" in out, out[-2000:]
    assert "old prompt gap-old1234:" in out and "from the Grok file of 2026-10-01" in out     # found in a stored night's file
    assert "This sends 8 runs" in out and "about $1.36" in out            # 2 nights x 4 cells
    assert f"ALL NIGHTS WITH BOTH FILES ({N1}, {N2})" in out
    # old prompt + old-style file: Pardon 20, Heli 70, SNAP 20, Romo 20 (said) -> (0.04+0.09+0.04+0.64)/4
    assert "gap-old1234 + old-style file | 8 | 4 | 0.203 | 45 | 20 | 6 | 2" in out
    # old prompt + full file: Romo 70, SNAP 45 -> (0.04+0.09+0.2025+0.09)/4
    # new prompt = every number 25 higher: old-style 0.7100/4, full 0.6975/4
    assert "gap-old1234 + full file | 8 | 4 | 0.106 | 70 | 32 | 2 | 0" in out
    assert "gap-new5678 + old-style file | 8 | 4 | 0.178 | 70 | 45 | 0 | 0" in out
    assert "gap-new5678 + full file | 8 | 4 | 0.174 | 95 | 58 | 0 | 0" in out
    assert "PROMPT effect (new minus old, both files averaged): +0.022 Brier" in out
    assert "FILE effect (full minus old-style, both prompts averaged): -0.050 Brier" in out
    assert "Romo | YES | 20 | 70 | 45 | 95" in out and "spent: $1.36 on 8 run(s)" in out
    saved = [p for p in tmp_path.iterdir() if p.name.startswith("gap-replay-2x2-")]
    assert len(saved) == 1 and "PROMPT effect" in saved[0].read_text(encoding="utf-8")
    assert "nothing was written to the database" in out and counts() == before          # read only
    for secret in ("xai-secret-key", "postgresql://", os.environ["TEST_DATABASE_URL"]):
        assert secret not in out and secret not in listing

    # an old-era night has one file only: two runs, prompt effect only
    one = run_script(monkeypatch, tmp_path, "--yes", "--old", "gap-old1234", "--nights", "2026-09-25")
    assert "one file only (it has none of the new blocks): prompt effect only" in one and "This sends 2 runs" in one
    assert "ALL NIGHTS WITH BOTH FILES" not in one and "NIGHT 2026-09-25" in one

    # a saved preview file replaces the stored file of its date
    path = tmp_path / "gap-2026-10-02-1528-preview.txt"
    path.write_text("WHATEVER PROMPT" + prompt.SEP + user_for(N2).replace("13. Tony Romo parting ways", "13. Tony Romo is out"), encoding="utf-8")
    pre = run_script(monkeypatch, tmp_path, "--yes", "--old", "gap-old1234", "--nights", N2, "--full-file", str(path), "--repeat", "2")
    assert "2026-10-02: gap-2026-10-02-1528-preview.txt; old-style file" in pre and "This sends 8 runs" in pre
    assert "spent: $1.36 on 8 run(s)" in pre

    # an unknown prompt label, and a refusal, cost nothing
    assert "NOT FOUND" in run_script(monkeypatch, tmp_path, "--yes", "--old", "gap-nope")
    monkeypatch.setattr("builtins.input", lambda _q="": "no")
    stop = run_script(monkeypatch, tmp_path, "--old", "gap-old1234")
    assert "stopped. Nothing was sent and nothing was spent." in stop and "spent: $" not in stop


def test_v1161_prompt_text_comes_from_the_stored_versions_first(db):
    seed_nights()
    assert replay.prompt_text_for("gap-old1234") == (OLD_PROMPT, "the Grok file of 2026-10-01")
    store.save_prompt_version("gap-old1234", "THE STORED TEXT")
    assert replay.prompt_text_for("gap-old1234") == ("THE STORED TEXT", "stored prompt versions")
    assert replay.prompt_text_for("gap-nope") == (None, "not found")


def test_v1161_replay_script_is_read_only_and_hides_secrets():
    src = (ROOT / "scripts" / "replay_2x2.py").read_text(encoding="utf-8")
    mod = (ROOT / "gap" / "replay.py").read_text(encoding="utf-8")
    for bad in ("insert_", "insert into", "update ", "delete from", "freeze(", "record_llm_run", "set_state", "notify.send",
                "save_prompt_version", "results_save", "init_db", "print(db_url", "DATABASE_URL)", "create_no_order"):
        assert bad not in src and bad not in mod, bad
    assert "need_database_url" in src and "_prompt_hidden" in src and src.index("_secrets") < src.index("from gap import")
    assert "Type yes" in src and "--list" in src
