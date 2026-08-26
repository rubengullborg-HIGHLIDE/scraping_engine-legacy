alter table public.kaufmann_products
  add column if not exists source_available boolean,
  add column if not exists publication_status text not null default 'active',
  add column if not exists status_reason text,
  add column if not exists status_checked_at timestamptz,
  add column if not exists discontinued_at timestamptz,
  add column if not exists last_inventory_checked_at timestamptz,
  add column if not exists last_seen_in_catalog_at timestamptz,
  add column if not exists consecutive_source_misses integer not null default 0,
  add column if not exists consecutive_catalog_misses integer not null default 0,
  add column if not exists last_refresh_error text,
  add column if not exists last_refresh_error_at timestamptz;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'kaufmann_products_publication_status_check'
      and conrelid = 'public.kaufmann_products'::regclass
  ) then
    alter table public.kaufmann_products
      add constraint kaufmann_products_publication_status_check
      check (publication_status in ('active', 'unavailable', 'missing'));
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'kaufmann_products_source_misses_nonnegative'
      and conrelid = 'public.kaufmann_products'::regclass
  ) then
    alter table public.kaufmann_products
      add constraint kaufmann_products_source_misses_nonnegative
      check (consecutive_source_misses >= 0);
  end if;

  if not exists (
    select 1
    from pg_constraint
    where conname = 'kaufmann_products_catalog_misses_nonnegative'
      and conrelid = 'public.kaufmann_products'::regclass
  ) then
    alter table public.kaufmann_products
      add constraint kaufmann_products_catalog_misses_nonnegative
      check (consecutive_catalog_misses >= 0);
  end if;
end
$$;

create index if not exists kaufmann_products_publication_status_idx
  on public.kaufmann_products (publication_status);

create index if not exists kaufmann_products_active_inventory_checked_idx
  on public.kaufmann_products (last_inventory_checked_at asc nulls first, id)
  where publication_status = 'active';

alter table public.kaufmann_inventory_snapshots
  add column if not exists source_available boolean,
  add column if not exists publication_status text,
  add column if not exists status_reason text;

do $$
begin
  if not exists (
    select 1
    from pg_constraint
    where conname = 'kaufmann_inventory_snapshots_publication_status_check'
      and conrelid = 'public.kaufmann_inventory_snapshots'::regclass
  ) then
    alter table public.kaufmann_inventory_snapshots
      add constraint kaufmann_inventory_snapshots_publication_status_check
      check (
        publication_status is null
        or publication_status in ('active', 'unavailable', 'missing')
      );
  end if;
end
$$;

comment on column public.kaufmann_products.source_available is
  'Kaufmann color-level available flag from /widgets/product/variation/{source_parent_id}; false takes precedence over residual stock.';

comment on column public.kaufmann_products.publication_status is
  'Frontend lifecycle status. Only active rows should be published; unavailable and missing rows are retained as soft tombstones.';

comment on column public.kaufmann_products.last_inventory_checked_at is
  'Last successful dynamic refresh check. Fetch failures do not advance this timestamp.';

comment on column public.kaufmann_products.last_seen_in_catalog_at is
  'Last successful full-catalog discovery that included this product. Inventory refresh does not update it.';
