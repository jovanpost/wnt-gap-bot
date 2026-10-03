"""Read-only preview of the news blocks that go into tonight's Grok file.

    python3 scripts/preview_news.py            # today's words from Kalshi, ABC + Google News
    python3 scripts/preview_news.py --abc-only # just ABC's feeds

Touches NOTHING: no database, no Telegram, no orders, no Kalshi key (word list comes from
Kalshi's public market list). Safe to run any time, even while orders are resting.
Use this instead of /gap_resend to test: /gap_resend re-sends the file AND resets tonight's
run to "waiting for JSON", which after the 4:30 PM CT deadline marks the night expired.

Output has no secrets in it (titles, counts, times only), so it is safe to paste into chat.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from gap import abcfeeds, clock, config as C, netlimit  # noqa: E402
from gap.kalshi import KalshiClient, uniquify_words, word_from_market  # noqa: E402


def todays_words() -> tuple[str, list[dict]]:
    client = KalshiClient(key_id="", private_key_pem="", private_key_path="")  # public data only
    date_str = clock.today_ct()
    tokens = clock.event_date_tokens(date_str)
    events = client.get_events(C.SERIES, status="open")
    hits = [e for e in events if any(t in (e.get("event_ticker") or "").upper() for t in tokens)]
    event = hits[0] if hits else (events[0] if events else None)
    if not event:
        raise SystemExit(f"no open {C.SERIES} event found for {date_str}")
    rows = []
    for m in client.get_markets(event["event_ticker"]):
        if (m.get("status") or "").lower() in ("settled", "closed", "finalized"):
            continue
        rows.append({"market_ticker": m.get("ticker"), "title": m.get("title") or "", "word": word_from_market(m)})
    return event["event_ticker"], [{"word": r["word"]} for r in uniquify_words(rows)]


def main(argv: list[str] | None = None, words: list[dict] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--abc-only", action="store_true")
    args = ap.parse_args(argv)

    print(f"{C.VERSION} | news preview | read-only")
    if words is None:
        event_ticker, words = todays_words()
        print(f"event {event_ticker}: {len(words)} words")

    t0 = time.monotonic()
    if args.abc_only:
        g, abc = "", abcfeeds.abc_block(words)
    else:
        g, abc, more = abcfeeds.all_blocks(words)   # exactly what the Grok file gets
    took = time.monotonic() - t0
    print(abc or "(ABC feeds switched off: ABC_FEEDS_ON=false)")
    if not args.abc_only:
        print(more or "(other networks switched off: MORE_FEEDS_ON=false)")
    if not args.abc_only:
        print(g or "(Google headlines switched off or empty)")

    st = netlimit.stats()
    print("")
    try:                                   # v1.14.0: how ABC's homepage and video page were read (to spot a layout change)
        from gap import abcfront
        for which in ("home", "video"):
            fs = abcfront.last_stats.get(which)
            if not fs:
                print(f"ABC {which} page: not fetched in this run (switched off, or served from the 5-minute cache)")
            elif fs.get("error"):
                print(f"ABC {which} page: FAILED ({fs['error']})")
            else:
                print(f"ABC {which} page: {fs.get('kept', 0)} headlines kept ({fs.get('links', 0)} from story links, "
                      f"{fs.get('aria', 0)} from card labels, {fs.get('embedded', 0)} from page data; {fs.get('bytes', 0):,} bytes)")
    except Exception as exc:  # noqa: BLE001
        print(f"(front page stats unavailable: {type(exc).__name__})")
    print(f"SUMMARY: {took:.1f}s total (ABC, other networks and Google at the same time), {st['requests']} web requests, "
          f"{st['waited_s']:.1f}s total spacing wait (speed limit: {C.NET_MIN_GAP_S}s apart per site, "
          f"max {C.NET_MAX_PARALLEL} at once)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
