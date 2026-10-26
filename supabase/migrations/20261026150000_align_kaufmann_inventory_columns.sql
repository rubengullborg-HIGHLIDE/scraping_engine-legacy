-- Give kaufmann_products the shared store inventory shape and retire the
-- Kaufmann-only history objects. Stop both scraper timers before applying.
--
-- Kaufmann tracks only its three Aarhus shops, so the local summaries equal
-- the Aarhus summaries. aarhus_total_stock and aarhus_available stay, as on
-- every other store table.
--
-- Database readers: search_products_ranked, refresh_search_catalog (the
-- pg_cron source of search_catalog / search_products_ranked_v2),
-- get_highlide_dashboard and the backstage_catalogue_v1 view (read by
-- backstage_catalogue_page_v1) use only aarhus_total_stock, aarhus_available
-- and catalog columns, none of which change. No other function or view
-- references the renamed or dropped columns.
--
-- Expand step: the website still selects aarhus_inventory and
-- last_inventory_checked_at, and Backstage still reads
-- kaufmann_inventory_refresh_runs. The read-only aliases below and that table
-- remain until both apps ship; maintenance/drop_kaufmann_compatibility.sql
-- then removes them.
begin;
set local lock_timeout = '10s';
lock table public.kaufmann_products in access exclusive mode;

alter table public.kaufmann_products rename column aarhus_inventory to local_inventory;
alter table public.kaufmann_products rename column last_inventory_checked_at to inventory_checked_at;
alter index public.kaufmann_products_aarhus_inventory_gin_idx
    rename to kaufmann_products_local_inventory_gin_idx;

alter table public.kaufmann_products
    add column local_total_stock integer not null default 0,
    add column local_available boolean not null default false;
update public.kaufmann_products
set local_total_stock = aarhus_total_stock,
    local_available = aarhus_available;

alter table public.kaufmann_products
    add constraint kaufmann_products_local_inventory_shape check (
        jsonb_typeof(local_inventory) = 'object'
        and local_inventory ? 'stores'
        and jsonb_typeof(local_inventory -> 'stores') = 'object'),
    add constraint kaufmann_products_local_total_stock_nonnegative check (local_total_stock >= 0),
    add constraint kaufmann_products_aarhus_total_stock_nonnegative check (aarhus_total_stock >= 0);

-- The shared refresh logs failures and orders by updated_at, like other stores.
drop index public.kaufmann_products_active_inventory_checked_idx;
alter table public.kaufmann_products
    drop column last_refresh_error,
    drop column last_refresh_error_at;

comment on column public.kaufmann_products.source_available is
    'Kaufmann colour-level availability flag. A false value zeroes stock.';
comment on column public.kaufmann_products.consecutive_source_misses is
    'Successful refreshes in a row whose variation response omitted this colour; two mark it unavailable.';

-- Temporary read-only aliases for the website (see header).
alter table public.kaufmann_products
    add column aarhus_inventory jsonb generated always as (local_inventory) stored,
    add column last_inventory_checked_at timestamptz generated always as (inventory_checked_at) stored;
comment on column public.kaufmann_products.aarhus_inventory is
    'Deprecated read-only alias of local_inventory; dropped after the website reads local_inventory.';
comment on column public.kaufmann_products.last_inventory_checked_at is
    'Deprecated read-only alias of inventory_checked_at; dropped after the website reads inventory_checked_at.';

-- The compatibility view and its orphaned trigger function have no readers;
-- Kaufmann history is store_inventory_history where store = 'kaufmann'.
drop function public.merge_kaufmann_inventory_history_observation();
drop view public.kaufmann_inventory_history;

notify pgrst, 'reload schema';
commit;
