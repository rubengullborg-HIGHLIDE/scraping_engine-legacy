-- Run only after the compact-history migration has copied and verified the
-- legacy observations. TRUNCATE releases the old table and index pages without
-- requiring a blocking VACUUM FULL.
truncate table public.kaufmann_inventory_snapshots restart identity;

comment on table public.kaufmann_inventory_snapshots is
  'Legacy daily Kaufmann snapshots. Current refreshes write change-based history to kaufmann_inventory_history.';
