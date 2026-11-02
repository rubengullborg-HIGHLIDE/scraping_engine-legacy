-- Oversized Lifting Club (oversizedstudios.com). Unlike the scraper tables,
-- this one is written by the Highlide Shopify app
-- (github.com/HIGHLIDE-DK/HIGHLIDE-private-shopify-distribution) with the
-- service-role key, from Shopify's Admin API.
--
-- Same shape as the scraper store tables: one row per product colour, sizes in
-- webshop_sizes, exact per-store stock in local_inventory. Only locations OLC
-- marked as a physical store in Aarhus appear in local_inventory, so the
-- local_* and aarhus_* summaries are equal. Each further app merchant gets its
-- own <store>_products table with this shape.

create table if not exists public.olc_products (
  id bigserial primary key,
  source_parent_id text not null,
  source_color_id text not null,
  source_url text not null,
  canonical_url text,
  source_product_number text,
  name text,
  brand text,
  product_type text,
  color text,
  current_price numeric,
  list_price numeric,
  currency text not null default 'DKK',
  description text,
  tags text[] not null default '{}'::text[],
  images text[] not null default '{}'::text[],
  webshop_sizes jsonb not null default '[]'::jsonb,
  local_inventory jsonb not null default '{"stores": {}}'::jsonb,
  local_total_stock integer not null default 0,
  local_available boolean not null default false,
  aarhus_total_stock integer not null default 0,
  aarhus_available boolean not null default false,
  inventory_checked_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  scraped_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint olc_products_source_color_unique unique (source_parent_id, source_color_id),
  constraint olc_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint olc_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint olc_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint olc_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint olc_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.olc_products is
  'Oversized Lifting Club products, prices and exact Aarhus store stock from the Highlide Shopify app.';
comment on column public.olc_products.source_parent_id is
  'Numeric Shopify product id.';
comment on column public.olc_products.source_color_id is
  'Shopify product id, plus ":<colour>" when the product has a colour option.';
comment on column public.olc_products.local_inventory is
  'Exact stock per Aarhus store location from the Shopify Admin API, keyed "olc-<Shopify location id>".';

create index if not exists olc_products_source_parent_idx
  on public.olc_products (source_parent_id);

create index if not exists olc_products_product_type_idx
  on public.olc_products (product_type);

create index if not exists olc_products_updated_at_idx
  on public.olc_products (updated_at);

create index if not exists olc_products_aarhus_available_idx
  on public.olc_products (id)
  where aarhus_available;

create index if not exists olc_products_local_inventory_gin_idx
  on public.olc_products using gin (local_inventory);

alter table public.olc_products enable row level security;

revoke all on table public.olc_products from anon, authenticated;
revoke all on sequence public.olc_products_id_seq from anon, authenticated;
grant select, insert, update, delete on table public.olc_products to service_role;
grant usage, select on sequence public.olc_products_id_seq to service_role;
