-- v1.10.0 (Phase A of the prompt lab): every night's input is frozen so any model + prompt can be
-- replayed on exactly what Grok saw. Insert-once: the database refuses to edit a stored package.
create table if not exists gap_prompt_versions (
  prompt_version  text primary key,
  system_prompt   text not null,
  first_seen      timestamptz not null default now()
);
alter table gap_prompt_versions enable row level security;

create table if not exists gap_news_packages (
  id              bigserial primary key,
  event_date      date not null,
  event_ticker    text,
  kind            text not null default 'grok_file',
  prompt_version  text,
  words           text not null,
  user_message    text not null,
  sha             text not null,
  created_at      timestamptz not null default now(),
  unique (event_date, kind)
);
create index if not exists gap_news_packages_date on gap_news_packages (event_date);
alter table gap_news_packages enable row level security;
drop rule if exists gap_news_packages_no_update on gap_news_packages;
drop rule if exists gap_prompt_versions_no_update on gap_prompt_versions;
create or replace function gap_frozen_no_update() returns trigger language plpgsql as $$ begin raise exception 'frozen row in % cannot be edited', TG_TABLE_NAME; end $$;
drop trigger if exists gap_news_packages_frozen on gap_news_packages;
create trigger gap_news_packages_frozen before update on gap_news_packages for each row execute function gap_frozen_no_update();
drop trigger if exists gap_prompt_versions_frozen on gap_prompt_versions;
create trigger gap_prompt_versions_frozen before update on gap_prompt_versions for each row execute function gap_frozen_no_update();

alter table gap_shadow_forecasts add column if not exists prompt_version text;
alter table gap_shadow_forecasts add column if not exists package_id bigint;
