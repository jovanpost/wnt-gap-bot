"""
Read-only pre-flight check for Book L (LIVE, real money) before you flip
L_LIVE_ON. Mirrors the same check already built for wnt-nolive-bot.

Proves, without placing or cancelling a single order:
  1. The live Kalshi key signs in and can read your account.
  2. Your real cash balance and today's worst-case resting collateral
     under Book L's own caps.
  3. Any orders already resting for this bot's series.
  4. Today's event/market list from Kalshi, and -- if a run for today has
     already been scored -- which words currently qualify under Book L's
     own rule (fade15_gate30_no), using the same gap/strategy.py function
     the live bot calls. No new logic, no LLM calls made here.
  5. Which storage backend it connected to (Postgres vs local SQLite
     fallback) and how many Book L order rows exist for today.

Run it by hand:  python scripts/verify_live_api.py
Nothing here calls create_no_order, cancel_order, or batch_cancel.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must happen BEFORE any `gap` import -- gap/config.py reads these env vars
# (and DATABASE_URL) at import time.
from _secrets import mask, need_database_url_optional, need_kalshi_credentials  # noqa: E402

need_kalshi_credentials()
need_database_url_optional()

from gap import clock as CK          # noqa: E402
from gap import config as C          # noqa: E402
from gap import store as ST          # noqa: E402
from gap import strategy as STRAT    # noqa: E402
from gap.kalshi import KalshiClient  # noqa: E402


def line(title: str) -> None:
    print()
    print(f"--- {title} " + "-" * max(0, 60 - len(title)))


def main() -> int:
    line("Book L config summary")
    print(C.summary())
    print(
        f"L_LIVE_ON={C.L_LIVE_ON}  L_NOTIONAL_DOLLARS=${C.L_NOTIONAL_DOLLARS:g}  "
        f"L_NIGHTLY_CAP_DOLLARS=${C.L_NIGHTLY_CAP_DOLLARS:g}  "
        f"L_MAX_WORDS_PER_NIGHT={C.L_MAX_WORDS_PER_NIGHT}  "
        f"SHOW_CANCEL_CT={C.SHOW_CANCEL_CT}"
    )
    print(f"BASE_URL={C.BASE_URL}  ORDER_API={C.ORDER_API}  POST_ONLY={C.POST_ONLY}")
    if C.L_LIVE_ON:
        print("NOTE: L_LIVE_ON is currently TRUE in this environment's config/secrets.")
    else:
        print("L_LIVE_ON is currently false -- the live bot will not place real orders as-is.")

    line("Kalshi auth")
    client = KalshiClient()
    print(f"Key loaded: {client.authenticated}")
    print(f"Key ID: {mask(client.key_id or '')}")
    if not client.authenticated:
        print("No usable key/private key -- stopping here (nothing else needs a real key).")
        return 1

    line("Balance & resting collateral")
    try:
        bal = client.get_balance()
        cash_cents = bal.get("balance")
        print(f"Cash balance: ${cash_cents / 100:,.2f}" if cash_cents is not None else f"raw: {bal}")
    except Exception as exc:
        print(f"get_balance() failed: {exc}")

    try:
        resting = client.get_resting_orders(series_prefix=C.SERIES)
        worst_case_cents = 0
        for o in resting:
            px = o.get("no_price") or o.get("price") or 0
            ct = o.get("remaining_count") or o.get("count") or 0
            try:
                worst_case_cents += int(float(px) * float(ct))
            except (TypeError, ValueError):
                pass
        print(f"Resting orders on {C.SERIES}: {len(resting)}")
        print(f"Worst-case resting collateral (this series): ${worst_case_cents / 100:,.2f}")
        print(
            f"Book L nightly cap: ${C.L_NIGHTLY_CAP_DOLLARS:g} "
            f"(first {C.L_MAX_WORDS_PER_NIGHT} qualifying words, ${C.L_NOTIONAL_DOLLARS:g} each)"
        )
        for o in resting[:10]:
            print(f"  - {o.get('ticker')}: {o.get('side')} {o.get('remaining_count') or o.get('count')} @ "
                  f"{o.get('no_price') or o.get('price')}c  order_id={o.get('order_id')}")
    except Exception as exc:
        print(f"get_resting_orders() failed: {exc}")

    line("Today's events & markets (read-only)")
    today = CK.today_ct()
    print(f"Series: {C.SERIES}  Date (CT): {today}")
    try:
        events = client.get_events(C.SERIES, status="open")
        print(f"Open events: {len(events)}")
        markets: list[dict] = []
        event_ticker = None
        for ev in events[:1]:
            event_ticker = ev.get("event_ticker")
            markets = client.get_markets(event_ticker)
            print(f"Event: {event_ticker}  markets: {len(markets)}")
        if not events:
            print("No open events right now for this series.")
    except Exception as exc:
        print(f"get_events()/get_markets() failed: {exc}")
        markets = []
        event_ticker = None

    line("Which words currently qualify under Book L's rule")
    run = ST.get_run_for_date(today)
    if not run:
        print(f"No scored run found in storage for {today} yet -- nothing to evaluate.")
        print("(This only reads a run this bot already scored; it does not call the model.)")
    else:
        forecasts = ST.forecasts_for_run(run["id"])
        by_ticker = {f["market_ticker"]: f for f in forecasts}
        qualifying = 0
        checked = 0
        from gap.kalshi import market_yes_quotes  # local import, read-only helper

        for m in markets:
            ticker = m.get("ticker") or m.get("market_ticker")
            fc = by_ticker.get(ticker)
            if not fc or fc.get("probability") is None:
                continue
            checked += 1
            bid, ask = market_yes_quotes(m)
            valid = bid is not None and ask is not None and 1 <= bid < ask <= 99
            order = STRAT.order_for_rule(
                "fade15_gate30_no", int(fc["probability"]), bid, ask, valid, C.L_NOTIONAL_DOLLARS,
            )
            if order:
                qualifying += 1
                print(
                    f"  QUALIFIES: {fc.get('word')} ({ticker})  grok={fc['probability']}  "
                    f"bid/ask={bid}/{ask}  side={order['side']}  our_price={order['our_price_cents']}c"
                )
        print(f"Checked {checked} scored word(s), {qualifying} currently qualify for Book L.")

    line("Storage backend & today's Book L order rows")
    print(f"Backend: {'Postgres' if ST.using_postgres() else 'local SQLite fallback'}")
    if not ST.using_postgres():
        print(f"SQLite path: {C.SQLITE_PATH}")
    try:
        l_rows = ST.l_orders_for_date(today)
        print(f"Book L order rows for {today}: {len(l_rows)}")
        for r in l_rows[:10]:
            print(f"  - {r.get('market_ticker')}  status={r.get('status')}  "
                  f"contracts={r.get('contracts')}  cost_cents={r.get('cost_cents')}")
    except Exception as exc:
        print(f"l_orders_for_date() failed: {exc}")

    line("Done")
    print("Read-only pre-flight complete. No orders were placed or cancelled.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
