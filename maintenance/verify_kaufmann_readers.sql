-- Read-only fingerprints of every reader of kaufmann_products. Run with the
-- scraper timers stopped, before and after the migration, and diff the output:
--   psql "$DB" -X -f maintenance/verify_kaufmann_readers.sql > before.txt
set default_transaction_read_only = on;
\pset format unaligned
\pset tuples_only on
select 'search_jakke', md5(string_agg(row_to_json(r)::text, '|' order by ord)), count(*) from (select row_number() over () ord, * from public.search_products_ranked('jakke', array['kaufmann'], p_limit => 500)) r;
select 'dashboard', md5(public.get_highlide_dashboard()::text);
select 'catalogue_view', md5(string_agg(row_to_json(c)::text, '|' order by key)), count(*) from public.backstage_catalogue_v1 c where retailer = 'kaufmann';
select 'catalogue_page', md5(public.backstage_catalogue_page_v1(jsonb_build_object('start','2026-10-01T00:00:00Z','end','2026-10-26T00:00:00Z','retailer','kaufmann','pageSize',100,'page',0))::text);
select 'catalogue_page_all', md5(public.backstage_catalogue_page_v1(jsonb_build_object('start','2026-10-01T00:00:00Z','end','2026-10-26T00:00:00Z','pageSize',100,'page',3,'sort','stock_desc'))::text);
select 'search_boss', md5(string_agg(row_to_json(r)::text, '|' order by ord)), count(*), max(total_count) from (select row_number() over () ord, * from public.search_products_ranked('boss', array['kaufmann'], p_limit => 500)) r;
select 'search_skjorte_all', md5(string_agg(row_to_json(r)::text, '|' order by ord)), count(*), max(total_count) from (select row_number() over () ord, * from public.search_products_ranked('skjorte', '{}', p_sort => 'price_asc', p_limit => 500)) r;
select 'search_sale_kaufmann', md5(string_agg(row_to_json(r)::text, '|' order by ord)), count(*), max(total_count) from (select row_number() over () ord, * from public.search_products_ranked('sort', array['kaufmann'], p_on_sale => true, p_limit => 500)) r;
select 'website_compat', md5(string_agg(concat_ws('|', id, aarhus_inventory::text, last_inventory_checked_at::text, aarhus_total_stock, aarhus_available), E'\n' order by id)) from public.kaufmann_products;
select 'search_v2_kaufmann', md5(string_agg(row_to_json(c)::text, '|' order by c.id)), count(*) from public.search_catalog c where c.source_key = 'kaufmann';
