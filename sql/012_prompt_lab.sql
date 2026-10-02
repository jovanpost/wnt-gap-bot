-- v1.12.0 (prompt lab, phases B-D): prompt variants written by a model under strict edit rules,
-- replayed on frozen nights, scored, and ranked. PAPER ONLY: nothing here is read by any trading code.
create table if not exists gap_lab_prompts (
  prompt_id      text primary key,
  parent_id      text,
  system_prompt  text not null,
  name           text,
  section        text,
  action         text,
  unit_before    text,
  unit_after     text,
  why            text,
  written_by     text,
  written_from   date,
  status         text not null default 'candidate',
  note           text,
  champion_from  date,
  created_at     timestamptz not null default now()
);
alter table gap_lab_prompts enable row level security;

create table if not exists gap_lab_runs (
  prompt_id      text not null,
  model_key      text not null,
  event_date     date not null,
  purpose        text not null,
  status         text not null default 'pending',
  model          text,
  attempts       int not null default 0,
  not_before     timestamptz,
  claimed_at     timestamptz,
  finished_at    timestamptz,
  seconds        numeric,
  cost_usd       numeric(12,6),
  live           boolean not null default false,
  error          text,
  created_at     timestamptz not null default now(),
  primary key (prompt_id, model_key, event_date)
);
create index if not exists gap_lab_runs_status on gap_lab_runs (status);
alter table gap_lab_runs enable row level security;

create table if not exists gap_lab_forecasts (
  prompt_id      text not null,
  model_key      text not null,
  event_date     date not null,
  market_ticker  text not null,
  word           text not null,
  probability    int not null,
  reasoning      text,
  created_at     timestamptz not null default now(),
  primary key (prompt_id, model_key, event_date, market_ticker)
);
alter table gap_lab_forecasts enable row level security;

create table if not exists gap_lab_nights (
  event_date     date primary key,
  champion_id    text,
  stage          text not null default 'new',
  writer_model   text,
  decision       text,
  detail         text,
  updated_at     timestamptz not null default now()
);
alter table gap_lab_nights enable row level security;
