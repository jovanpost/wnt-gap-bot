# WNT Gap Bot (addendum v1.1)

Paper-first gap trader for Kalshi `KXWORLDNEWSMENTION`.

You are the courier. Streamlit catches tonight’s event, Telegram sends a
`.txt` (frozen prompt + words), you paste into a **new Expert** Grok chat,
you paste JSON back. Quotes happen at **open + 60 minutes**. Entry is a
**capped taker sweep**: `limit = model − 15¢`, remainder rests at that
same limit, cancel **60 minutes after send**. Partial fills are normal.

The Streamlit board shows **all four backtest variants at once**.

| | Hold to settlement | Scalp at model |
|---|---|---|
| **$1** | A | C |
| **$100** | B | D |

They do not share a liquidity pool. Decision points are sampled once.

## Push

```bash
gh repo create jovanpost/wnt-gap-bot --public --source=. --remote=origin --push
```

## Supabase (same project as no-fade)

SQL editor:

1. New install: `sql/001_gap_schema.sql`
2. Already applied v1.0: also run `sql/002_addendum_v11.sql`

```sql
select tablename from pg_tables
where schemaname = 'public' and tablename like 'gap_%'
order by 1;
```

Reuse no-fade `DATABASE_URL`. Never touch `orders` / `days` / `fills`.

## Telegram + Streamlit

New bot token. Secrets from `.streamlit/secrets.toml.example`.
`PAPER=true`, `LIVE_TRADING=false`. App file: `streamlit_app.py`.

Commands: `/gap_prep` `/gap_resend` `/gap_status` `/gap_pnl`
plus a JSON reply.

Decision clock fallback: `MARKET_OPEN_CT=10:00` so decision is 11:00 CT
until Kalshi `open_time` is wired through.

## Four-way board

Tab **Four-way backtest** runs a deterministic FIXTURE tape so the four
cards have numbers the moment the app boots. Labeled fixture — replace
with real minute bars in `data/` before trusting the pick.

Tab **Cancel-window curve** walks the **same** batch through 1 / 5 / 15 /
30 / 60 / 90 / 120 minutes so price-paid is comparable (the informal
prototype table was not).

```bash
python scripts/run_backtest.py
```

## Frozen knobs still frozen

Gap 15, both sides, no promo, cluster cap 2, night cap 20%, 20-broadcast
window, Grok web-expert courier until the API transfer test. This version
only replaces “take the current price.”
