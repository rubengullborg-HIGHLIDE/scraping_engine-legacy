create table if not exists public.stoy_products (
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
  color_group text,
  current_price numeric,
  list_price numeric,
  currency text not null default 'DKK',
  description text,
  specifications jsonb not null default '{}'::jsonb,
  materials text[] not null default '{}'::text[],
  fit text,
  country_of_origin text,
  category text,
  category_path text[] not null default '{}'::text[],
  collections text[] not null default '{}'::text[],
  tags text[] not null default '{}'::text[],
  images text[] not null default '{}'::text[],
  model_info jsonb not null default '{}'::jsonb,
  size_guide jsonb not null default '{}'::jsonb,
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
  constraint stoy_products_source_color_unique unique (source_parent_id, source_color_id),
  constraint stoy_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint stoy_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint stoy_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint stoy_products_model_info_object check (jsonb_typeof(model_info) = 'object'),
  constraint stoy_products_size_guide_object check (jsonb_typeof(size_guide) = 'object'),
  constraint stoy_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint stoy_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint stoy_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.stoy_products is
  'STOY full-catalog colour rows with webshop availability and public Aarhus/Copenhagen store availability.';
comment on column public.stoy_products.source_color_id is
  'STOY Shopify product id. Each Shopify product represents one colour and its variants represent sizes.';
comment on column public.stoy_products.local_inventory is
  'Clean inventory interface: {"stores": {store_slug: {"name": text, "stock_known": false, "available": bool, "total_stock": null, "sizes": {size: {"available": bool, "stock": null}}}}}. STOY only publishes availability, never quantities.';

create index if not exists stoy_products_source_parent_idx on public.stoy_products (source_parent_id);
create index if not exists stoy_products_source_color_idx on public.stoy_products (source_color_id);
create index if not exists stoy_products_brand_idx on public.stoy_products (brand);
create index if not exists stoy_products_category_idx on public.stoy_products (category);
create index if not exists stoy_products_updated_at_idx on public.stoy_products (updated_at);
create index if not exists stoy_products_local_available_idx on public.stoy_products (id) where local_available;
create index if not exists stoy_products_aarhus_available_idx on public.stoy_products (id) where aarhus_available;
create index if not exists stoy_products_local_inventory_gin_idx on public.stoy_products using gin (local_inventory);

alter table public.stoy_products enable row level security;
