"""Run tonight's challenger forecasts NOW from your Mac (paper only, never trades).

    ~/.venvs/wnt-gap/bin/python scripts/shadow_now.py

It asks (hidden input, nothing shows on screen) for:
  1. the Supabase DATABASE_URL -- press Enter to skip: then nothing is saved and there is no
     word history and no Grok column, but you still see Gemini's and the baseline's numbers;
  2. the Gemini API key (the same one that is in the gap app's Streamlit secrets).

Then it builds tonight's Grok file with the current code (ABC + Google News + word history),
asks Gemini and the no-AI baseline, prints one table (word | Grok | Gemini | base), and -- if a
database URL was given -- saves the numbers in gap_shadow_forecasts so Saturday scores them.
It never prints the database URL or the key. Safe to paste the output into chat.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from _secrets import _prompt_hidden, need_database_url_optional  # noqa: E402

db_url = need_database_url_optional("Supabase DATABASE_URL (hidden; press Enter to skip saving): ")
print("Keys: paste each one (hidden), or press Enter to skip that service.")
for _env, _label in (("GEMINI_API_KEY", "Gemini API key"), ("NVIDIA_API_KEY", "NVIDIA API key"),
                     ("CEREBRAS_API_KEY", "Cerebras API key"), ("MISTRAL_API_KEY", "Mistral API key"),
                     ("OPENROUTER_API_KEY", "OpenRouter API key"), ("XAI_API_KEY", "xAI (Grok) API key")):
    if not os.environ.get(_env):
        _key = _prompt_hidden(_label)
        if _key:
            os.environ[_env] = _key
        del _key
if not db_url:
    os.environ["WORD_HISTORY_NIGHTS"] = "0"     # history needs the database

from gap import clock, config as C, prompt, shadow, store  # noqa: E402
from gap.kalshi import KalshiClient, uniquify_words, word_from_market  # noqa: E402


def todays_event() -> tuple[str, list[dict]]:
    client = KalshiClient(key_id="", private_key_pem="", private_key_path="")   # public data only
    date_str = clock.today_ct()
    tokens = clock.event_date_tokens(date_str)
    events = client.get_events(C.SERIES, status="open")
    hits = [e for e in events if any(t in (e.get("event_ticker") or "").upper() for t in tokens)]
    event = hits[0] if hits else (events[0] if events else None)
    if not event:
        raise SystemExit(f"no open {C.SERIES} event for {date_str}")
    rows = []
    for m in client.get_markets(event["event_ticker"]):
        if (m.get("status") or "").lower() in ("settled", "closed", "finalized"):
            continue
        rows.append({"market_ticker": m.get("ticker"), "title": m.get("title") or "", "word": word_from_market(m)})
    words = [{"word": r["word"], "market_ticker": r["market_ticker"], "title": r["title"]} for r in uniquify_words(rows)]
    return event["event_ticker"], words


def apply_sql(name: str) -> None:
    """Apply one migration file (all statements are 'if not exists' / safe to repeat)."""
    path = os.path.join(HERE, "..", "sql", name)
    with open(path, encoding="utf-8") as fh:
        for stmt in store._split_statements(fh.read()):
            with store.engine().begin() as conn:
                conn.execute(store.text(stmt))


def apply_shadow_table() -> None:
    """Create gap_shadow_forecasts (with Row-Level Security) if missing. Only this one migration."""
    path = os.path.join(HERE, "..", "sql", "009_shadow_forecasts.sql")
    with open(path, encoding="utf-8") as fh:
        for stmt in store._split_statements(fh.read()):
            with store.engine().begin() as conn:
                conn.execute(store.text(stmt))


def main() -> int:
    print(f"{C.VERSION} | challengers now | paper only, never trades")
    print("database:", f"connected ({store.engine().url.host})" if db_url else "skipped (nothing will be saved)")
    from gap import challengers as CH
    print("gemini key:", "set" if C.GEMINI_API_KEY else "NOT set (Gemini will be skipped)")
    print("other challengers:", ", ".join(f"{p}:{m}" for p, m in CH.enabled_specs()) or "none (no keys)")
    from gap import xai
    print("grok via xAI API:", ", ".join(xai.label(m) for m in xai.enabled_modes()) or "off (no key)")
    date_str = clock.today_ct()
    event_ticker, words = todays_event()
    print(f"event {event_ticker}: {len(words)} words. Building the file (news + history), then asking every challenger...")
    if db_url:
        apply_shadow_table()
        apply_sql("010_frozen_packages.sql")
        apply_sql("011_llm_runs.sql")
    paste = prompt.build_paste_file(date_str, event_ticker, words)
    pkg = prompt.freeze(date_str, event_ticker, words, paste, kind="manual") if db_url else None
    print("if a service is busy it retries the SAME request for up to "
          f"{C.GEMINI_RETRY_BUDGET_S / 60:.0f} min; all services run at the same time (Ctrl-C to stop)")
    rep = shadow.run(date_str, event_ticker, words, paste, save=bool(db_url),
                     on_attempt=lambda msg: print("  ", msg, flush=True), package_id=(pkg or {}).get("id"))
    grok = {}
    if db_url:
        grok = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(date_str)}
    print("")
    print(shadow.summary_text(date_str, words, rep, grok))
    if db_url:
        print("")
        print("saved: rows already there for tonight were kept as they were (first forecast of the night wins).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
