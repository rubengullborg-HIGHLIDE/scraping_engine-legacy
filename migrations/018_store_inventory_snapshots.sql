create table if not exists public.store_inventory_snapshots (
  id bigserial primary key,
  store text not null,
  product_id bigint not null,
  source_parent_id text,
  source_item_id text not null,
  canonical_url text,
  source_url text,
  checked_at timestamptz not null,
  checked_bucket date not null,
  refresh_status text not null default 'ok',
  current_price numeric,
  list_price numeric,
  webshop_sizes jsonb not null default '[]'::jsonb,
  local_inventory jsonb not null default '{"stores": {}}'::jsonb,
  local_total_stock integer,
  local_available boolean,
  aarhus_total_stock integer,
  aarhus_available boolean,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (store, product_id, checked_bucket)
);

create index if not exists store_inventory_snapshots_product_checked_at_idx
  on public.store_inventory_snapshots (store, product_id, checked_at desc);

create index if not exists store_inventory_snapshots_checked_bucket_idx
  on public.store_inventory_snapshots (checked_bucket desc);

create index if not exists store_inventory_snapshots_store_bucket_idx
  on public.store_inventory_snapshots (store, checked_bucket desc);

alter table public.store_inventory_snapshots enable row level security;

comment on table public.store_inventory_snapshots is
  'One lean dynamic inventory observation per non-Kaufmann product row and UTC day. Product IDs are scoped by store because the source rows live in separate product tables.';

comment on column public.store_inventory_snapshots.checked_bucket is
  'UTC calendar day used to make refresh retries idempotent; a later successful run on the same day replaces the earlier observation.';

comment on column public.store_inventory_snapshots.source_item_id is
  'Stable store item identifier: source_color_id where present, otherwise source_product_id.';
