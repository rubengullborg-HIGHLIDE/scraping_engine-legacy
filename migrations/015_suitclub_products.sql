create table if not exists public.suitclub_products (
  id bigint generated always as identity primary key,
  source_product_id text not null unique,
  source_url text not null,
  canonical_url text,
  name text,
  brand text,
  product_type text not null,
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
  collections jsonb not null default '[]'::jsonb,
  images text[] not null default '{}'::text[],
  model_info jsonb not null default '{}'::jsonb,
  matching_products jsonb not null default '{}'::jsonb,
  related_colors jsonb not null default '[]'::jsonb,
  webshop_sizes jsonb not null default '[]'::jsonb,
  local_inventory jsonb not null default '{"stores": {}}'::jsonb,
  local_total_stock integer,
  local_available boolean,
  aarhus_total_stock integer,
  aarhus_available boolean,
  inventory_checked_at timestamptz,
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  scraped_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint suitclub_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint suitclub_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint suitclub_products_highlights_array check (jsonb_typeof(highlights) = 'array'),
  constraint suitclub_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint suitclub_products_collections_array check (jsonb_typeof(collections) = 'array'),
  constraint suitclub_products_model_info_object check (jsonb_typeof(model_info) = 'object'),
  constraint suitclub_products_matching_products_object check (jsonb_typeof(matching_products) = 'object'),
  constraint suitclub_products_related_colors_array check (jsonb_typeof(related_colors) = 'array'),
  constraint suitclub_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint suitclub_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint suitclub_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.suitclub_products is
  'SuitClub mens clothing and footwear with exact webshop and physical-store inventory; accessories and suit bundles are excluded.';

comment on column public.suitclub_products.source_product_id is
  'Stable Shopify product id. Each row is one independently purchasable product and colour.';

comment on column public.suitclub_products.webshop_sizes is
  'Exact per-variant stock from SuitClub internal Lager Aarhus, which fulfils online orders.';

comment on column public.suitclub_products.local_inventory is
  'Exact physical-shop inventory for SuitClub Aarhus, Copenhagen, Odense, and Aalborg. The internal online warehouse is excluded.';

comment on column public.suitclub_products.local_total_stock is
  'Sum of exact variant quantities across the four physical shops; null when the Storefront response is incomplete.';

comment on column public.suitclub_products.aarhus_total_stock is
  'Sum of exact variant quantities at the physical SuitClub Aarhus shop; null when its inventory is incomplete.';

create index if not exists suitclub_products_product_type_idx
  on public.suitclub_products (product_type);

create index if not exists suitclub_products_category_idx
  on public.suitclub_products (category);

create index if not exists suitclub_products_updated_at_idx
  on public.suitclub_products (updated_at);

create index if not exists suitclub_products_local_available_idx
  on public.suitclub_products (id)
  where local_available is true;

create index if not exists suitclub_products_aarhus_available_idx
  on public.suitclub_products (id)
  where aarhus_available is true;

create index if not exists suitclub_products_local_inventory_gin_idx
  on public.suitclub_products using gin (local_inventory);

alter table public.suitclub_products enable row level security;

revoke all on table public.suitclub_products from anon, authenticated;
revoke all on sequence public.suitclub_products_id_seq from anon, authenticated;
grant select, insert, update, delete on table public.suitclub_products to service_role;
grant usage, select on sequence public.suitclub_products_id_seq to service_role;
