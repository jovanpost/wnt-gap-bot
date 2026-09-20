-- 005_v151.sql  (wnt-gap v1.5.1)
-- Safe to run more than once. gap/store.py init_db() also applies this file at boot,
-- so you normally do NOT need to run it by hand.
--
-- 1) Decision-time quote freeze: one frozen quote per (run, market), never overwritten.
alter table gap_quotes add column if not exists captured_at timestamptz;
alter table gap_quotes add column if not exists decision_at timestamptz;
alter table gap_quotes add column if not exists valid boolean;
alter table gap_quotes add column if not exists invalid_reason text;
alter table gap_quotes add column if not exists age_s integer;
alter table gap_quotes add column if not exists frozen boolean not null default false;
create unique index if not exists gap_quotes_frozen_once on gap_quotes (run_id, market_ticker) where frozen is true;

-- 2) Every order remembers exactly which quote and rule created it (for the RULE AUDIT).
alter table gap_orders add column if not exists quote_bid_cents integer;
alter table gap_orders add column if not exists quote_ask_cents integer;
alter table gap_orders add column if not exists quote_captured_at timestamptz;
alter table gap_orders add column if not exists book_rule text;

-- 3) Official Kalshi results never change once published, so cache them.
create table if not exists gap_results (
  market_ticker text primary key,
  result        text not null,
  fetched_at    timestamptz not null default now()
);
