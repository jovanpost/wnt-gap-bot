"""Status light + four-way backtest board. No public order buttons."""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd
import streamlit as st

from gap import backtest, board, clock, config as C, notify, pipeline, store

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s",
)

st.set_page_config(page_title="WNT Gap Bot", page_icon="📐", layout="wide")


@st.cache_resource
def boot():
    store.init_db()
    pipeline.register_commands()
    notify.start_listener()

    def _poll_loop():
        while True:
            try:
                pipeline.poll_once()
            except Exception:
                logging.getLogger("gap.poll").exception("poll_once")
            time.sleep(60)

    t = threading.Thread(target=_poll_loop, name="gap-poll", daemon=True)
    t.start()
    return {"started_at": clock.now_ct().isoformat()}


@st.cache_data(show_spinner=False)
def fixture_board():
    decisions, tape = backtest.build_fixture()
    batch = backtest.fixed_batch(decisions)
    four = backtest.run_four_way(batch, tape, cancel_min=C.CANCEL_AFTER_MIN)
    curve_1 = backtest.cancel_window_curve(batch, tape, notional=1.0, exit_rule="hold")
    curve_100 = backtest.cancel_window_curve(batch, tape, notional=100.0, exit_rule="hold")
    return {
        "n_raw": len(decisions),
        "n_batch": len(batch),
        "four": four,
        "curve_1": curve_1,
        "curve_100": curve_100,
    }


if st.query_params.get("ping") == "true":
    boot()
    st.write("alive")
    st.stop()

if st.query_params.get("weekly") == "true":
    boot()
    from gap import weekly
    st.write(weekly.send_week_report(force=False))
    st.stop()

services = boot()

st.title("📐 WNT Gap Bot")
st.caption(
    f"{C.VERSION} · four paper books · mark-to-market from live Kalshi quotes · "
    "Telegram courier · Saturday weekly dump"
)

tab_live, tab_books, tab_four, tab_curve = st.tabs(
    ["Tonight", "Four books · P&L", "Four-way backtest", "Cancel-window curve"]
)

# ---------------------------------------------------------------------------
with tab_live:
    if C.PAPER:
        st.info("**PAPER** — capped-sweep rows only. No Kalshi place_order.")
    elif C.may_place_live():
        st.error("**LIVE** — Phase 1 should not be here.")
    else:
        st.warning("Live flags mixed. Placement blocked.")

    st.code(C.summary())
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Today CT", date_str)
    send_label = clock.fmt(clock.decision_at(date_str))
    if run:
        due = clock.send_due_at(run.get("market_open_at") or run.get("created_at"))
        if due:
            send_label = clock.fmt(due)
    c2.metric("File send", send_label)
    if run:
        c3.metric("Run", str(run.get("status")))
        c4.metric("Markets", str(run.get("markets_n") or 0))
    else:
        c3.metric("Run", "none")
        c4.metric("Markets", "—")

    if not run:
        st.write(
            "No event yet. Poll starts", C.POLL_START_CT,
            "CT. File sends", C.DECISION_LAG_MIN, "min after first detect."
        )
    elif run.get("status") == "detected":
        st.warning(
            "Event seen. Telegram file waits until "
            + send_label
            + ". /gap_sendnow skips the wait."
        )
    else:
        st.write(
            f"`{run.get('event_ticker')}` · `{run.get('harness')}` · "
            f"`{run.get('prompt_version')}`"
        )
        if run.get("parse_error"):
            st.error(run["parse_error"])
        markets = store.markets_for_run(run["id"])
        if markets:
            st.dataframe(
                pd.DataFrame(markets)[["word", "market_ticker", "title"]],
                hide_index=True,
                use_container_width=True,
            )

    act = store.recent_activity(20)
    if act:
        st.subheader("Activity")
        st.dataframe(
            pd.DataFrame(act)[["at", "kind", "message"]],
            hide_index=True,
            use_container_width=True,
        )

