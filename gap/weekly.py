"""Saturday weekly dump — one .txt Claude can ingest."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from . import clock, config as C, notify, settle, store


def _money(cents) -> str:
    if cents is None:
        return "n/a"
    return f"{cents / 100.0:+.2f}"


def build_week_report(start: str | None = None, end: str | None = None) -> tuple[str, str]:
    if not start or not end:
        start, end, week_id = clock.week_mon_fri()
    else:
        week_id = f"{start}_to_{end}"

    settle.settle_range(start, end)
    runs = store.runs_between(start, end)

    lines: list[str] = []
    lines.append("WNT GAP TRADER — WEEKLY PAPER BOOK")
    lines.append(f"generated {clock.fmt(clock.now_ct())}")
    lines.append(f"week {week_id}  nights {start} → {end}")
    lines.append("")
    lines.append("FROZEN RULES")
    lines.append(C.summary())
    lines.append("")
    lines.append("FILL MODEL")
    lines.append("paper dry-book slices; settle on filled size only")
    lines.append("Assumes 100% ENTRY fill AT the limit. B is not evidence $100 fills.")
    lines.append("Do NOT read A vs B as a size study. Entry size is still untested.")
    lines.append("fills like $1. Real tape (60m): ~95% fill at $20 vs ~84% at $100.")
    lines.append("A real sweep also fills through the cap, not only at the cap.")
    lines.append("C/D scalp: cover when YES ask (NO side) or YES bid (YES side) tags Grok.")
    lines.append("E/F: same fade as A/B but skip unless Grok p on the bought side >= 50.01.")
    lines.append("G/H: no gap. Rest 10¢ cheap to Grok on Grok's side. Cancel 5:29 CT.")
    lines.append("Do not invent fills after 1¢. Leftover at cancel = unfilled.")
    lines.append("")
    lines.append("CAPS")
    lines.append("Paper: no cluster cap, no night cap, no bankroll — maximize nights.")
    lines.append("LIVE must restore cluster cap. One Iran-Gulf package was 64% of")
    lines.append("all historical losses. Caps are paper-off, not abolished.")
    lines.append("")

    book_pnl = {v["id"]: 0 for v in C.VARIANTS}
    book_n = {v["id"]: 0 for v in C.VARIANTS}
    book_open = {v["id"]: 0 for v in C.VARIANTS}

    if not runs:
        lines.append("NO RUNS THIS WEEK")
    for run in runs:
        lines.append("=" * 72)
        lines.append(f"NIGHT {run.get('event_date')}  {run.get('event_ticker')}  status={run.get('status')}")
        lines.append(f"harness={run.get('harness')}  prompt={run.get('prompt_version')}  markets={run.get('markets_n')}")
        lines.append("-" * 72)
        lines.append("GROK RAW RESPONSE")
        raw = run.get("raw_response") or ""
        lines.append(raw if raw else "(no JSON pasted this night)")
        lines.append("-" * 72)
        lines.append("FORECASTS")
        forecasts = store.forecasts_for_run(run["id"])
        quotes = store.quotes_for_run(run["id"])
        q_by_ticker = {}
        for q in quotes:
            q_by_ticker[q["market_ticker"]] = q
        for f in forecasts:
            q = q_by_ticker.get(f["market_ticker"], {})
            lines.append(
                f"  {f['word']}  model={f['probability']}  "
                f"mkt_bid={q.get('yes_bid_cents')} ask={q.get('yes_ask_cents')} "
                f"mid={q.get('market_prob')}"
            )
            if f.get("carrying_story"):
                lines.append(f"    story: {f['carrying_story']}")
            if f.get("reasoning"):
                lines.append(f"    reason: {f['reasoning']}")
            extra = {
                k: f.get(k) for k in (
                    "p_block_airs", "p_said_given_airs", "substitute_risk", "other_routes"
                ) if f.get(k) not in (None, "")
            }
            if extra:
                lines.append(f"    extra: {json.dumps(extra, ensure_ascii=False)}")
        lines.append("-" * 72)
        lines.append("PAPER BOOKS")
        orders = store.orders_for_run(run["id"])
        settles = store.settlements_for_order_ids([o["id"] for o in orders])
        if not orders:
            lines.append("  (no gaps cleared 15¢ this night)")
        for o in orders:
            s = settles.get(o["id"], {})
            vid = o.get("variant_id") or "?"
            lines.append(
                f"  [{vid} ${o.get('notional_dollars')} {o.get('exit_rule')}] "
                f"{o['word']} {o['side']} {o['contracts']}ct @ YES {o['limit_price_cents']}¢ "
                f"gap {o.get('gap_points')}  status={o.get('status')}"
            )
            if s:
                lines.append(
                    f"      outcome={s.get('outcome')}  net={_money(s.get('net_cents'))} "
                    f"fees={_money(s.get('fees_cents'))}  fill={s.get('fill_model')}"
                )
                if vid in book_pnl and s.get("net_cents") is not None:
                    book_pnl[vid] += int(s["net_cents"])
                    book_n[vid] += 1
            else:
                lines.append("      not settled yet")
                if vid in book_open:
                    book_open[vid] += 1
        lines.append("")

    lines.append("=" * 72)
    lines.append("WEEK TOTALS (settled paper only)")
    for spec in C.VARIANTS:
        vid = spec["id"]
        lines.append(
            f"  {spec['label']}: n_settled={book_n[vid]}  "
            f"n_open={book_open[vid]}  net={_money(book_pnl[vid])}"
        )
    lines.append("")
    lines.append("ASK FOR CLAUDE")
    lines.append("Read the frozen rules. Do not invent a bankroll, cluster cap, or $5 size.")
    lines.append("Compare A-H on this week only. E/F vs A/B is the 50.01 gate.")
    lines.append("G/H vs A/B is Grok-10 until 5:29, not the 15¢ fade.")
    lines.append("C/D scalp vs A/B hold. Do not retune 15¢ or 26¢ no-fade from one night.")
    text = "\n".join(lines) + "\n"
    fname = f"gap-week-{week_id}.txt"
    return fname, text


def send_week_report(force: bool = False) -> str:
    start, end, week_id = clock.week_mon_fri()
    state_key = f"weekly_sent_{week_id}"
    if not force and store.get_state(state_key):
        return f"already sent {week_id}"
    fname, text = build_week_report(start, end)
    caption = (
        f"WNT gap weekly {week_id} ({start} → {end}). "
        f"Paste this file into the Claude strategy chat."
    )
    msg_id = notify.send_document(fname, text, caption)
    store.set_state(state_key, {
        "sent_at": datetime.now(timezone.utc).isoformat(),
        "telegram_msg_id": msg_id,
        "bytes": len(text),
    })
    store.log_activity("weekly", f"{week_id} msg={msg_id} bytes={len(text)}")
    return f"sent {fname} ({len(text)} bytes) msg={msg_id}"
