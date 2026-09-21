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


# ---------------------------------------------------------------------------
# Copy-as-Markdown for every tab: each show_* helper draws the thing AND records it,
# so the whole tab can be copied with one click and pasted into a chat (no screenshots).
# ---------------------------------------------------------------------------
def _cell(v):
    if v is None:
        return ""
    try:
        if v != v:  # NaN
            return ""
    except Exception:
        pass
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e9 else str(v)
    return str(v).replace("|", "/").replace("\n", " ")


def df_to_md(df: pd.DataFrame) -> str:
    if df is None or len(df) == 0:
        return "_(empty)_"
    df = df.reset_index() if (df.index.name or not isinstance(df.index, pd.RangeIndex)) else df
    cols = [str(c) for c in df.columns]
    out = ["| " + " | ".join(cols) + " |", "|" + "|".join("---" for _ in cols) + "|"]
    for row in df.itertuples(index=False):
        out.append("| " + " | ".join(_cell(v) for v in row) + " |")
    return "\n".join(out)


class Raw:
    """Wraps ready-made Markdown text so copy_box can take it."""
    def __init__(self, text):
        self._t = text

    def text(self):
        return self._t


class MD:
    def __init__(self, title: str):
        self.title = title
        self.parts = [f"# {title}", f"_{C.VERSION} · as of {clock.fmt(clock.now_ct())}_"]

    def h(self, text):
        self.parts.append(f"\n## {text}")

    def p(self, text):
        self.parts.append(str(text))

    def df(self, df):
        self.parts.append(df_to_md(df))

    def code(self, text):
        self.parts.append("```\n" + str(text) + "\n```")

    def text(self):
        return "\n\n".join(self.parts)


def show_h(md, text):
    st.subheader(text)
    md.h(text)


def show_p(md, text):
    st.markdown(text)
    md.p(text)


def show_cap(md, text):
    st.caption(text)
    md.p("_" + str(text) + "_")


def show_df(md, df, **kw):
    kw.setdefault("hide_index", True)
    kw.setdefault("use_container_width", True)
    st.dataframe(df, **kw)
    md.df(df)


def show_note(md, kind, text):
    getattr(st, kind)(text)
    md.p(f"> **{kind.upper()}:** {text}")


def show_metrics(md, items):
    """items = [(label, value, delta_or_None), ...] drawn as one row of metrics."""
    cols = st.columns(len(items))
    for col, (label, value, delta) in zip(cols, items):
        col.metric(label, value, delta)
    md.p("  \n".join(f"**{label}:** {value}" + (f" ({delta})" if delta else "") for label, value, delta in items))


def copy_box(slot, md, key, filename):
    """The copy button lives at the TOP of the tab (slot), filled after the tab has been drawn."""
    with slot:
        text = md.text()
        with st.expander("📋 Copy this whole page as Markdown (click the copy icon in the box, then paste)", expanded=False):
            st.code(text, language="markdown")
        st.download_button("Download this page as .md", data=text, file_name=filename, mime="text/markdown", key=key)


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
    slot_live = st.container()
    md = MD("Tonight")
    if C.PAPER:
        show_note(md, "info", "**PAPER** — capped-sweep rows only. No Kalshi place_order.")
    elif C.may_place_live():
        show_note(md, "error", "**LIVE** — Phase 1 should not be here.")
    else:
        show_note(md, "warning", "Live flags mixed. Placement blocked.")

    st.code(C.summary())
    md.code(C.summary())
    date_str = clock.today_ct()
    run = store.get_run_for_date(date_str)
    send_label = clock.fmt(clock.decision_at(date_str))
    if run:
        due = clock.send_due_at(run.get("market_open_at") or run.get("created_at"))
        if due:
            send_label = clock.fmt(due)
    show_metrics(md, [
        ("Today CT", date_str, None),
        ("File send", send_label, None),
        ("Run", str(run.get("status")) if run else "none", None),
        ("Markets", str(run.get("markets_n") or 0) if run else "—", None),
    ])

    if not run:
        show_p(md, f"No event yet. Poll starts {C.POLL_START_CT} CT. File sends {C.DECISION_LAG_MIN} min after first detect.")
    elif run.get("status") == "detected":
        show_note(md, "warning", "Event seen. Telegram file waits until " + send_label + ". /gap_sendnow skips the wait.")
    else:
        show_p(md, f"`{run.get('event_ticker')}` · `{run.get('harness')}` · `{run.get('prompt_version')}`")
        if run.get("parse_error"):
            show_note(md, "error", run["parse_error"])
        markets = store.markets_for_run(run["id"])
        if markets:
            show_df(md, pd.DataFrame(markets)[["word", "market_ticker", "title"]])

    act = store.recent_activity(20)
    if act:
        show_h(md, "Activity")
        show_df(md, pd.DataFrame(act)[["at", "kind", "message"]])
    copy_box(slot_live, md, "md_live", f"gap-tonight-{clock.today_ct()}.md")

