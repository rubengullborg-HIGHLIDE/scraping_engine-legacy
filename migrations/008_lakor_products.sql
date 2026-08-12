create table if not exists public.lakor_products (
  id bigserial primary key,
  source_parent_id text not null,
  source_color_id text not null,
  source_url text not null,
  canonical_url text,
  source_product_number text,
  name text,
  brand text,
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
  care_instructions text,
  country_of_origin text,
  category text,
  images text[] not null default '{}'::text[],
  model_info jsonb not null default '{}'::jsonb,
  size_guide jsonb not null default '{}'::jsonb,
  webshop_sizes jsonb not null default '[]'::jsonb,
  local_inventory jsonb not null default '{"stores": {}}'::jsonb,
  local_available boolean not null default false,
  aarhus_available boolean,
  raw jsonb not null default '{}'::jsonb,
  first_seen_at timestamptz not null default now(),
  scraped_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (source_parent_id, source_color_id)
);

create index if not exists lakor_products_source_parent_idx on public.lakor_products (source_parent_id);
create index if not exists lakor_products_source_color_idx on public.lakor_products (source_color_id);
create index if not exists lakor_products_brand_idx on public.lakor_products (brand);
create index if not exists lakor_products_category_idx on public.lakor_products (category);
create index if not exists lakor_products_aarhus_available_idx on public.lakor_products (aarhus_available);
create index if not exists lakor_products_local_inventory_gin_idx on public.lakor_products using gin (local_inventory);

alter table public.lakor_products enable row level security;
