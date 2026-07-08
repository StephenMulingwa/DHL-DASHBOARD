-- Operation sessions + logs for in-app pipeline visibility.
create table if not exists operation_sessions (
  id text primary key,
  trigger text not null,
  username text,
  started_at timestamptz not null default now(),
  ended_at timestamptz,
  status text not null default 'running'
);

create table if not exists operation_logs (
  id bigserial primary key,
  session_id text,
  ts timestamptz not null default now(),
  category text not null,
  step text not null,
  status text not null,
  message text not null,
  detail jsonb
);

alter table operation_logs add column if not exists session_id text;

create index if not exists operation_logs_session_idx on operation_logs (session_id, id);
create index if not exists operation_logs_ts_idx on operation_logs (ts desc);

create table if not exists dashboard_meta (
  key text primary key,
  value jsonb not null,
  updated_at timestamptz not null default now()
);
