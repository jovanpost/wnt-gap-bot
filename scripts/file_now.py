"""Build tonight's Grok file NOW with the code on this Mac, and save it as a text file. Writes NOTHING
to the database and never trades.

    ~/.venvs/wnt-gap/bin/python scripts/file_now.py              the file only (to paste into Grok on the web)
    ~/.venvs/wnt-gap/bin/python scripts/file_now.py --ask-grok   also send the SAME file to Grok through the API
                                                                  (no search; about $0.15) and print the numbers

It asks (hidden input) for the Supabase DATABASE_URL -- press Enter to skip: the file then has no WORD
HISTORY block and the table has no "manual" column. With --ask-grok it also asks for the xAI key.
The file is saved in your Downloads folder as gap-<date>-<time>-preview.txt.
It never prints the database URL or a key. Safe to paste the output into chat.

Use it to cross-check: paste the saved file into a new Grok Expert chat on the web, and compare with
the API numbers on exactly the same file.
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.abspath(os.path.join(HERE, "..")))

from _secrets import _prompt_hidden, need_database_url_optional  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--ask-grok", action="store_true", help="also send the file to Grok through the API (no search, about $0.15)")
ap.add_argument("--out", help="folder to save the file in (default: ~/Downloads)")
ARGS = ap.parse_args()

db_url = need_database_url_optional("Supabase DATABASE_URL (hidden; press Enter to skip): ")
if ARGS.ask_grok and not os.environ.get("XAI_API_KEY"):
    _k = _prompt_hidden("xAI (Grok) API key")
    if _k:
        os.environ["XAI_API_KEY"] = _k
    del _k
if not os.environ.get("YOUTUBE_API_KEY"):
    _y = _prompt_hidden("YouTube API key for the PREVIOUS BROADCASTS block (press Enter to skip)")
    if _y:
        os.environ["YOUTUBE_API_KEY"] = _y
    del _y
if not db_url:
    os.environ["WORD_HISTORY_NIGHTS"] = "0"     # the history block needs the database

from gap import clock, config as C, prompt, shadow, store, xai  # noqa: E402
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


def main() -> int:
    print(f"{C.VERSION} | build tonight's file now | writes nothing to the database, never trades")
    print("database:", f"connected ({store.engine().url.host}), read only" if db_url else "skipped (no word history, no manual column)")
    date_str = clock.today_ct()
    event_ticker, words = todays_event()
    print(f"event {event_ticker}: {len(words)} words. Fetching the news and building the file ...")
    paste = prompt.build_paste_file(date_str, event_ticker, words)
    folder = os.path.expanduser(ARGS.out or "~/Downloads")
    name = f"gap-{date_str}-{clock.now_ct().strftime('%H%M')}-preview.txt"
    path = os.path.join(folder, name)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(paste)
    print(f"saved: {name} in {folder}  ({len(paste):,} characters, prompt {C.PROMPT_VERSION})")
    for block in ("WORD HISTORY", "PREVIOUS BROADCASTS (the last", "ABC NEWS FEEDS", "ABC Front Page:", "ABC Video:", "OTHER NETWORKS AND WIRES", "GOOGLE NEWS HEADLINES"):
        print(f"  {'yes' if block in paste else 'NO '}  {block}")
    try:                                   # v1.15.0: show the new block, or say why it is missing
        from gap import broadcasts
        if "PREVIOUS BROADCASTS (the last" in paste:
            start = paste.index("PREVIOUS BROADCASTS (the last")
            end = paste.find("\nABC NEWS FEEDS", start)
            print("")
            print(paste[start:end if end > 0 else start + 4000].rstrip())
            print("")
        else:
            print(f"  (PREVIOUS BROADCASTS left out: {broadcasts.last_error.get('why') or 'nothing found'})")
    except Exception as exc:  # noqa: BLE001
        print(f"  (PREVIOUS BROADCASTS check failed: {type(exc).__name__})")
    print("Paste that whole file into a NEW Grok Expert chat for the cross-check.")
    print("Do NOT paste Grok's answer back into Telegram: tonight's trades are already set from the 12:20 file.")

    if not ARGS.ask_grok:
        return 0
    if not C.XAI_API_KEY:
        print("no xAI key given: the API part is skipped")
        return 0
    names = [w["word"] for w in words]
    print("")
    print(f"asking {xai.label('plain')} on this same file (no search; a few minutes) ...", flush=True)
    try:
        g = xai.forecast("plain", paste, names, date_str, shadow.PREFACE, on_attempt=lambda m: print("  ", m, flush=True))
    except Exception as exc:  # noqa: BLE001
        spent = xai.cost_line(getattr(exc, "usage", None))
        print(f"  FAILED: {exc}" + (f" | spent {spent}" if spent else ""))
        return 1
    print(f"  done in {g['seconds']}s | {xai.cost_line(g['usage'])}")
    now_api = {f["word"]: f["probability"] for f in g["forecasts"]}
    manual, before = {}, {}
    if db_url:
        manual = {f["word"]: f["probability"] for f in store.grok_forecasts_for_date(date_str)}
        before = {r["word"]: r["probability"] for r in store.shadow_forecasts(date_str)
                  if str(r["model"]).startswith("xai:") and not str(r["model"]).endswith("+search")}
    print("")
    print("word | manual Grok (12:20 file) | Grok API (12:20 file) | Grok API (this file) | change")
    for w in names:
        b, n = before.get(w), now_api.get(w)
        change = "-" if b is None or n is None else f"{n - b:+d}"
        print(f"{w} | {manual.get(w, '-')} | {b if b is not None else '-'} | {n if n is not None else '-'} | {change}")
    old = [w for w in names if w in manual and w in before]
    if old:
        print(f"12:20 file, manual Grok against Grok API: {sum(abs(manual[w] - before[w]) for w in old) / len(old):.1f} points apart on average")
    moved = [w for w in names if w in before and w in now_api]
    if moved:
        print(f"Grok API, 12:20 file against this file: {sum(abs(now_api[w] - before[w]) for w in moved) / len(moved):.1f} points apart on average")
    print("For a fair web-against-API number on THIS file, paste this file into Grok on the web and compare its answer with the last column.")
    print("nothing was written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
