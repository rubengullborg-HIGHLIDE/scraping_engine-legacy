drop trigger if exists store_inventory_snapshots_mirror_history
  on public.store_inventory_snapshots;

drop function if exists public.mirror_legacy_store_inventory_snapshot();

truncate table only public.store_inventory_snapshots restart identity;

comment on table public.store_inventory_snapshots is
  'Retired legacy daily snapshot table. Historical rows were exported and compacted into store_inventory_history on September 2, 2026; current jobs must not write here.';
