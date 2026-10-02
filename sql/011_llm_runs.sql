-- v1.11.0: one row per model run with its usage and cost (paper challengers). Lets the weekly
-- report and /gap_cost say what each model costs per night. Not money-critical.
create table if not exists gap_llm_runs (
  id               bigserial primary key,
  event_date       date not null,
  model            text not null,
  prompt_version   text,
  package_id       bigint,
  ok               boolean not null default true,
  seconds          numeric,
  attempts         int,
  input_tokens     bigint,
  output_tokens    bigint,
  reasoning_tokens bigint,
  cached_tokens    bigint,
  tool_calls       int,
  cost_usd         numeric(12,6),
  detail           text,
  created_at       timestamptz not null default now()
);
create index if not exists gap_llm_runs_date on gap_llm_runs (event_date);
alter table gap_llm_runs enable row level security;
