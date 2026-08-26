alter table public.rains_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.romerhus_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.suitclub_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.cejf_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.skagen_clothing_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.shoechapter_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.stoy_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

alter table public.lakor_products
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_catalog_misses integer not null default 0;

do $$
declare
  target_table text;
  status_constraint text;
  misses_constraint text;
begin
  foreach target_table in array array[
    'rains_products',
    'romerhus_products',
    'suitclub_products',
    'cejf_products',
    'skagen_clothing_products',
    'shoechapter_products',
    'stoy_products',
    'lakor_products'
  ] loop
    status_constraint := target_table || '_publication_status_check';
    misses_constraint := target_table || '_catalog_misses_nonnegative';

    if not exists (
      select 1
      from pg_constraint
      where conname = status_constraint
        and conrelid = format('public.%I', target_table)::regclass
    ) then
      execute format(
        'alter table public.%I add constraint %I check (publication_status in (''active'', ''unavailable'', ''missing''))',
        target_table,
        status_constraint
      );
    end if;

    if not exists (
      select 1
      from pg_constraint
      where conname = misses_constraint
        and conrelid = format('public.%I', target_table)::regclass
    ) then
      execute format(
        'alter table public.%I add constraint %I check (consecutive_catalog_misses >= 0)',
        target_table,
        misses_constraint
      );
    end if;

    execute format(
      'create index if not exists %I on public.%I (publication_status)',
      target_table || '_publication_status_idx',
      target_table
    );
  end loop;
end
$$;

comment on column public.rains_products.publication_status is
  'Catalog lifecycle state. Active means listed; unavailable is a confirmed dead page; missing means absent from two successful weekly catalog imports.';
