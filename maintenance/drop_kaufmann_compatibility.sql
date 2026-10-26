-- Contract step for 20261026140000_align_kaufmann_inventory_columns.sql.
-- Apply only after the website reads local_inventory / inventory_checked_at
-- for Kaufmann and Backstage no longer reads kaufmann_inventory_refresh_runs.
-- The table's last write was 2026-10-18; current runs are in
-- store_inventory_refresh_runs.
begin;
set local lock_timeout = '10s';
alter table public.kaufmann_products
    drop column aarhus_inventory,
    drop column last_inventory_checked_at;
drop table public.kaufmann_inventory_refresh_runs;
notify pgrst, 'reload schema';
commit;