# ---------------------------------------------------------------------------
with tab_books:
    slot_books = st.container()
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

        st.caption(
            f"{snap['n_words']} words · {snap['n_orders']} tickets · "
            "Refresh checks Kalshi for new settlements"
        )
        copy_box(slot_books, Raw(board.as_markdown(snap)), "md_books", f"gap-board-{date_str}.md")
    if not rows:
        copy_box(slot_books, Raw(f"# Books · P&L\n\nNo paper orders yet for {date_str}."), "md_books_empty", f"gap-board-{date_str}.md")

# ---------------------------------------------------------------------------
with tab_four:
    slot_four = st.container()
    md = MD("All nights · books" + ("" if SEL == "All weeks" else f" · {SEL}"))
    show_p(md, "Running paper totals across every night in the database. "
               "This is the paper books on real booked tickets, not the old 128-night fixture. "
               "No live marks here either — unsettled nights show *pending*. "
               "**Book B/F/H are the $100 copies: in W38 their fills came from the old paper model (see the Book K tab) and are NOT proof that $100 fills.**")
    hist = board.history()
    if SEL != "All weeks":
        _rows = [r for r in hist["rows"] if r.get("event_date") and weekly._week_id(str(r["event_date"])[:10]) == SEL]
        hist = {**hist, "rows": _rows, "books": board.summarize(_rows),
                "nights": sorted({str(r["event_date"])[:10] for r in _rows}), "n_orders": len(_rows),
                "n_words": len({(str(r["event_date"])[:10], r.get("word")) for r in _rows})}
        show_note(md, "info", f"Showing only week {SEL} (change it in the sidebar).")
    books = hist["books"]
    show_cap(md, f"{len(hist['nights'])} nights · {hist['n_orders']} tickets · {hist['n_words']} word-nights")
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
        show_df(md, cmp)
    else:
        show_note(md, "info", "No paper tickets stored yet.")
    copy_box(slot_four, md, "md_four", f"gap-all-nights-{SEL}.md")

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

CFG_SWEEP = {
    "filled %": st.column_config.NumberColumn(format="%.0f"), "avg NO px ¢": st.column_config.NumberColumn(format="%.1f"),
    "fee/contract ¢": st.column_config.NumberColumn(format="%.1f"), "break-even %": st.column_config.NumberColumn(format="%.1f"),
    "hit %": st.column_config.NumberColumn(format="%.0f"), "margin (pts)": st.column_config.NumberColumn(format="%+.1f"),
    "net $": st.column_config.NumberColumn(format="$%+.2f"),
}


def _sweep_df(rows):
    return pd.DataFrame([{
        "size $": r["size"], "signals": r["signals"], "trades": r["trades"], "filled %": r["filled_pct"], "avg NO px ¢": r["avg_px"],
        "fee/contract ¢": r["fee_pc"], "break-even %": r["be"], "hit %": r["hit"],
        "90% range": "n/a" if r["lo"] is None else f"{r['lo']:.0f}–{r['hi']:.0f}", "margin (pts)": r["margin"], "net $": r["net"]} for r in rows])


def _status_banner(md, text):
    kind = "success" if text.startswith("PASSING") else ("error" if text.startswith(("FAILING", "NOT PASSING")) else "info")
    show_note(md, kind, "STATUS: " + text)


