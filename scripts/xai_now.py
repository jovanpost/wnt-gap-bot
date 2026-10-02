"""Try Grok through the xAI API on a night whose Grok file is already stored. Writes NOTHING.

    ~/.venvs/wnt-gap/bin/python scripts/xai_now.py                    plain Grok (no tools) on the latest stored night
    ~/.venvs/wnt-gap/bin/python scripts/xai_now.py --expert           also the search-enabled run (costs more)
    ~/.venvs/wnt-gap/bin/python scripts/xai_now.py --date 2026-10-01  pick the night

It asks (hidden input) for the Supabase DATABASE_URL and the xAI API key, sends that night's stored
file to Grok, and prints: every word with said / not said (if settled), the manual Grok number, the
API numbers, a Brier score, and what the run used and cost. Safe to paste the output into chat.

On a PAST night the plain run is a fair replay (same file, no tools, nothing newer to look up).
The --expert run is NOT a fair forecast on a past night: its searches can find reports of the
broadcast itself. Use it there only to check that it works and to see what one run costs.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from _secrets import _prompt_hidden, need_database_url  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--date")
ap.add_argument("--expert", action="store_true")
ap.add_argument("--only-expert", action="store_true")
ARGS = ap.parse_args()

need_database_url("Supabase DATABASE_URL (hidden): ")
if not os.environ.get("XAI_API_KEY"):
    _k = _prompt_hidden("xAI (Grok) API key")
    if _k:
        os.environ["XAI_API_KEY"] = _k
    del _k

from gap import clock, config as C, shadow, store, xai  # noqa: E402


def pick_run():
    if ARGS.date:
        return store.get_run_for_date(ARGS.date)
    today = clock.today_ct()
    start = (date.fromisoformat(today) - timedelta(days=10)).isoformat()
    runs = [r for r in store.runs_between(start, today) if r.get("prompt_text")]
    past = [r for r in runs if str(r["event_date"])[:10] < today]
    return (past or runs or [None])[-1]


def main() -> int:
    print(f"{C.VERSION} | Grok API test | writes nothing")
    if not C.XAI_API_KEY:
        print("no xAI key given - nothing to do")
        return 1
    run = pick_run()
    if not run or not run.get("prompt_text"):
        print("no stored Grok file found for that night")
        return 1
    d = str(run["event_date"])[:10]
    words = run["word_list"]
    words = json.loads(words) if isinstance(words, str) else words
    names = [w["word"] for w in words]
    paste = run["prompt_text"]
    past = d < clock.today_ct()
    print(f"night {d} | {len(names)} words | file {len(paste):,} characters | model {C.XAI_MODEL}")
    modes = (["expert"] if ARGS.only_expert else ["plain"] + (["expert"] if ARGS.expert else []))
    if "expert" in modes and past:
        print("NOTE: the search run on a past night can read about the broadcast itself. Its numbers are a "
              "works/cost check, not a fair forecast.")

    got: dict = {}
    for mode in modes:
        name = xai.label(mode)
        print(f"asking {name} ... (plain: about a minute or two; search: can take 10+ minutes)", flush=True)
        try:
            g = xai.forecast(mode, paste, names, d, shadow.PREFACE, on_attempt=lambda m: print("  ", m, flush=True))
            got[name] = g
            print(f"  done in {g['seconds']}s, {g['attempts']} tr{'y' if g['attempts'] == 1 else 'ies'} | {xai.cost_line(g['usage'])}")
        except Exception as exc:  # noqa: BLE001
            spent = xai.cost_line(getattr(exc, "usage", None))
            print(f"  FAILED: {exc}" + (f" | spent {spent}" if spent else ""))

    result = dict(store.official_results([w["market_ticker"] for w in words]))
    if len(result) < len(words):                       # not cached yet: read Kalshi's public market list (read-only)
        try:
            from gap.kalshi import KalshiClient
            pub = KalshiClient(key_id="", private_key_pem="", private_key_path="")
            for m in pub.get_markets(run["event_ticker"]):
                r = (m.get("result") or "").lower()
                if r in ("yes", "no"):
                    result.setdefault(m.get("ticker"), r)
        except Exception as exc:  # noqa: BLE001
            print(f"(could not read results from Kalshi: {type(exc).__name__})")
    manual = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(d)}
    cols = {"Grok manual": manual}
    for name, g in got.items():
        cols[shadow.short_name(name)] = {f["word"]: f["probability"] for f in g["forecasts"]}
    print("")
    print("word | said? | " + " | ".join(cols))
    for w in words:
        r = result.get(w["market_ticker"])
        said = "YES" if r == "yes" else ("no" if r == "no" else "?")
        print(f"{w['word']} | {said} | " + " | ".join(str(c.get(w["word"], "-")) for c in cols.values()))
    settled = [w for w in words if result.get(w["market_ticker"]) in ("yes", "no")]
    if settled:
        print("")
        print(f"BRIER on {len(settled)} settled words (lower is better; 0.25 = always 50%):")
        for cname, c in cols.items():
            pairs = [(c[w["word"]] / 100, 1.0 if result[w["market_ticker"]] == "yes" else 0.0) for w in settled if w["word"] in c]
            if pairs:
                print(f"  {cname}: {sum((p - y) ** 2 for p, y in pairs) / len(pairs):.3f} on {len(pairs)} words")
    total = sum((g["usage"].get("cost_usd") or 0) for g in got.values())
    print("")
    print(f"COST of this test: ${total:.2f}")
    print("nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
