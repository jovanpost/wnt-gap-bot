-- WNT Gap Trader schema (v1.2)
-- Run ONCE in the SAME Supabase project as wnt-nofade.
-- Tables are prefixed gap_ so they never collide with days/orders/fills.

create table if not exists gap_state (
  key         text primary key,
  value       jsonb not null default '{}'::jsonb,
  updated_at  timestamptz not null default now()
);

create table if not exists gap_runs (
  id              bigserial primary key,
  event_date      date not null,
  event_ticker    text not null,
  status          text not null default 'prompt_ready',
  prompt_version  text not null default 'gap-v1.0',
  harness         text not null default 'grok-web-expert',
  word_list       jsonb not null default '[]'::jsonb,
  prompt_text     text not null default '',
  raw_response    text,
  parsed          jsonb,
  parse_error     text,
  grok_share_url  text,
  telegram_msg_id bigint,
  markets_n       int not null default 0,
  submitted_at    timestamptz,
  parsed_at       timestamptz,
  market_open_at  timestamptz,
  decision_at     timestamptz,
  created_at      timestamptz not null default now(),
  unique (event_date, event_ticker)
);

create index if not exists gap_runs_date_idx on gap_runs (event_date desc);

create table if not exists gap_markets (
  id              bigserial primary key,
  run_id          bigint not null references gap_runs(id) on delete cascade,
  event_date      date not null,
  event_ticker    text not null,
  market_ticker   text not null,
  word            text not null,
  title           text,
  unique (run_id, market_ticker)
);

create table if not exists gap_forecasts (
  id                  bigserial primary key,
  run_id              bigint not null references gap_runs(id) on delete cascade,
  event_date          date not null,
  event_ticker        text not null,
  market_ticker       text not null,
  word                text not null,
  model               text not null default 'grok-web-expert',
  prompt_version      text not null default 'gap-v1.0',
  harness             text not null default 'grok-web-expert',
  probability         int not null check (probability between 1 and 99),
  p_block_airs        numeric,
  p_said_given_airs   numeric,
  carrying_story      text,
  substitute_risk     text,
  other_routes        text,
  reasoning           text,
  created_at          timestamptz not null default now(),
  unique (run_id, market_ticker)
);

create table if not exists gap_quotes (
  id              bigserial primary key,
  forecast_id     bigint references gap_forecasts(id) on delete set null,
  run_id          bigint not null references gap_runs(id) on delete cascade,
  market_ticker   text not null,
  quoted_at       timestamptz not null default now(),
  yes_bid_cents   int,
  yes_ask_cents   int,
  market_prob     numeric,
  source          text not null default 'kalshi_market'
);

create table if not exists gap_orders (
  id              bigserial primary key,
  forecast_id     bigint references gap_forecasts(id) on delete set null,
  run_id          bigint not null references gap_runs(id) on delete cascade,
  event_date      date not null,
  market_ticker   text not null,
  word            text not null,
  placed_at       timestamptz not null default now(),
  side            text not null check (side in ('YES', 'NO')),
  limit_price_cents int not null,
  contracts       numeric not null,
  cost_cents      int not null,
  gap_points      numeric not null,
  threshold       int not null,
  cluster_key     text,
  paper           boolean not null default true,
  client_order_id text,
  status          text not null default 'paper_booked',
  filled_contracts numeric not null default 0,
  fees_cents      int not null default 0,
  result          text,
  realized_pnl_cents int,
  execution_model text default 'capped_sweep',
  variant_id      text,
  exit_rule       text,
  notional_dollars numeric
);

create unique index if not exists gap_orders_one_per_book
  on gap_orders (event_date, market_ticker, variant_id)
  where status not in ('rejected', 'cancelled');

create table if not exists gap_settlements (
  id              bigserial primary key,
  forecast_id     bigint references gap_forecasts(id) on delete set null,
  order_id        bigint references gap_orders(id) on delete set null,
  settled_at      timestamptz,
  outcome         text,
  gross_cents     int,
  fees_cents      int,
  net_cents       int,
  fill_model      text,
  unique (order_id)
);

create table if not exists gap_activity (
  id          bigserial primary key,
  at          timestamptz not null default now(),
  kind        text not null,
  message     text not null
);
