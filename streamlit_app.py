"""Status light + six-book board. No public order buttons. No live marks."""
from __future__ import annotations

import logging
import threading
import time

import pandas as pd
import streamlit as st

from gap import backtest, board, clock, config as C, lab, notify, pipeline, store, weekly

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
    st.write(weekly.send_week_report(force=False))
    st.stop()

services = boot()

st.title("📐 WNT Gap Bot")
st.caption(
    f"{C.VERSION} · {len(C.VARIANTS)} paper books ({', '.join(v['id'] for v in C.VARIANTS)}), hold-to-settlement only · "
    "no live marks — P&L shows once Kalshi publishes an official result · "
    "Telegram courier · Saturday weekly dump"
)

@st.cache_data(ttl=180, show_spinner="Reading every night from the database…")
def _slice():
    return weekly.slice_panel()


try:
    PANEL = _slice()
    PANEL_ERR = None
except Exception as exc:  # never let the research tabs break the live page
    PANEL, PANEL_ERR = None, str(exc)

with st.sidebar:
    st.header("View")
    _week_opts = ["All weeks"] + (sorted(PANEL["weeks"], reverse=True) if PANEL else [])
    SEL = st.selectbox(
        "Week",
        _week_opts,
        help="Filters the Book K, Strategy lab and All-nights tabs to one week. 'All weeks' = everything so far.",
    )
    if st.button("Reload research data", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.caption("Research tabs read the database only (no Kalshi calls) and refresh every 3 minutes.")

tab_live, tab_books, tab_four, tab_k, tab_lab, tab_curve = st.tabs(
    ["Tonight", "Books · P&L", "All nights · books", "Book K + blend", "Strategy lab", "Cancel-window curve"]
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
    if SEL != "All weeks":
        _rows = [r for r in hist["rows"] if r.get("event_date") and weekly._week_id(str(r["event_date"])[:10]) == SEL]
        hist = {**hist, "rows": _rows, "books": board.summarize(_rows),
                "nights": sorted({str(r["event_date"])[:10] for r in _rows}), "n_orders": len(_rows),
                "n_words": len({(str(r["event_date"])[:10], r.get("word")) for r in _rows})}
        st.info(f"Showing only week {SEL} (change it in the sidebar).")
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

# ---------------------------------------------------------------------------
def _pick(rows):
    """Rows for the selected week (plus the cumulative row), or everything."""
    if SEL == "All weeks":
        return rows
    return [r for r in rows if r["label"] in (SEL, "CUMULATIVE")]


def _k_df(rows):
    return pd.DataFrame([{
        "week": r["label"], "prompt": r["prompt"], "booked": r["booked"], "filled": r["filled"],
        "unfilled": r["unfilled"], "W": r["w"], "L": r["l"], "fill % (contracts)": r["fill_ct"], "hit %": r["hit"],
        "gross $": r["gross"] / 100.0, "fees $": r["fees"] / 100.0, "net $": r["net"] / 100.0,
        "ROI %": r["roi"], "avg NO px ¢": r["px"], "break-even": r["be"], "margin": r["margin"],
        "unfilled would-hit %": r["unf_hit"],
    } for r in rows])


CFG_K = {
    "fill % (contracts)": st.column_config.NumberColumn(format="%.0f"),
    "hit %": st.column_config.NumberColumn(format="%.0f"),
    "gross $": st.column_config.NumberColumn(format="$%+.2f"),
    "fees $": st.column_config.NumberColumn(format="$%.2f"),
    "net $": st.column_config.NumberColumn(format="$%+.2f"),
    "ROI %": st.column_config.NumberColumn(format="%+.0f"),
    "avg NO px ¢": st.column_config.NumberColumn(format="%.1f"),
    "break-even": st.column_config.NumberColumn(format="%.1f"),
    "margin": st.column_config.NumberColumn(format="%+.1f"),
    "unfilled would-hit %": st.column_config.NumberColumn(format="%.0f"),
}


def _status_banner(text):
    if text.startswith("PASSING"):
        st.success("STATUS: " + text)
    elif text.startswith("FAILING"):
        st.error("STATUS: " + text)
    else:
        st.info("STATUS: " + text)


def _metrics(cum):
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Filled", f"{cum['filled']} / {weekly.K_MIN_FILLED}")
    m2.metric("Hit rate", "n/a" if cum["hit"] is None else f"{cum['hit']:.0f}%",
              None if cum["be"] is None else f"break-even {cum['be']:.1f}%")
    m3.metric("Margin (points)", "n/a" if cum["margin"] is None else f"{cum['margin']:+.1f}")
    m4.metric("Net after fees", f"${cum['net'] / 100:+.2f}")
    m5.metric("Unfilled would-hit", "n/a" if cum["unf_hit"] is None else f"{cum['unf_hit']:.0f}% ({cum['unf_n']})")


with tab_k:
    st.markdown(
        "**Book K is a pre-registered slice of Book A** (and B, the $100 copy) — not new orders. "
        "It asks: *do NO-side fades where Grok is low make money after fees?* "
        f"Rule (FROZEN): side = NO, Grok ≤ {weekly.K_MAX_GROK}, quote valid at booking, "
        f"|Grok − mid| strictly > {weekly.K_MIN_GAP}. "
        f"**K_HIGH** = booked mid ≥ {weekly.K_SPLIT_MID} (cheap NO tickets); **K_LOW** = mid < {weekly.K_SPLIT_MID} (dear ones). "
        f"Frozen until {weekly.K_MIN_FILLED} filled trades or {weekly.K_WINDOW_WEEKS} weeks."
    )
    if PANEL is None:
        st.error(f"Book K could not load: {PANEL_ERR}")
    else:
        kt = st.tabs(["K", "K_HIGH", "K_LOW", "A vs B at size", "Fills by Grok bucket", "Blend"])
        for tab, key_a, key_b, name in ((kt[0], "k", "kb", "K"), (kt[1], "k_high", "kb_high", "K_HIGH"), (kt[2], "k_low", "kb_low", "K_LOW")):
            with tab:
                pa, pb = PANEL[key_a], PANEL[key_b]
                _status_banner(pa["status"])
                _metrics(pa["cum"] if SEL == "All weeks" else next((r for r in pa["rows"] if r["label"] == SEL), pa["cum"]))
                st.caption(
                    "break-even hit % = average NO price paid + average fee per contract (points). margin = hit % − break-even. "
                    "PASSING needs margin ≥ +5. " + (f"Showing week {SEL}. " if SEL != "All weeks" else "")
                    + f"as of {PANEL['asof']}."
                )
                st.subheader(f"{name} · Book A ($1)")
                st.dataframe(_k_df(_pick(pa["rows"])), hide_index=True, use_container_width=True, column_config=CFG_K)
                st.subheader(f"{name} · Book B ($100 copy)")
                st.dataframe(_k_df(_pick(pb["rows"])), hide_index=True, use_container_width=True, column_config=CFG_K)
                if name == "K":
                    st.subheader("Same slice for Book I (NO, Grok ≤ 30, valid quote)")
                    st.dataframe(_k_df(_pick(PANEL["i"]["rows"])), hide_index=True, use_container_width=True, column_config=CFG_K)
                    with st.expander(f"The {len(pa['orders'])} Book K orders (Book A)", expanded=False):
                        st.dataframe(pd.DataFrame(pa["orders"]), hide_index=True, use_container_width=True) if pa["orders"] else st.write("none yet")

        with kt[3]:
            st.markdown("**Does the edge survive at size?** Book B is the same trades as Book A at $100 instead of $1. "
                        "'Survives' = B's margin within about 5 points of A's **and** B's fill % not more than 10 points lower.")
            recs = []
            for name, ka, kb in (("K", "k", "kb"), ("K_HIGH", "k_high", "kb_high"), ("K_LOW", "k_low", "kb_low")):
                a, b = PANEL[ka]["cum"], PANEL[kb]["cum"]
                for book, r in (("A ($1)", a), ("B ($100)", b)):
                    recs.append({"slice": name, "book": book, "booked": r["booked"], "filled": r["filled"], "fill % (contracts)": r["fill_ct"],
                                 "avg NO px ¢": r["px"], "hit %": r["hit"], "margin": r["margin"], "net $": r["net"] / 100.0})
                st.write(f"**{name}:** {weekly.ab_verdict(a, b)}")
            st.dataframe(pd.DataFrame(recs), hide_index=True, use_container_width=True, column_config={
                "fill % (contracts)": st.column_config.NumberColumn(format="%.0f"), "avg NO px ¢": st.column_config.NumberColumn(format="%.1f"),
                "hit %": st.column_config.NumberColumn(format="%.0f"), "margin": st.column_config.NumberColumn(format="%+.1f"),
                "net $": st.column_config.NumberColumn(format="$%+.2f")})

        with kt[4]:
            st.markdown("Every **filled, settled** order after fees, split by Grok's probability. "
                        "(In the live no-fade bot the same table showed 8 of 12 wins for Grok ≤ 30 against 10 of 37 above 30.)")
            scope = "CUMULATIVE" if SEL == "All weeks" else SEL
            brs = [r for r in PANEL["lab"]["buckets"] if r["scope"] == scope]
            if brs:
                st.dataframe(pd.DataFrame([{
                    "book": r["book"], "bucket": r["bucket"], "fills": r["fills"], "wins": r["wins"], "hit %": r["hit"],
                    "avg price ¢": r["avg_px"], "net $": r["net"]} for r in brs]), hide_index=True, use_container_width=True,
                    column_config={"hit %": st.column_config.NumberColumn(format="%.0f"), "avg price ¢": st.column_config.NumberColumn(format="%.1f"),
                                   "net $": st.column_config.NumberColumn(format="$%+.2f")})
            else:
                st.info("No filled orders for this selection yet.")

        with kt[5]:
            st.caption("blend = (Grok + market mid) / 2. Brier: lower is better. Grok − market < 0: Grok beat the market. "
                       "blend − market < 0: the blend beat the market. Valid-quote words only.")

            def _s_df(rows):
                return pd.DataFrame([{
                    "group": r["group"], "n": r["n"], "YES %": r["yes_pct"], "avg Grok": r["avg_grok"],
                    "Brier Grok": r["brier_grok"], "Brier market": r["brier_market"], "Brier blend": r["brier_blend"],
                    "Brier base rate": r["brier_base"], "Grok − market": r["grok_minus_market"], "blend − market": r["blend_minus_market"],
                } for r in rows])

            cfg_s = {c: st.column_config.NumberColumn(format="%.4f") for c in ("Brier Grok", "Brier market", "Brier blend", "Brier base rate")}
            cfg_s.update({"Grok − market": st.column_config.NumberColumn(format="%+.4f"), "blend − market": st.column_config.NumberColumn(format="%+.4f"),
                          "YES %": st.column_config.NumberColumn(format="%.0f"), "avg Grok": st.column_config.NumberColumn(format="%.1f")})
            st.write("**All weeks together**")
            st.dataframe(_s_df(PANEL["score"]["cumulative"]), hide_index=True, use_container_width=True, column_config=cfg_s)
            wk = [r for r in PANEL["score"]["by_week"] if SEL == "All weeks" or r["group"] == SEL]
            st.write("**Week by week**")
            st.dataframe(_s_df(wk), hide_index=True, use_container_width=True, column_config=cfg_s)

# ---------------------------------------------------------------------------
with tab_lab:
    st.markdown(
        "### Strategy lab — how could this grow an account, and how sure are we?\n"
        "Every row is a **report-only simulation**: it *takes at market* on the order book at the decision time "
        "(so fill timing does not matter), walks the book level by level, and charges Kalshi's fee. No orders are placed."
    )
    st.warning(
        f"**Read this first.** The data so far is a handful of nights. Weeks before **{lab.FROZEN_FROM}** are **in-sample** "
        "(these variants were designed while looking at them) — they cannot prove anything. Only later weeks are out-of-sample. "
        "The hit-rate range (90%) is wide on purpose; a strategy needs **30+ trades** before a verdict means much. "
        "The grid below is *hypotheses for next week*, not results."
    )
    if PANEL is None:
        st.error(f"Strategy lab could not load: {PANEL_ERR}")
    else:
        L = PANEL["lab"]
        words_all = L["words"]
        words = words_all if SEL == "All weeks" else [w for w in words_all if w["week"] == SEL]
        cov = L["coverage"]
        st.caption(f"{len(words)} words in view · {cov['with_book']} of {cov['words']} words have a decision-time book · "
                   f"{cov['valid']} with a valid quote · {'all weeks' if SEL == 'All weeks' else 'week ' + SEL}")

        size = st.select_slider("Dollars per trade", options=list(lab.SIZES), value=25, key="lab_size")
        board_rows = lab.leaderboard(words, float(size))
        st.subheader("1 · Leaderboard")
        recs = []
        for r in board_rows:
            a = r["all"]
            rng = "n/a" if a["lo"] is None else f"{a['lo']:.0f}–{a['hi']:.0f}"
            recs.append({
                "variant": r["id"], "kind": r["family"], "idea": r["label"], "trades": a["trades"], "fill %": a["filled_pct"],
                "hit %": a["hit"], "90% range": rng, "break-even %": a["be"], "margin (pts)": a["margin"],
                "net $": a["net"], "ROI %": a["roi"], "verdict": a["verdict"],
                "in-sample trades": r["in_sample"]["trades"], "out-of-sample trades": r["out_of_sample"]["trades"],
                "note": r["note"],
            })
        st.dataframe(pd.DataFrame(recs), hide_index=True, use_container_width=True, column_config={
            "fill %": st.column_config.NumberColumn(format="%.0f"), "hit %": st.column_config.NumberColumn(format="%.0f"),
            "break-even %": st.column_config.NumberColumn(format="%.1f"), "margin (pts)": st.column_config.NumberColumn(format="%+.1f"),
            "net $": st.column_config.NumberColumn(format="$%+.2f"), "ROI %": st.column_config.NumberColumn(format="%+.0f")})
        st.caption("margin = hit % − break-even %. break-even = average price paid + fee per contract. "
                   "Baselines: GROK_ONLY ignores the market; ALL_NO buys NO on everything — the real strategies must beat these.")

        st.subheader("2 · K_HIGH size sweep (pre-registered)")
        st.caption(f"Pass = at ${lab.SWEEP_PASS_SIZE}: filled ≥ {lab.SWEEP_PASS_FILL:.0f}% **and** margin ≥ +{lab.SWEEP_PASS_MARGIN:.0f} points.")
        _status_banner(L["sweep_pass"]["status"]) if L["sweep_pass"]["status"].startswith(("PASSING", "NOT")) else st.info("STATUS: " + L["sweep_pass"]["status"])
        scope = "CUMULATIVE" if SEL == "All weeks" else SEL
        sw = [r for r in L["sweep"] if r["scope"] == scope]
        if sw:
            st.dataframe(pd.DataFrame([{
                "size $": r["size"], "signals": r["signals"], "trades": r["trades"], "filled %": r["filled_pct"], "avg NO px ¢": r["avg_px"],
                "fee/contract ¢": r["fee_pc"], "break-even %": r["be"], "hit %": r["hit"],
                "90% range": "n/a" if r["lo"] is None else f"{r['lo']:.0f}–{r['hi']:.0f}", "margin (pts)": r["margin"], "net $": r["net"]} for r in sw]),
                hide_index=True, use_container_width=True, column_config={
                    "filled %": st.column_config.NumberColumn(format="%.0f"), "avg NO px ¢": st.column_config.NumberColumn(format="%.1f"),
                    "fee/contract ¢": st.column_config.NumberColumn(format="%.1f"), "break-even %": st.column_config.NumberColumn(format="%.1f"),
                    "hit %": st.column_config.NumberColumn(format="%.0f"), "margin (pts)": st.column_config.NumberColumn(format="%+.1f"),
                    "net $": st.column_config.NumberColumn(format="$%+.2f")})
        else:
            st.info("No K_HIGH candidates with a decision-time book for this selection yet.")

        st.subheader("3 · Capacity: how much can the book absorb?")
        st.caption("Dollars of NO you could buy from YES bids at or above each floor (top 10 levels). Blank = no book. "
                   "This is the ceiling on account size for this segment.")
        cap = [r for r in L["capacity"]]
        st.dataframe(pd.DataFrame([{"group": r["group"], "YES bid ≥": r["floor"], "words": r["n"], "median $": r["median"],
                                    "p75 $": r["p75"], "p90 $": r["p90"]} for r in cap]), hide_index=True, use_container_width=True)
        seg = [r for r in L["segments"] if SEL == "All weeks" or weekly._week_id(r["date"]) == SEL]
        st.write("**K_HIGH candidates per night** (Grok ≤ 30 and a valid decision-time mid ≥ 55):")
        st.dataframe(pd.DataFrame([{"night": r["date"], "candidates": r["candidates"], "booked by A": r["booked_by_A"],
                                    "filled": r["filled"], "words": r["words"], "words without a book": r["no_book"]} for r in seg]),
                     hide_index=True, use_container_width=True)

        st.subheader("4 · Exploratory grid: NO when Grok ≤ g and the market is high")
        st.caption("$25 per trade, market must be more than 15 above Grok. **Exploratory** — 20 cells will always show some winners by luck.")
        grid = lab.grid_no(words)
        gdf = pd.DataFrame(grid)
        if not gdf.empty and gdf["trades"].sum():
            piv = gdf.pivot(index="grok_max", columns="mid_min", values="margin")
            cnt = gdf.pivot(index="grok_max", columns="mid_min", values="trades")
            st.write("margin (points above break-even)")
            st.dataframe(piv.rename_axis("Grok ≤").rename_axis("market mid ≥", axis=1).round(1), use_container_width=True)
            st.write("number of trades in each cell")
            st.dataframe(cnt.rename_axis("Grok ≤").rename_axis("market mid ≥", axis=1), use_container_width=True)
        else:
            st.info("Not enough data for the grid yet.")

        st.subheader("5 · Account growth simulator")
        st.caption("Two views: (a) replay the real nights in order with compounding, (b) a forward Monte Carlo that shows the SPREAD of outcomes "
                   "for a hit rate you choose. Neither predicts the future — they show how much depends on the edge being real.")
        ids = [r["id"] for r in board_rows]
        c1, c2, c3 = st.columns(3)
        vid = c1.selectbox("Strategy", ids, index=ids.index("K_HIGH") if "K_HIGH" in ids else 0)
        bank0 = c2.number_input("Starting bankroll $", min_value=10.0, value=1000.0, step=100.0)
        mode = c3.radio("Sizing", ["Flat $ per trade", "% of bankroll per trade"], horizontal=True)
        s1, s2 = st.columns(2)
        if mode.startswith("Flat"):
            val = s1.number_input("$ per trade", min_value=1.0, value=50.0, step=5.0)
        else:
            val = s1.slider("% of bankroll per trade", 0.5, 25.0, 5.0, 0.5)
        night_cap = s2.slider("Max % of bankroll at risk in one night", 5, 100, 50, 5)
        fn = next(r for r in lab.REGISTRY if r[0] == vid)[2]
        rp = lab.replay(words, fn, bank0, "flat" if mode.startswith("Flat") else "pct", float(val), float(night_cap))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Final bankroll", f"${rp['final']:,.2f}", f"{rp['return_pct']:+.1f}%" if rp["return_pct"] is not None else None)
        m2.metric("Trades", rp["trades"])
        m3.metric("Worst night", f"${rp['worst_night']:+,.2f}")
        m4.metric("Max drawdown", f"{rp['max_drawdown_pct']:.1f}%")
        st.line_chart(pd.DataFrame(rp["curve"]).set_index("date")["bankroll"])
        st.caption("Each trade is walked through the real decision-time book, so a big size only fills what the book holds "
                   "(that is the capacity ceiling in action).")

        g = lab.growth_summary(words, fn)
        gs = g["stats"]
        st.markdown("**Forward Monte Carlo** — what could happen if this keeps going")
        if gs["trades"] < 3 or gs["avg_px"] is None:
            st.info("Too few trades in this selection to simulate.")
        else:
            opts = {
                f"Observed hit rate ({gs['hit']:.0f}%)": gs["hit"],
                f"Low end of the 90% range ({gs['lo']:.0f}%)": gs["lo"],
                f"Observed minus 10 points ({max(0.0, gs['hit'] - 10):.0f}%)": max(0.0, gs["hit"] - 10),
                f"No edge: break-even ({gs['be']:.0f}%)": gs["be"],
            }
            m_a, m_b, m_c = st.columns(3)
            pick_h = m_a.selectbox("Assume the true hit rate is", list(opts.keys()), index=1)
            nights_f = m_b.slider("Nights to simulate", 10, 260, 130, 10)
            stake = m_c.slider("% of bankroll per trade", 0.5, 25.0, 3.0, 0.5, key="mc_stake")
            tpn = st.number_input("Trades per night", min_value=0.1, value=float(max(0.5, round(g["trades_per_night"], 1))), step=0.5)
            mc = lab.monte_carlo(opts[pick_h], gs["avg_px"], gs["fee_pc"] or 0.0, tpn, int(nights_f), float(bank0), float(stake))
            q1, q2, q3, q4 = st.columns(4)
            q1.metric("Typical (median)", f"${mc['p50']:,.0f}")
            q2.metric("Bad luck (10th pct)", f"${mc['p10']:,.0f}")
            q3.metric("Good luck (90th pct)", f"${mc['p90']:,.0f}")
            q4.metric("Chance of ending below start", f"{100 * mc['prob_loss']:.0f}%")
            kel = lab.kelly_fraction(opts[pick_h], gs["avg_px"], gs["fee_pc"] or 0.0)
            st.caption(
                f"At that hit rate the fastest-growing bet (full Kelly) would be **{100 * kel:.0f}%** of the bankroll per trade; "
                f"a quarter of that is **{25 * kel:.1f}%**. Full Kelly is very aggressive and assumes the hit rate is known exactly. "
                f"Chance of at least doubling: {100 * mc['prob_2x']:.0f}%. Simulated with {mc['per_night']} trade(s) per night at about "
                f"{gs['avg_px']:.0f}¢ per contract, fees included."
            )
            if opts[pick_h] <= (gs["be"] or 0):
                st.warning("At this hit rate there is no edge, so expect the account to shrink over time.")

        st.subheader("6 · What would settle it")
        st.markdown(
            "- **K / K_HIGH** need **30 filled trades** (about 6 weeks at the current pace) — the status line on the *Book K* tab is the verdict.\n"
            f"- Every variant here is scored on **new weeks only** from {lab.FROZEN_FROM}; the week picker in the sidebar shows one week at a time.\n"
            "- Growth is capped by the book: see *Capacity*. Sizing up beyond what the book holds does not scale.\n"
            "- The two baselines (GROK_ONLY, ALL_NO) are the yardsticks: a variant that does not beat them is not adding anything."
        )

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
