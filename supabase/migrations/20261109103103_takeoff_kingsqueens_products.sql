-- Takeoff Copenhagen and Kings & Queens catalogue tables.
--
-- Both are Shopify stores imported by scripts/import_takeoff_products.py and
-- scripts/import_kingsqueens_products.py through scripts/shopify_import.py.
-- One row per Shopify product (= one colour), keyed on the Shopify product id.
-- Same lifecycle columns and access rules as the other store tables, so the
-- daily refresh and weekly catalogue batches can adopt them later.

begin;

do $$
declare
  t text;
begin
  foreach t in array array['takeoff_products', 'kingsqueens_products'] loop
    execute format($f$
      create table if not exists public.%1$I (
        id bigint generated always as identity primary key,
        source_product_id text not null unique,
        source_url text not null,
        canonical_url text,
        source_product_number text,
        name text not null,
        brand text,
        product_type text,
        color text,
        color_group text,
        current_price numeric,
        list_price numeric,
        currency text not null default 'DKK',
        description text,
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
        publication_status text not null default 'active',
        status_reason text,
        status_checked_at timestamptz,
        discontinued_at timestamptz,
        last_seen_in_catalog_at timestamptz,
        consecutive_catalog_misses integer not null default 0,
        constraint %1$s_local_total_stock_nonnegative check (local_total_stock >= 0),
        constraint %1$s_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0),
        constraint %1$s_webshop_sizes_array check (jsonb_typeof(webshop_sizes) = 'array'),
        constraint %1$s_local_inventory_shape check (
          jsonb_typeof(local_inventory) = 'object'
          and local_inventory ? 'stores'
          and jsonb_typeof(local_inventory -> 'stores') = 'object'
        ),
        constraint %1$s_raw_object check (jsonb_typeof(raw) = 'object'),
        constraint %1$s_publication_status_check check (publication_status in ('active', 'unavailable', 'missing')),
        constraint %1$s_catalog_misses_nonnegative check (consecutive_catalog_misses >= 0)
      )$f$, t);

    execute format('create index if not exists %I on public.%I (brand)', t || '_brand_idx', t);
    execute format('create index if not exists %I on public.%I (category)', t || '_category_idx', t);
    execute format('create index if not exists %I on public.%I (updated_at)', t || '_updated_at_idx', t);
    execute format('create index if not exists %I on public.%I (id) where aarhus_available is true', t || '_aarhus_available_idx', t);
    execute format('create index if not exists %I on public.%I (first_seen_at desc) where publication_status = ''active''', t || '_recent_products_idx', t);
    execute format('create index if not exists %I on public.%I using gin (local_inventory)', t || '_local_inventory_gin_idx', t);

    execute format('alter table public.%I enable row level security', t);
    execute format('revoke all on table public.%I from anon, authenticated', t);
    execute format('grant select on table public.%I to authenticated', t);
    execute format('grant select, insert, update, delete on table public.%I to service_role', t);
    execute format('grant usage, select on sequence public.%I to service_role', t || '_id_seq');
  end loop;
end
$$;

-- Takeoff also sells preloved (second-hand) items alongside new stock.
alter table public.takeoff_products
  add column if not exists condition text not null default 'new',
  drop constraint if exists takeoff_products_condition_check,
  add constraint takeoff_products_condition_check check (condition in ('new', 'preloved'));

drop policy if exists "Authenticated users can read Takeoff products" on public.takeoff_products;
create policy "Authenticated users can read Takeoff products"
  on public.takeoff_products for select to authenticated using (true);

drop policy if exists "Authenticated users can read Kings Queens products" on public.kingsqueens_products;
create policy "Authenticated users can read Kings Queens products"
  on public.kingsqueens_products for select to authenticated using (true);

comment on table public.takeoff_products is
  'Takeoff Copenhagen sneakers, streetwear and preloved; exact per-size stock for Takeoff Aarhus and Copenhagen from sell.takeoffcph.dk inventory levels.';
comment on column public.takeoff_products.webshop_sizes is
  'Shopify online availability per size. Takeoff sources most sizes in 5-10 days, so in_stock online does not mean shop stock.';
comment on table public.kingsqueens_products is
  'Kings & Queens men''s clothing and shoes; boolean per-size pickup availability for the Bruun''s Galleri shop in Aarhus (no quantities).';
comment on column public.kingsqueens_products.aarhus_total_stock is
  'Always null: Kings & Queens publishes availability, not quantities.';

commit;
