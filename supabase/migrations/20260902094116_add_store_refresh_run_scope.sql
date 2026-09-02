alter table public.store_inventory_refresh_runs
  add column if not exists row_limit integer,
  add column if not exists row_offset integer not null default 0,
  add column if not exists history_enabled boolean not null default true;

alter table public.store_inventory_refresh_runs
  drop constraint if exists store_inventory_refresh_runs_limit_check,
  add constraint store_inventory_refresh_runs_limit_check
    check (row_limit is null or row_limit > 0),
  drop constraint if exists store_inventory_refresh_runs_offset_check,
  add constraint store_inventory_refresh_runs_offset_check
    check (row_offset >= 0);
