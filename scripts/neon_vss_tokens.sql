-- Neon/Postgres table required by the DHL dashboard for VSS token storage.
-- The app keeps one active VSS session row and overwrites it on refresh.

create table if not exists vss_tokens (
  id text primary key default 'active',
  token text not null,
  pid text not null default '',
  issued_at timestamptz not null,
  base_url text,
  profile text,
  updated_at timestamptz not null default now()
);

create table if not exists dashboard_snapshots (
  key text primary key,
  payload jsonb not null,
  row_count integer not null default 0,
  updated_at timestamptz not null default now()
);
