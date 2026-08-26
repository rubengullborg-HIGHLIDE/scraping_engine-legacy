create table if not exists public.cejf_products (
  id bigint generated always as identity primary key,
  source_product_id text not null unique,
  source_url text not null,
  canonical_url text,
  name text not null,
  brand text not null,
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
  tags text[] not null default '{}'::text[],
  images text[] not null default '{}'::text[],
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
  constraint cejf_products_local_total_stock_nonnegative check (local_total_stock >= 0),
  constraint cejf_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
  constraint cejf_products_specifications_object check (jsonb_typeof(specifications) = 'object'),
  constraint cejf_products_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
  constraint cejf_products_local_inventory_shape check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  constraint cejf_products_raw_object check (jsonb_typeof(raw) = 'object')
);

comment on table public.cejf_products is
  'CEJF mens clothing catalogue with webshop size availability mapped to its single Aarhus shop as a boolean proxy; quantities remain unknown.';

comment on column public.cejf_products.source_product_id is
  'Stable Shopify product id. Each CEJF Shopify product represents one independently purchasable product and colour.';

comment on column public.cejf_products.webshop_sizes is
  'Shopify online availability per size. CEJF does not expose exact webshop quantities.';

comment on column public.cejf_products.local_inventory is
  'Clean CEJF Aarhus inventory interface. Shopify webshop availability is used as the boolean proxy because CEJF has one shop; stock quantities remain null.';

comment on column public.cejf_products.local_total_stock is
  'Always null while CEJF does not expose physical-store quantities; boolean availability does not imply a count.';

comment on column public.cejf_products.aarhus_total_stock is
  'Null until CEJF exposes reliable physical stock for the Graven 3B shop.';

create index if not exists cejf_products_brand_idx
  on public.cejf_products (brand);

create index if not exists cejf_products_category_idx
  on public.cejf_products (category);

create index if not exists cejf_products_updated_at_idx
  on public.cejf_products (updated_at);

create index if not exists cejf_products_local_available_idx
  on public.cejf_products (id)
  where local_available is true;

create index if not exists cejf_products_aarhus_available_idx
  on public.cejf_products (id)
  where aarhus_available is true;

create index if not exists cejf_products_local_inventory_gin_idx
  on public.cejf_products using gin (local_inventory);

alter table public.cejf_products enable row level security;

revoke all on table public.cejf_products from anon, authenticated;
revoke all on sequence public.cejf_products_id_seq from anon, authenticated;
grant select, insert, update, delete on table public.cejf_products to service_role;
grant usage, select on sequence public.cejf_products_id_seq to service_role;
