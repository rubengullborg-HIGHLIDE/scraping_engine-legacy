-- Store registry and approval for the HIGHLIDE Shopify app (public version).
--
-- Any Shopify store can install the app. The app registers each store here as
-- 'pending' and keeps its catalogue in its own database, but publishes nothing
-- until HIGHLIDE approves the store:
--
--   select public.approve_shopify_store('example.myshopify.com', 'example');
--
-- Approval creates public.<store_key>_products (same shape as olc_products)
-- and adds it to public.shopify_store_products, one view over every approved
-- Shopify store for search and the website. The app notices the approval
-- within about ten minutes and publishes the store's products.
--
--   select public.pause_shopify_store('example.myshopify.com');
--
-- stops publishing and hides the store from the view; its table is kept.
--
-- Columns the app writes: shop_name, storefront_url, installed_at,
-- uninstalled_at, product_count, aarhus_location_count, last_full_sync_at,
-- last_error, updated_at. Columns HIGHLIDE controls: status, store_key,
-- approved_at. The app never changes the latter.

begin;

create table if not exists public.shopify_stores (
  shop_domain text primary key,
  status text not null default 'pending'
    check (status in ('pending', 'approved', 'paused')),
  store_key text unique
    check (store_key is null or store_key ~ '^[a-z][a-z0-9_]{1,40}$'),
  shop_name text,
  storefront_url text,
  installed_at timestamptz,
  uninstalled_at timestamptz,
  product_count integer,
  aarhus_location_count integer,
  last_full_sync_at timestamptz,
  last_error text,
  approved_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  constraint shopify_stores_approved_has_key check (status <> 'approved' or store_key is not null)
);

comment on table public.shopify_stores is
  'Shopify stores that installed the HIGHLIDE app, and whether HIGHLIDE has approved them for publishing.';
comment on column public.shopify_stores.aarhus_location_count is
  'Locations the merchant marked as a physical store in Aarhus. 0 means nothing would be shown.';

alter table public.shopify_stores enable row level security;
revoke all on table public.shopify_stores from anon, authenticated;
grant select, insert, update, delete on table public.shopify_stores to service_role;

-- Shape of every <store_key>_products table. Never written to; new store
-- tables copy it, and the combined view starts from it.
create table if not exists public.shopify_store_products_template (
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
  unique (source_parent_id, source_color_id),
  check (local_total_stock >= 0),
  check (aarhus_total_stock >= 0),
  check (jsonb_typeof(webshop_sizes) = 'array'),
  check (
    jsonb_typeof(local_inventory) = 'object'
    and local_inventory ? 'stores'
    and jsonb_typeof(local_inventory -> 'stores') = 'object'
  ),
  check (jsonb_typeof(raw) = 'object')
);

comment on table public.shopify_store_products_template is
  'Template for <store_key>_products tables of the HIGHLIDE Shopify app. Always empty.';

alter table public.shopify_store_products_template enable row level security;
revoke all on table public.shopify_store_products_template from anon, authenticated;
grant select on table public.shopify_store_products_template to service_role;
-- New store tables share this sequence for their ids.
grant usage, select on sequence public.shopify_store_products_template_id_seq to service_role;

