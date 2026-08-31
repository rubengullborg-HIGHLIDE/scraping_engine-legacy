do $$
declare
  target_table text;
begin
  foreach target_table in array array[
    'kaufmann_products',
    'rains_products',
    'romerhus_products',
    'suitclub_products',
    'cejf_products',
    'skagen_clothing_products',
    'shoechapter_products',
    'stoy_products',
    'lakor_products'
  ] loop
    execute format(
      'create index if not exists %I on public.%I (first_seen_at desc) where publication_status = ''active''',
      target_table || '_recent_products_idx',
      target_table
    );
  end loop;
end
$$;

create table if not exists public.catalog_sync_runs (
  id bigint generated always as identity primary key,
  batch_id uuid not null,
  store text not null,
  run_type text not null default 'weekly_catalog_sync',
  status text not null default 'running',
  started_at timestamptz not null default now(),
  completed_at timestamptz,
  rows_before integer not null default 0,
  rows_after integer not null default 0,
  products_seen integer not null default 0,
  new_products integer not null default 0,
  reactivated_products integer not null default 0,
  absent_products integer not null default 0,
  confirmed_missing_products integer not null default 0,
  failed_products integer not null default 0,
  missing_check_skipped boolean not null default false,
  error_message text,
  details jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint catalog_sync_runs_store_check check (
    store in (
      'kaufmann', 'rains', 'romerhus', 'suitclub', 'cejf',
      'skagen_clothing', 'shoechapter', 'stoy', 'lakor'
    )
  ),
  constraint catalog_sync_runs_type_check check (
    run_type in ('weekly_catalog_sync', 'kaufmann_catalog_discovery')
  ),
  constraint catalog_sync_runs_status_check check (
    status in ('running', 'success', 'partial', 'failed', 'safety_blocked')
  ),
  constraint catalog_sync_runs_completed_after_start_check check (
    completed_at is null or completed_at >= started_at
  ),
  constraint catalog_sync_runs_nonnegative_counts_check check (
    rows_before >= 0
    and rows_after >= 0
    and products_seen >= 0
    and new_products >= 0
    and reactivated_products >= 0
    and absent_products >= 0
    and confirmed_missing_products >= 0
    and failed_products >= 0
  )
);

create index if not exists catalog_sync_runs_started_at_idx
  on public.catalog_sync_runs (started_at desc);

create index if not exists catalog_sync_runs_store_started_at_idx
  on public.catalog_sync_runs (store, started_at desc);

create index if not exists catalog_sync_runs_batch_id_idx
  on public.catalog_sync_runs (batch_id);

alter table public.catalog_sync_runs enable row level security;

revoke all on table public.catalog_sync_runs from anon, authenticated;
grant select, insert, update on table public.catalog_sync_runs to service_role;
grant usage, select on sequence public.catalog_sync_runs_id_seq to service_role;

do $$
begin
  if not exists (
    select 1
    from pg_policies
    where schemaname = 'public'
      and tablename = 'catalog_sync_runs'
      and policyname = 'Service role manages catalogue sync runs'
  ) then
    execute 'create policy "Service role manages catalogue sync runs" '
      'on public.catalog_sync_runs for all to service_role '
      'using (true) with check (true)';
  end if;
end
$$;

comment on table public.catalog_sync_runs is
  'One operational summary per store catalogue job, grouped into invocations by batch_id. Intended for scraper health and weekly catalogue insight dashboards.';

comment on column public.catalog_sync_runs.new_products is
  'Number of newly inserted store product rows. For Kaufmann, one row represents one colour variant.';

comment on column public.catalog_sync_runs.absent_products is
  'Rows or source pages absent from the current successful catalogue discovery compared with the previously known set.';