# ---------------------------------------------------------------------------
with tab_books:
    date_str = clock.today_ct()
    top = st.columns([3, 1])
    with top[0]:
        st.markdown(
            "Fills = nofade crossing book + public prints. "
            "SELL YES @ L only counts YES bids ≥ L and trades printed at ≥ L. "
            "Mid is mark-to-market only. Each book sees the full tape. "
            "Unfilled dies at send+60m."
        )
    with top[1]:
        if st.button("Refresh quotes", use_container_width=True):
            st.cache_data.clear()
            st.rerun()

    @st.cache_data(ttl=20, show_spinner="Quoting Kalshi…")
    def _tonight(d: str):
        return board.tonight(d)

    snap = _tonight(date_str)
    rows = snap["rows"]
    books = snap["books"]

    if not rows:
        st.info("No paper orders yet for " + date_str)
    else:
        cards = st.columns(4)
        for card, b in zip(cards, books):
            with card:
                st.subheader(b["label"])
                delta = f"{b['pct']:+.1f}%"
                st.metric("P&L", f"${b['pnl']:+.2f}", delta)
                st.caption(
                    f"cost ${b['cost']:.2f} → mark ${b['mark']:.2f}\n\n"
                    f"{b['filled']}/{b['n']} filled · W/L {b['wins']}/{b['losses']}"
                )

        cmp = pd.DataFrame([
            {
                "book": b["label"],
                "size": f"${b['notional']:.0f}",
                "exit": b["exit"],
                "tickets": b["n"],
                "filled": b["filled"],
                "cost $": round(b["cost"], 2),
                "mark $": round(b["mark"], 2),
                "P&L $": round(b["pnl"], 2),
                "P&L %": round(b["pct"], 2),
                "W": b["wins"],
                "L": b["losses"],
            }
            for b in books
        ])
        st.dataframe(
            cmp,
            hide_index=True,
            use_container_width=True,
            column_config={
                "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
                "P&L %": st.column_config.NumberColumn(format="%+.1f%%"),
                "cost $": st.column_config.NumberColumn(format="$%.2f"),
                "mark $": st.column_config.NumberColumn(format="$%.2f"),
            },
        )

        show_cols = [
            "word", "action", "fill_label", "intended_ct", "filled_ct",
            "unfilled_ct", "fill_pct", "entry_yes", "now_yes",
            "cost_dollars", "mark_dollars", "pnl_dollars", "pnl_pct", "gap_points",
        ]
        labels = {
            "word": "word",
            "action": "Kalshi",
            "fill_label": "status",
            "intended_ct": "want ct",
            "filled_ct": "filled ct",
            "unfilled_ct": "left ct",
            "fill_pct": "fill %",
            "entry_yes": "entry YES ¢",
            "now_yes": "now YES bid/ask/mid",
            "cost_dollars": "cost $",
            "mark_dollars": "mark $",
            "pnl_dollars": "P&L $",
            "pnl_pct": "P&L %",
            "gap_points": "gap ¢",
        }
        cfg = {
            "cost $": st.column_config.NumberColumn(format="$%.2f"),
            "mark $": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.1f%%"),
            "fill %": st.column_config.NumberColumn(format="%.0f%%"),
            "gap ¢": st.column_config.NumberColumn(format="%+.1f"),
            "entry YES ¢": st.column_config.NumberColumn(format="%d"),
        }

        for b in books:
            sub = [r for r in rows if r.get("variant_id") == b["id"]]
            with st.expander(
                f"{b['label']}  ·  ${b['pnl']:+.2f}  ({b['pct']:+.1f}%)  ·  "
                f"{b['filled']}/{b['n']} filled",
                expanded=(b["id"] in ("A", "B")),
            ):
                if not sub:
                    st.write("empty")
                    continue
                df = pd.DataFrame(sub)
                view = df[show_cols].rename(columns=labels)
                st.dataframe(
                    view,
                    hide_index=True,
                    use_container_width=True,
                    column_config=cfg,
                )

        st.caption(
            f"{snap['n_words']} words · {snap['n_orders']} tickets · "
            "quotes cached 20s · Refresh quotes to force a pull"
        )

