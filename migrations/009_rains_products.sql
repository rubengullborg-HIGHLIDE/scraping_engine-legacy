create table if not exists public.rains_products (
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
  care_instructions jsonb not null default '[]'::jsonb,
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
  constraint rains_products_source_color_unique unique (source_parent_id, source_color_id),
  constraint rains_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint rains_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint rains_products_highlights_array check (jsonb_typeof(highlights) = 'array'),
  constraint rains_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint rains_products_care_instructions_array check (jsonb_typeof(care_instructions) = 'array'),
  constraint rains_products_model_info_object check (jsonb_typeof(model_info) = 'object'),
  constraint rains_products_size_guide_object check (jsonb_typeof(size_guide) = 'object'),
  constraint rains_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint rains_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint rains_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.rains_products is
  'Rains full-catalog colour rows with online availability and exact local warehouse inventory.';

comment on column public.rains_products.source_color_id is
  'Stable normalized Rains style-colour SKU prefix, for example 18010-03.';

comment on column public.rains_products.webshop_sizes is
  'Online Shopify size variants; online availability must not be treated as local store stock.';

comment on column public.rains_products.local_inventory is
  'Clean inventory interface: {"stores": {store_slug: {"name": text, "stock_known": true, "available": bool, "total_stock": int, "sizes": {size: {"available": bool, "stock": int}}}}}. Expected store keys are rains-aarhus, rains-copenhagen, and rains-frederiksberg; source warehouse ids belong in raw.';

create index if not exists rains_products_source_parent_idx
  on public.rains_products (source_parent_id);

create index if not exists rains_products_source_color_idx
  on public.rains_products (source_color_id);

create index if not exists rains_products_canonical_url_idx
  on public.rains_products (canonical_url);

create index if not exists rains_products_category_idx
  on public.rains_products (category);

create index if not exists rains_products_updated_at_idx
  on public.rains_products (updated_at);

create index if not exists rains_products_local_available_idx
  on public.rains_products (id)
  where local_available;

create index if not exists rains_products_aarhus_available_idx
  on public.rains_products (id)
  where aarhus_available;

create index if not exists rains_products_local_inventory_gin_idx
  on public.rains_products using gin (local_inventory);

alter table public.rains_products enable row level security;