def _metrics(md, cum):
    show_metrics(md, [
        ("Filled", f"{cum['filled']} / {weekly.K_MIN_FILLED}", None),
        ("Hit rate", "n/a" if cum["hit"] is None else f"{cum['hit']:.0f}%", None if cum["be"] is None else f"break-even {cum['be']:.1f}%"),
        ("Margin (points)", "n/a" if cum["margin"] is None else f"{cum['margin']:+.1f}", None),
        ("Net after fees", f"${cum['net'] / 100:+.2f}", None),
        ("Unfilled would-hit", "n/a" if cum["unf_hit"] is None else f"{cum['unf_hit']:.0f}% ({cum['unf_n']})", None),
    ])


B_WARNING = (
    "**Book B ($100) is NOT proof that $100 fills.** In W38 the old paper model counted the same displayed liquidity on every "
    "poll, so B 'filled' huge sizes against a thin book — that is why it looks exactly like A × 100. From v1.5.6 an order can only take "
    "the size it actually sees. **Second reason B mirrors A:** every paper order is priced at its own limit price whatever its size, "
    "so dollars just scale. A real $100 order eats several price levels and pays worse prices as it goes. "
    "**The real-size test is the table 'taking at market, walking the order book' on each tab.**"
)

with tab_k:
    slot_k = st.container()
    md = MD("Book K + blend" + ("" if SEL == "All weeks" else f" · {SEL}"))
    show_p(md,
        "**Book K is a pre-registered slice of Book A** — not new orders. "
        "It asks: *do NO-side fades where Grok is low make money after fees?* "
        f"Rule (FROZEN): side = NO, Grok ≤ {weekly.K_MAX_GROK}, quote valid at booking, "
        f"|Grok − mid| strictly > {weekly.K_MIN_GAP}. "
        f"**K_HIGH** = booked mid ≥ {weekly.K_SPLIT_MID} (cheap NO tickets); **K_LOW** = mid < {weekly.K_SPLIT_MID} (dear ones). "
        f"Frozen until {weekly.K_MIN_FILLED} filled trades or {weekly.K_WINDOW_WEEKS} weeks."
    )
    if PANEL is None:
        show_note(md, "error", f"Book K could not load: {PANEL_ERR}")
    else:
        kt = st.tabs(["K", "K_HIGH", "K_LOW", "A vs B at size", "Fills by Grok bucket", "Blend", "Book M vs K_HIGH"])
        for tab, key_a, key_b, name in ((kt[0], "k", "kb", "K"), (kt[1], "k_high", "kb_high", "K_HIGH"), (kt[2], "k_low", "kb_low", "K_LOW")):
            with tab:
                pa, pb = PANEL[key_a], PANEL[key_b]
                md.h(f"{name}")
                _status_banner(md, pa["status"])
                _metrics(md, pa["cum"] if SEL == "All weeks" else next((r for r in pa["rows"] if r["label"] == SEL), pa["cum"]))
                show_cap(md,
                    "break-even hit % = average NO price paid + average fee per contract (points). margin = hit % − break-even. "
                    "PASSING needs margin ≥ +5. " + (f"Showing week {SEL}. " if SEL != "All weeks" else "")
                    + f"as of {PANEL['asof']}.")
                show_h(md, f"{name} · Book A ($1) — what the paper orders did")
                show_df(md, _k_df(_pick(pa["rows"])), column_config=CFG_K)

                show_h(md, f"{name} at REAL SIZE — taking at market, walking the order book")
                show_cap(md,
                    "This is the honest size test. For every qualifying word we take NO from the order book at the decision time: "
                    "best price first, then worse, until the dollars run out or the top 10 levels are gone. "
                    "'filled %' below 100 means the book could not hold that size; the price paid rises as it walks.")
                scope = "CUMULATIVE" if SEL == "All weeks" else SEL
                sw = [r for r in PANEL["lab"]["sweeps"][name] if r["scope"] == scope]
                if sw:
                    show_df(md, _sweep_df(sw), column_config=CFG_SWEEP)
                else:
                    show_note(md, "info", "No candidates with a decision-time order book for this selection yet.")

                with st.expander(f"{name} · Book B ($100 copy) — paper model, NOT real size", expanded=False):
                    show_note(md, "warning", B_WARNING)
                    show_df(md, _k_df(_pick(pb["rows"])), column_config=CFG_K)
                if name == "K":
                    show_h(md, "Same slice for Book I (NO, Grok ≤ 30, valid quote)")
                    show_df(md, _k_df(_pick(PANEL["i"]["rows"])), column_config=CFG_K)
                    with st.expander(f"The {len(pa['orders'])} Book K orders (Book A)", expanded=False):
                        if pa["orders"]:
                            show_df(md, pd.DataFrame(pa["orders"]))
                        else:
                            show_p(md, "none yet")

        with kt[3]:
            md.h("A vs B at size")
            show_note(md, "warning", B_WARNING)
            recs = []
            for name, ka, kb in (("K", "k", "kb"), ("K_HIGH", "k_high", "kb_high"), ("K_LOW", "k_low", "kb_low")):
                a, b = PANEL[ka]["cum"], PANEL[kb]["cum"]
                for book, r in (("A ($1)", a), ("B ($100)", b)):
                    recs.append({"slice": name, "book": book, "booked": r["booked"], "filled": r["filled"], "fill % (contracts)": r["fill_ct"],
                                 "avg NO px ¢": r["px"], "hit %": r["hit"], "margin": r["margin"], "net $": r["net"] / 100.0})
                show_p(md, f"**{name}:** {weekly.ab_verdict(a, b)}")
            show_df(md, pd.DataFrame(recs), column_config={
                "fill % (contracts)": st.column_config.NumberColumn(format="%.0f"), "avg NO px ¢": st.column_config.NumberColumn(format="%.1f"),
                "hit %": st.column_config.NumberColumn(format="%.0f"), "margin": st.column_config.NumberColumn(format="%+.1f"),
                "net $": st.column_config.NumberColumn(format="$%+.2f")})
            show_p(md, "**Recomputed:** the same K orders replayed against the stored depth history, taking only the size the book showed "
                       "(booking time + the cancel window). If B's 'real ct' is far below its 'paper ct', the paper fills were inflated.")
            rc = PANEL.get("recompute") or []
            if rc:
                show_df(md, pd.DataFrame([{
                    "night": r["date"], "book": r["book"], "word": r["word"], "want ct": r["intended"], "paper filled ct": r["paper_filled"],
                    "real filled ct": r.get("real_filled"), "paper net $": r["paper_net"], "real net $": r.get("real_net"),
                    "note": r.get("note", "")} for r in rc]))
            else:
                show_note(md, "info", "No K orders to recompute yet.")

        with kt[4]:
            md.h("Fills by Grok bucket")
            show_p(md, "Every **filled, settled** order after fees, split by Grok's probability. "
                       "(In the live no-fade bot the same table showed 8 of 12 wins for Grok ≤ 30 against 10 of 37 above 30.)")
            scope = "CUMULATIVE" if SEL == "All weeks" else SEL
            brs = [r for r in PANEL["lab"]["buckets"] if r["scope"] == scope]
            if brs:
                show_df(md, pd.DataFrame([{
                    "book": r["book"], "bucket": r["bucket"], "fills": r["fills"], "wins": r["wins"], "hit %": r["hit"],
                    "avg price ¢": r["avg_px"], "net $": r["net"]} for r in brs]),
                    column_config={"hit %": st.column_config.NumberColumn(format="%.0f"), "avg price ¢": st.column_config.NumberColumn(format="%.1f"),
                                   "net $": st.column_config.NumberColumn(format="$%+.2f")})
            else:
                show_note(md, "info", "No filled orders for this selection yet.")

        with kt[5]:
            md.h("Blend")
            show_cap(md, "blend = (Grok + market mid) / 2. Brier: lower is better. Grok − market < 0: Grok beat the market. "
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
            show_p(md, "**All weeks together**")
            show_df(md, _s_df(PANEL["score"]["cumulative"]), column_config=cfg_s)
            wk = [r for r in PANEL["score"]["by_week"] if SEL == "All weeks" or r["group"] == SEL]
            show_p(md, "**Week by week**")
            show_df(md, _s_df(wk), column_config=cfg_s)
        with kt[6]:
            md.h("Book M vs K_HIGH")
            show_p(md,
                f"**Book M** (pre-registered, nothing to tune): buy NO at market on **every** word with a valid frozen YES bid ≥ {lab.M_MIN_YES_BID}, "
                "**regardless of Grok**. Fill = walk the YES-bid book from the best bid down, fees included, $1 per trade. "
                "It sits next to K_HIGH (the Grok-low words). **M_GROKLOW** = M words where Grok is also low; **M_REST** = M words where Grok is not low.")
            show_note(md, "info", "How to read it: if M is at break-even and K_HIGH is above it, Grok is what makes taking at market work. "
                                   "If M also clears break-even, there is a bigger, deeper edge that does not need Grok.")
            mreading = PANEL["lab"]["m_reading"]
            show_note(md, "success" if mreading.startswith("M clears") else "info", "READING: " + mreading)
            mscope = "CUMULATIVE" if SEL == "All weeks" else SEL
            mrows = [r for r in PANEL["lab"]["m"] if r["scope"] == mscope]
            if mrows and any(r["trades"] for r in mrows):
                show_df(md, pd.DataFrame([{
                    "variant": r["variant"], "trades": r["trades"], "avg NO px ¢": r["avg_px"], "fee/contract ¢": r["fee_pc"],
                    "break-even %": r["be"], "hit %": r["hit"], "90% range": "n/a" if r["lo"] is None else f"{r['lo']:.0f}–{r['hi']:.0f}",
                    "margin (pts)": r["margin"], "net $": r["net"], "ROI %": r["roi"], "verdict": r["verdict"]} for r in mrows]),
                    column_config={"avg NO px ¢": st.column_config.NumberColumn(format="%.1f"), "fee/contract ¢": st.column_config.NumberColumn(format="%.1f"),
                                   "break-even %": st.column_config.NumberColumn(format="%.1f"), "hit %": st.column_config.NumberColumn(format="%.0f"),
                                   "margin (pts)": st.column_config.NumberColumn(format="%+.1f"), "net $": st.column_config.NumberColumn(format="$%+.2f"),
                                   "ROI %": st.column_config.NumberColumn(format="%+.0f")})
            else:
                show_note(md, "info", "No words with a decision-time order book and a YES bid ≥ 60 for this selection yet.")
            show_h(md, "Book M at REAL SIZE — taking at market, walking the order book")
            msw = [r for r in PANEL["lab"]["sweeps"]["M"] if r["scope"] == mscope]
            if msw:
                show_df(md, _sweep_df(msw), column_config=CFG_SWEEP)
    copy_box(slot_k, md, "md_k", f"gap-book-k-{SEL}.md")

# ---------------------------------------------------------------------------
with tab_lab:
    slot_lab = st.container()
    md = MD("Strategy lab" + ("" if SEL == "All weeks" else f" · {SEL}"))
    show_p(md,
        "### Strategy lab — how could this grow an account, and how sure are we?\n"
        "Every row is a **report-only simulation**: it *takes at market* on the order book at the decision time "
        "(so fill timing does not matter), walks the book level by level, and charges Kalshi's fee. No orders are placed."
    )
    show_note(md, "warning",
        f"**Read this first.** The data so far is a handful of nights. Weeks before **{lab.FROZEN_FROM}** are **in-sample** "
        "(these variants were designed while looking at them) — they cannot prove anything. Only later weeks are out-of-sample. "
        "The hit-rate range (90%) is wide on purpose; a strategy needs **30+ trades** before a verdict means much. "
        "The grid below is *hypotheses for next week*, not results."
    )
    if PANEL is None:
        show_note(md, "error", f"Strategy lab could not load: {PANEL_ERR}")
    else:
        L = PANEL["lab"]
        words_all = L["words"]
        words = words_all if SEL == "All weeks" else [w for w in words_all if w["week"] == SEL]
        cov = L["coverage"]
        show_cap(md, f"{len(words)} words in view · {cov['with_book']} of {cov['words']} words have a decision-time book · "
                     f"{cov['valid']} with a valid quote · {'all weeks' if SEL == 'All weeks' else 'week ' + SEL}")

        size = st.select_slider("Dollars per trade", options=list(lab.SIZES), value=25, key="lab_size")
        md.p(f"**Dollars per trade:** ${size}")
        board_rows = lab.leaderboard(words, float(size))
        show_h(md, "1 · Leaderboard")
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
        show_df(md, pd.DataFrame(recs), column_config={
            "fill %": st.column_config.NumberColumn(format="%.0f"), "hit %": st.column_config.NumberColumn(format="%.0f"),
            "break-even %": st.column_config.NumberColumn(format="%.1f"), "margin (pts)": st.column_config.NumberColumn(format="%+.1f"),
            "net $": st.column_config.NumberColumn(format="$%+.2f"), "ROI %": st.column_config.NumberColumn(format="%+.0f")})
        show_cap(md, "margin = hit % − break-even %. break-even = average price paid + fee per contract. "
                     "Baselines: GROK_ONLY ignores the market; ALL_NO buys NO on everything — the real strategies must beat these.")

        show_h(md, "2 · K_HIGH size sweep (pre-registered) — the real-size test")
        show_cap(md, f"Pass = at ${lab.SWEEP_PASS_SIZE}: filled ≥ {lab.SWEEP_PASS_FILL:.0f}% **and** margin ≥ +{lab.SWEEP_PASS_MARGIN:.0f} points. "
                     "The book is walked level by level, so bigger sizes pay worse prices and may not fully fill.")
        _status_banner(md, L["sweep_pass"]["status"])
        scope = "CUMULATIVE" if SEL == "All weeks" else SEL
        sw = [r for r in L["sweep"] if r["scope"] == scope]
        if sw:
            show_df(md, _sweep_df(sw), column_config=CFG_SWEEP)
        else:
            show_note(md, "info", "No K_HIGH candidates with a decision-time book for this selection yet.")

        show_h(md, "3 · Capacity: how much can the book absorb?")
        show_cap(md, "Dollars of NO you could buy from YES bids at or above each floor (top 10 levels). Blank = no book. "
                     "This is the ceiling on account size for this segment.")
        show_df(md, pd.DataFrame([{"group": r["group"], "YES bid ≥": r["floor"], "words": r["n"], "median $": r["median"],
                                   "p75 $": r["p75"], "p90 $": r["p90"]} for r in L["capacity"]]))
        seg = [r for r in L["segments"] if SEL == "All weeks" or weekly._week_id(r["date"]) == SEL]
        show_p(md, "**K_HIGH candidates per night** (Grok ≤ 30 and a valid decision-time mid ≥ 55):")
        show_df(md, pd.DataFrame([{"night": r["date"], "candidates": r["candidates"], "booked by A": r["booked_by_A"],
                                   "filled": r["filled"], "words": r["words"], "words without a book": r["no_book"]} for r in seg]))

        show_h(md, "4 · Exploratory grid: NO when Grok ≤ g and the market is high")
        show_cap(md, "$25 per trade, market must be more than 15 above Grok. **Exploratory** — 20 cells will always show some winners by luck.")
        gdf = pd.DataFrame(lab.grid_no(words))
        if not gdf.empty and gdf["trades"].sum():
            piv = gdf.pivot(index="grok_max", columns="mid_min", values="margin").rename_axis("Grok ≤").rename_axis("market mid ≥", axis=1).round(1)
            cnt = gdf.pivot(index="grok_max", columns="mid_min", values="trades").rename_axis("Grok ≤").rename_axis("market mid ≥", axis=1)
            show_p(md, "margin (points above break-even)")
            st.dataframe(piv, use_container_width=True)
            md.df(piv)
            show_p(md, "number of trades in each cell")
            st.dataframe(cnt, use_container_width=True)
            md.df(cnt)
        else:
            show_note(md, "info", "Not enough data for the grid yet.")

        show_h(md, "5 · Account growth simulator")
        show_cap(md, "Two views: (a) replay the real nights in order with compounding, (b) a forward Monte Carlo that shows the SPREAD of outcomes "
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
        md.p(f"**Simulator settings:** strategy {vid} · start ${bank0:,.0f} · {mode}: {val} · night cap {night_cap}%")
        fn = next(r for r in lab.REGISTRY if r[0] == vid)[2]
        rp = lab.replay(words, fn, bank0, "flat" if mode.startswith("Flat") else "pct", float(val), float(night_cap))
        show_metrics(md, [
            ("Final bankroll", f"${rp['final']:,.2f}", f"{rp['return_pct']:+.1f}%" if rp["return_pct"] is not None else None),
            ("Trades", str(rp["trades"]), None),
            ("Worst night", f"${rp['worst_night']:+,.2f}", None),
            ("Max drawdown", f"{rp['max_drawdown_pct']:.1f}%", None),
        ])
        curve_df = pd.DataFrame(rp["curve"])
        st.line_chart(curve_df.set_index("date")["bankroll"])
        md.df(curve_df)
        show_cap(md, "Each trade is walked through the real decision-time book, so a big size only fills what the book holds "
                     "(that is the capacity ceiling in action).")

        g = lab.growth_summary(words, fn)
        gs = g["stats"]
        show_p(md, "**Forward Monte Carlo** — what could happen if this keeps going")
        if gs["trades"] < 3 or gs["avg_px"] is None:
            show_note(md, "info", "Too few trades in this selection to simulate.")
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
            md.p(f"**Monte Carlo settings:** {pick_h} · {nights_f} nights · {stake}% per trade · {tpn} trades/night")
            mc = lab.monte_carlo(opts[pick_h], gs["avg_px"], gs["fee_pc"] or 0.0, tpn, int(nights_f), float(bank0), float(stake))
            show_metrics(md, [
                ("Typical (median)", f"${mc['p50']:,.0f}", None),
                ("Bad luck (10th pct)", f"${mc['p10']:,.0f}", None),
                ("Good luck (90th pct)", f"${mc['p90']:,.0f}", None),
                ("Chance of ending below start", f"{100 * mc['prob_loss']:.0f}%", None),
            ])
            kel = lab.kelly_fraction(opts[pick_h], gs["avg_px"], gs["fee_pc"] or 0.0)
            show_cap(md,
                f"At that hit rate the fastest-growing bet (full Kelly) would be **{100 * kel:.0f}%** of the bankroll per trade; "
                f"a quarter of that is **{25 * kel:.1f}%**. Full Kelly is very aggressive and assumes the hit rate is known exactly. "
                f"Chance of at least doubling: {100 * mc['prob_2x']:.0f}%. Simulated with {mc['per_night']} trade(s) per night at about "
                f"{gs['avg_px']:.0f}¢ per contract, fees included.")
            if opts[pick_h] <= (gs["be"] or 0):
                show_note(md, "warning", "At this hit rate there is no edge, so expect the account to shrink over time.")

        show_h(md, "6 · What would settle it")
        show_p(md,
            "- **K / K_HIGH** need **30 filled trades** (about 6 weeks at the current pace) — the status line on the *Book K* tab is the verdict.\n"
            f"- Every variant here is scored on **new weeks only** from {lab.FROZEN_FROM}; the week picker in the sidebar shows one week at a time.\n"
            "- Growth is capped by the book: see *Capacity*. Sizing up beyond what the book holds does not scale.\n"
            "- The two baselines (GROK_ONLY, ALL_NO) are the yardsticks: a variant that does not beat them is not adding anything."
        )
    copy_box(slot_lab, md, "md_lab", f"gap-strategy-lab-{SEL}.md")

with tab_curve:
    slot_curve = st.container()
    md = MD("Cancel-window curve (old fixture)")
    show_p(md, "Same decision batch, same orders, cancel window extended "
               "1 → 120 minutes. Average price paid is comparable across rows. "
               "This tab is historical fixture data, unrelated to live quotes.")
    board_fx = fixture_board()
    left, right = st.columns(2)
    with left:
        st.write("**$1 hold**")
        st.dataframe(pd.DataFrame(board_fx["curve_1"]), hide_index=True)
    with right:
        st.write("**$100 hold**")
        st.dataframe(pd.DataFrame(board_fx["curve_100"]), hide_index=True)
    md.h("$1 hold")
    md.df(pd.DataFrame(board_fx["curve_1"]))
    md.h("$100 hold")
    md.df(pd.DataFrame(board_fx["curve_100"]))
    show_cap(md, "$100 fill % still climbing at 60m on the informal table is why 90 and "
                 "120 are on this curve. If it has not flattened, do not freeze 60 yet.")
    copy_box(slot_curve, md, "md_curve", "gap-cancel-window-curve.md")

st.caption(f"boot {services['started_at']} · Telegram commands only")
