create table if not exists public.skagen_clothing_products (
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
  category text,
  category_path text[] not null default '{}'::text[],
  tags text[] not null default '{}'::text[],
  images text[] not null default '{}'::text[],
  model_info jsonb not null default '{}'::jsonb,
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
  constraint skagen_clothing_products_source_color_unique unique (source_parent_id, source_color_id),
  constraint skagen_clothing_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint skagen_clothing_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint skagen_clothing_products_highlights_array check (jsonb_typeof(highlights) = 'array'),
  constraint skagen_clothing_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint skagen_clothing_products_model_info_object check (jsonb_typeof(model_info) = 'object'),
  constraint skagen_clothing_products_size_guide_object check (jsonb_typeof(size_guide) = 'object'),
  constraint skagen_clothing_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint skagen_clothing_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint skagen_clothing_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.skagen_clothing_products is
  'Skagen Clothing full-catalog colour rows with webshop availability and exact Aarhus/Copenhagen stock.';

comment on column public.skagen_clothing_products.source_color_id is
  'Skagen Clothing Shopify product id. Each Shopify product represents one colour and its variants represent sizes.';

comment on column public.skagen_clothing_products.local_inventory is
  'Clean exact inventory for skagen-aarhus and skagen-copenhagen. Internal warehouse and fictive locations are retained only under raw.';

comment on column public.skagen_clothing_products.local_total_stock is
  'Sum of all known exact size stock at the tracked Aarhus and Copenhagen shops.';

create index if not exists skagen_clothing_products_source_parent_idx
  on public.skagen_clothing_products (source_parent_id);
create index if not exists skagen_clothing_products_source_color_idx
  on public.skagen_clothing_products (source_color_id);
create index if not exists skagen_clothing_products_category_idx
  on public.skagen_clothing_products (category);
create index if not exists skagen_clothing_products_updated_at_idx
  on public.skagen_clothing_products (updated_at);
create index if not exists skagen_clothing_products_local_available_idx
  on public.skagen_clothing_products (id) where local_available;
create index if not exists skagen_clothing_products_aarhus_available_idx
  on public.skagen_clothing_products (id) where aarhus_available;
create index if not exists skagen_clothing_products_local_inventory_gin_idx
  on public.skagen_clothing_products using gin (local_inventory);

alter table public.skagen_clothing_products enable row level security;

grant select, insert, update, delete on table public.skagen_clothing_products to service_role;
grant usage, select on sequence public.skagen_clothing_products_id_seq to service_role;