-- Creates public.<store_key>_products if it doesn't exist yet.
create or replace function public.create_shopify_store_table(p_store_key text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  t text := p_store_key || '_products';
begin
  if p_store_key !~ '^[a-z][a-z0-9_]{1,40}$' then
    raise exception 'Invalid store key "%": use lowercase letters, digits and _', p_store_key;
  end if;
  if to_regclass('public.' || quote_ident(t)) is not null then
    return;
  end if;

  execute format(
    'create table public.%I (like public.shopify_store_products_template including all)', t);
  execute format('create index %I on public.%I (source_parent_id)', t || '_source_parent_idx', t);
  execute format('create index %I on public.%I (product_type)', t || '_product_type_idx', t);
  execute format('create index %I on public.%I (updated_at)', t || '_updated_at_idx', t);
  execute format('create index %I on public.%I (id) where aarhus_available', t || '_aarhus_available_idx', t);
  execute format('create index %I on public.%I using gin (local_inventory)', t || '_local_inventory_gin_idx', t);
  execute format('comment on table public.%I is %L', t,
    'Products, prices and exact Aarhus store stock for store "' || p_store_key || '" from the HIGHLIDE Shopify app.');
  execute format('alter table public.%I enable row level security', t);
  execute format('revoke all on table public.%I from anon, authenticated', t);
  execute format('grant select, insert, update, delete on table public.%I to service_role', t);
end
$$;

-- Rebuilds public.shopify_store_products: every approved store's rows, with
-- a "store" column naming the store key.
create or replace function public.refresh_shopify_store_products_view()
returns void
language plpgsql
security definer
set search_path = ''
as $$
declare
  cols text;
  sql text;
  rec record;
begin
  select string_agg(quote_ident(column_name), ', ' order by ordinal_position)
  into cols
  from information_schema.columns
  where table_schema = 'public' and table_name = 'shopify_store_products_template';

  sql := format('select null::text as store, %s from public.shopify_store_products_template', cols);
  for rec in
    select s.store_key
    from public.shopify_stores s
    where s.status = 'approved'
      and to_regclass('public.' || quote_ident(s.store_key || '_products')) is not null
    order by s.store_key
  loop
    sql := sql || format(' union all select %L::text, %s from public.%I',
      rec.store_key, cols, rec.store_key || '_products');
  end loop;

  execute 'drop view if exists public.shopify_store_products';
  execute 'create view public.shopify_store_products with (security_invoker = true) as ' || sql;
  execute 'comment on view public.shopify_store_products is '
    || quote_literal('All approved HIGHLIDE Shopify app stores. Rebuilt by refresh_shopify_store_products_view().');
  execute 'revoke all on public.shopify_store_products from anon, authenticated';
  execute 'grant select on public.shopify_store_products to service_role';
end
$$;

-- Approves a store for publishing: creates its table and adds it to the view.
-- Works before the store has installed the app, too (the app then publishes
-- right after its first sync).
create or replace function public.approve_shopify_store(p_shop_domain text, p_store_key text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  if exists (
    select 1 from public.shopify_stores
    where store_key = p_store_key and shop_domain <> p_shop_domain
  ) then
    raise exception 'Store key "%" is already used by another shop', p_store_key;
  end if;

  perform public.create_shopify_store_table(p_store_key);

  insert into public.shopify_stores (shop_domain, status, store_key, approved_at)
  values (p_shop_domain, 'approved', p_store_key, now())
  on conflict (shop_domain) do update
    set status = 'approved', store_key = excluded.store_key,
        approved_at = now(), updated_at = now();

  perform public.refresh_shopify_store_products_view();
end
$$;

-- Stops publishing a store and hides it from the view. Its table is kept.
create or replace function public.pause_shopify_store(p_shop_domain text)
returns void
language plpgsql
security definer
set search_path = ''
as $$
begin
  update public.shopify_stores
  set status = 'paused', updated_at = now()
  where shop_domain = p_shop_domain;
  perform public.refresh_shopify_store_products_view();
end
$$;

revoke all on function public.create_shopify_store_table(text) from public, anon, authenticated;
revoke all on function public.refresh_shopify_store_products_view() from public, anon, authenticated;
revoke all on function public.approve_shopify_store(text, text) from public, anon, authenticated;
revoke all on function public.pause_shopify_store(text) from public, anon, authenticated;
grant execute on function public.approve_shopify_store(text, text) to service_role;
grant execute on function public.pause_shopify_store(text) to service_role;
grant execute on function public.refresh_shopify_store_products_view() to service_role;

-- Stores set up before the registry existed. Their tables already exist.
select public.approve_shopify_store('oversized-liftingclub.myshopify.com', 'olc');
select public.approve_shopify_store('highlide-test-store.myshopify.com', 'highlide_test');

commit;

notify pgrst, 'reload schema';
