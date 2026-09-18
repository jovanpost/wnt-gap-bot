"""Status light + six-book board. No public order buttons. No live marks."""
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
            # Config says "poll ... every 60s" (see config.summary()); this was
            # actually sleeping 5s, i.e. firing 12x more often than intended --
            # every tick does a DB read (book_waiting_if_due) and, during the
            # detection window, a Kalshi call too. 30s keeps responsiveness
            # (fills/timers still checked twice a minute) while cutting that
            # load 6x.
            time.sleep(30)

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
    f"{C.VERSION} · six paper books, hold-to-settlement only · "
    "no live marks — P&L shows once Kalshi publishes an official result · "
    "Telegram courier · Saturday weekly dump"
)

tab_live, tab_books, tab_four, tab_curve = st.tabs(
    ["Tonight", "Six books · P&L", "All nights · 6 books", "Cancel-window curve"]
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
    pick = st.columns([2, 2, 1])
    with pick[0]:
        date_str = st.text_input("Board date (CT)", value=clock.today_ct())
    with pick[1]:
        st.caption(
            "A/B fade 15¢. E/F fade + Grok side ≥50.01 hold. "
            "G/H Grok−10 hold, cancel 5:29 CT. Scalp (C/D) removed v1.5.0."
        )
    with pick[2]:
        if st.button("Refresh", use_container_width=True):
            st.cache_data.clear()
            st.rerun()
    st.markdown(
        "**No live marks.** A row shows P&L only once Kalshi has published "
        "an official result for that market. Everything else reads *pending*. "
        "The fixture/curve tab is an old 128-night backtest — ignore it for last night."
    )

    @st.cache_data(ttl=20, show_spinner="Checking settlement…")
    def _tonight(d: str):
        return board.tonight(d)

    snap = _tonight(date_str)
    rows = snap["rows"]
    books = snap["books"]

    if not rows:
        st.info("No paper orders yet for " + date_str)
    else:
        for chunk_start in range(0, len(books), 3):
            cards = st.columns(3)
            for card, b in zip(cards, books[chunk_start:chunk_start + 3]):
                with card:
                    st.subheader(b["label"])
                    if b["settled_n"] == 0:
                        st.metric("P&L", "pending")
                    else:
                        st.metric("P&L", f"${b['pnl']:+.2f}", f"{b['pct']:+.1f}%")
                    st.caption(
                        f"{b['filled']}/{b['n']} filled · {b['settled_n']}/{b['n']} settled · "
                        f"W/L {b['wins']}/{b['losses']}"
                    )

        cmp = pd.DataFrame([
            {
                "book": b["label"],
                "size": f"${b['notional']:.0f}",
                "tickets": b["n"],
                "filled": b["filled"],
                "settled": b["settled_n"],
                "cost $": round(b["cost"], 2),
                "P&L $": round(b["pnl"], 2) if b["settled_n"] else None,
                "P&L %": round(b["pct"], 2) if b["settled_n"] else None,
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
            },
        )

        show_cols = [
            "word", "action", "fill_label", "intended_ct", "filled_ct",
            "unfilled_ct", "fill_pct", "entry_yes",
            "cost_dollars", "pnl_dollars", "pnl_pct", "gap_points",
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
            "cost_dollars": "cost $",
            "pnl_dollars": "P&L $",
            "pnl_pct": "P&L %",
            "gap_points": "gap ¢",
        }
        cfg = {
            "cost $": st.column_config.NumberColumn(format="$%.2f"),
            "P&L $": st.column_config.NumberColumn(format="$%+.2f"),
            "P&L %": st.column_config.NumberColumn(format="%+.1f%%"),
            "fill %": st.column_config.NumberColumn(format="%.0f%%"),
            "gap ¢": st.column_config.NumberColumn(format="%+.1f"),
            "entry YES ¢": st.column_config.NumberColumn(format="%d"),
        }

        for b in books:
            sub = [r for r in rows if r.get("variant_id") == b["id"]]
            pnl_label = "pending" if b["settled_n"] == 0 else f"${b['pnl']:+.2f} ({b['pct']:+.1f}%)"
            with st.expander(
                f"{b['label']}  ·  {pnl_label}  ·  "
                f"{b['filled']}/{b['n']} filled  ·  {b['settled_n']}/{b['n']} settled",
                expanded=(b["id"] in ("A", "E", "G")),
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

        md = board.as_markdown(snap)
        st.download_button(
            "Download board.md",
            data=md,
            file_name=f"gap-board-{date_str}.md",
            mime="text/markdown",
        )
        with st.expander("Copy as Markdown", expanded=False):
            st.caption("Select all, copy, paste back here.")
            st.code(md, language="markdown")
        st.caption(
            f"{snap['n_words']} words · {snap['n_orders']} tickets · "
            "Refresh checks Kalshi for new settlements"
        )

# ---------------------------------------------------------------------------
with tab_four:
    st.markdown(
        "Running paper totals across every night in the database. "
        "This is A/B/E/F/G/H on real booked tickets, not the old 128-night fixture. "
        "No live marks here either — unsettled nights show *pending*."
    )
    hist = board.history()
    books = hist["books"]
    st.caption(f"{len(hist['nights'])} nights · {hist['n_orders']} tickets · {hist['n_words']} word-nights")
    if books:
        for chunk_start in range(0, len(books), 3):
            cards = st.columns(3)
            for card, b in zip(cards, books[chunk_start:chunk_start + 3]):
                with card:
                    st.subheader(b["label"])
                    if b["settled_n"] == 0:
                        st.metric("P&L", "pending")
                    else:
                        st.metric("P&L", f"${b['pnl']:+.2f}", f"{b['pct']:+.1f}%")
                    st.caption(f"{b['filled']}/{b['n']} filled · W/L {b['wins']}/{b['losses']}")
        cmp = pd.DataFrame([{
            "book": b["label"], "tickets": b["n"], "filled": b["filled"],
            "settled": b["settled_n"],
            "cost $": round(b["cost"], 2),
            "P&L $": round(b["pnl"], 2) if b["settled_n"] else None,
            "P&L %": round(b["pct"], 2) if b["settled_n"] else None,
            "W": b["wins"], "L": b["losses"],
        } for b in books])
        st.dataframe(cmp, hide_index=True, use_container_width=True)
    else:
        st.info("No paper tickets stored yet.")

with tab_curve:
    st.markdown(
        "Same decision batch, same orders, cancel window extended "
        "1 → 120 minutes. Average price paid is comparable across rows. "
        "This tab is historical fixture data, unrelated to live quotes."
    )
    board_fx = fixture_board()
    left, right = st.columns(2)
    with left:
        st.write("**$1 hold**")
        st.dataframe(pd.DataFrame(board_fx["curve_1"]), hide_index=True)
    with right:
        st.write("**$100 hold**")
        st.dataframe(pd.DataFrame(board_fx["curve_100"]), hide_index=True)
    st.caption(
        "$100 fill % still climbing at 60m on the informal table is why 90 and "
        "120 are on this curve. If it has not flattened, do not freeze 60 yet."
    )

st.caption(f"boot {services['started_at']} · Telegram commands only")
