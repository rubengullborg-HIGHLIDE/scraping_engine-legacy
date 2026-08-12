create table if not exists public.shoechapter_products (
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
  highlights jsonb not null default '[]'::jsonb,
  specifications jsonb not null default '{}'::jsonb,
  materials text[] not null default '{}'::text[],
  fit text,
  country_of_origin text,
  category text,
  category_path text[] not null default '{}'::text[],
  tags text[] not null default '{}'::text[],
  images text[] not null default '{}'::text[],
  size_guide jsonb not null default '{}'::jsonb,
  webshop_sizes jsonb not null default '[]'::jsonb,
  local_inventory jsonb not null default '{"stores": {}}'::jsonb,
  local_total_stock integer not null default 0,
  local_available boolean not null default false,
  aarhus_total_stock integer,
  aarhus_available boolean not null default false,
  inventory_checked_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  scraped_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shoechapter_products_source_color_unique unique (source_parent_id, source_color_id),
  constraint shoechapter_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint shoechapter_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint shoechapter_products_highlights_array check (jsonb_typeof(highlights) = 'array'),
  constraint shoechapter_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint shoechapter_products_size_guide_object check (jsonb_typeof(size_guide) = 'object'),
  constraint shoechapter_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint shoechapter_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint shoechapter_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.shoechapter_products is
  'Shoe Chapter full-catalog footwear colour rows with webshop availability and rendered Aarhus store stock.';

comment on column public.shoechapter_products.source_color_id is
  'Shoe Chapter Shopify product id. Each Shopify product represents one colour and its variants represent sizes.';

comment on column public.shoechapter_products.local_inventory is
  'Clean inventory interface: {"stores": {store_slug: {"name": text, "stock_known": true, "available": bool, "total_stock": int, "sizes": {size: {"available": bool, "stock": int}}}}}. Expected store key is shoechapter-aarhus.';

comment on column public.shoechapter_products.local_total_stock is
  'Sum of known exact per-size local stock counts. For Shoe Chapter this can be a lower bound when some available sizes are rendered only as På lager.';

comment on column public.shoechapter_products.aarhus_total_stock is
  'Exact Aarhus total stock only when every available Aarhus size has a count; null when the rendered page only gives partial counts.';

create index if not exists shoechapter_products_source_parent_idx
  on public.shoechapter_products (source_parent_id);

create index if not exists shoechapter_products_source_color_idx
  on public.shoechapter_products (source_color_id);

create index if not exists shoechapter_products_brand_idx
  on public.shoechapter_products (brand);

create index if not exists shoechapter_products_category_idx
  on public.shoechapter_products (category);

create index if not exists shoechapter_products_updated_at_idx
  on public.shoechapter_products (updated_at);

create index if not exists shoechapter_products_local_available_idx
  on public.shoechapter_products (id)
  where local_available;

create index if not exists shoechapter_products_aarhus_available_idx
  on public.shoechapter_products (id)
  where aarhus_available;

create index if not exists shoechapter_products_local_inventory_gin_idx
  on public.shoechapter_products using gin (local_inventory);

alter table public.shoechapter_products enable row level security;