# ---------------------------------------------------------------------------
with tab_four:
    st.markdown(
        "Entry for all four: **capped taker sweep**, decision at open+60, "
        f"`|gap|>{C.GAP_THRESHOLD}` then take `{C.LIMIT_OFFSET_CENTS}¢` from mid, cancel at send+60. "
        "Each variant is an independent pass over the **same fixed batch** "
        "and a **read-only** tape — no shared remaining-volume counter."
    )
    board = fixture_board()
    st.caption(
        f"FIXTURE tape · {board['n_raw']} market-nights · "
        f"{board['n_batch']} triggered gaps · cancel {board['four']['cancel_min']}m. "
        "Drop real candles later in `data/`; this board is the algorithm, labeled."
    )

    winner = board["four"]["winner"]
    cards = st.columns(4)
    for spec, card in zip(backtest.VARIANTS, cards):
        row = next(s for s in board["four"]["summaries"] if s["variant"] == spec["id"])
        with card:
            badge = " ← pick" if spec["id"] == winner else ""
            st.subheader(f"{row['label']}{badge}")
            st.metric("Fill-adjusted edge", f"{row['fill_adjusted_edge']:+.3f}")
            st.metric("Net $", f"{row['net_dollars']:+.2f}")
            st.metric("Avg fill", f"{row['avg_fill_pct']:.0f}%")
            st.write(
                f"fully filled {row['fully_filled_pct']:.0f}% · "
                f"triggered {row['n_triggered']} · filled {row['n_filled']}\n\n"
                f"intended ${row['intended_dollars']:.0f} · "
                f"deployed ${row['deployed_dollars']:.0f}\n\n"
                f"per intended $ {row['edge_per_intended_dollar']:+.3f} · "
                f"per deployed $ {row['edge_per_deployed_dollar']:+.3f}\n\n"
                f"W/L {row['wins']}/{row['losses']}"
            )

    st.markdown(
        f"**Current pick on this fixture:** variant **{winner}** "
        "(highest fill-adjusted edge = deployed-dollar edge × avg fill). "
        "Shelve the other three; do not delete them."
    )

    st.subheader("Compare")
    st.dataframe(
        pd.DataFrame(board["four"]["summaries"]).drop(columns=[]),
        hide_index=True,
        use_container_width=True,
    )

    st.subheader("Trades")
    pick = st.selectbox("Variant book", ["A", "B", "C", "D"], index=0)
    book = pd.DataFrame(board["four"]["books"][pick])
    show = [c for c in (
        "event_date", "word", "kalshi_action", "side", "model_prob",
        "market_yes_cents", "yes_price_cents", "limit_cents", "intended",
        "filled", "fill_pct", "avg_fill_cents", "exit_reason", "net_cents",
        "outcome",
    ) if c in book.columns]
    st.dataframe(book[show], hide_index=True, use_container_width=True)

    st.subheader("Informal prototype table (do not use for price-paid)")
    st.caption(
        "These numbers sampled a fresh batch per window. Fill % columns are "
        "honest within each sample; average price paid was not. The curve tab "
        "is the fixed-batch version."
    )
    st.dataframe(pd.DataFrame(backtest.INFORMAL_FILL_TABLE), hide_index=True)

# ---------------------------------------------------------------------------
with tab_curve:
    st.markdown(
        "Same decision batch, same orders, cancel window extended "
        "1 → 120 minutes. Average price paid is comparable across rows."
    )
    board = fixture_board()
    left, right = st.columns(2)
    with left:
        st.write("**$1 hold**")
        st.dataframe(pd.DataFrame(board["curve_1"]), hide_index=True)
    with right:
        st.write("**$100 hold**")
        st.dataframe(pd.DataFrame(board["curve_100"]), hide_index=True)
    st.caption(
        "$100 fill % still climbing at 60m on the informal table is why 90 and "
        "120 are on this curve. If it has not flattened, do not freeze 60 yet."
    )

st.caption(f"boot {services['started_at']} · Telegram commands only")
