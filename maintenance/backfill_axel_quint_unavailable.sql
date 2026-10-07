-- Run AFTER old AXEL/qUINT jobs finish and fixed code is deployed.
-- Marks discontinued colours unavailable and zeroes published stale stock.
-- Preserves raw source data, prices and metadata. Safe to rerun, including
-- after the earlier lifecycle-only repair. No scraping or schema migration.
begin;

create temporary table unavailable_candidates on commit drop as
select 'axel'::text as store, id from public.axel_products
where (publication_status = 'unavailable' and status_reason = 'source_available_false')
   or (publication_status = 'active' and raw->'source_available' = 'false'::jsonb
       and ((last_seen_in_catalog_at is null and status_checked_at is null)
            or (inventory_checked_at <= last_seen_in_catalog_at
                and status_checked_at <= last_seen_in_catalog_at)))
union all
select 'quint'::text as store, id from public.quint_products
where (publication_status = 'unavailable' and status_reason = 'source_available_false')
   or (publication_status = 'active' and raw->'source_available' = 'false'::jsonb
       and ((last_seen_in_catalog_at is null and status_checked_at is null)
            or (inventory_checked_at <= last_seen_in_catalog_at
                and status_checked_at <= last_seen_in_catalog_at)));

select store, count(*) as rows_to_mark_unavailable
from unavailable_candidates group by store;

-- Session-local helpers preserve shop names, size labels and source identifiers.
create function pg_temp.zero_local_inventory(inventory jsonb)
returns jsonb language sql immutable as $$
  select coalesce(inventory, '{}'::jsonb) || jsonb_build_object('stores',
    coalesce((select jsonb_object_agg(shop.key,
      shop.value || jsonb_build_object('available', false, 'total_stock', 0,
        'sizes', coalesce((select jsonb_object_agg(size.key,
          size.value || '{"available":false,"stock":0}'::jsonb)
          from jsonb_each(coalesce(shop.value->'sizes', '{}'::jsonb)) size), '{}'::jsonb)))
      from jsonb_each(coalesce(inventory->'stores', '{}'::jsonb)) shop), '{}'::jsonb))
$$;

create function pg_temp.zero_webshop_sizes(sizes jsonb)
returns jsonb language sql immutable as $$
  select coalesce(jsonb_agg(value ||
    '{"available":false,"stock":0,"online_warehouse_stock":0}'::jsonb order by ordinal), '[]'::jsonb)
  from jsonb_array_elements(coalesce(sizes, '[]'::jsonb)) with ordinality as s(value, ordinal)
$$;

update public.axel_products p
set publication_status = 'unavailable', status_reason = 'source_available_false',
    status_checked_at = now(), discontinued_at = coalesce(p.discontinued_at, now()),
    local_inventory = pg_temp.zero_local_inventory(p.local_inventory),
    webshop_sizes = pg_temp.zero_webshop_sizes(p.webshop_sizes),
    local_available = false, aarhus_available = false,
    local_total_stock = 0, aarhus_total_stock = 0
from unavailable_candidates c where c.store = 'axel' and c.id = p.id;

update public.quint_products p
set publication_status = 'unavailable', status_reason = 'source_available_false',
    status_checked_at = now(), discontinued_at = coalesce(p.discontinued_at, now()),
    local_inventory = pg_temp.zero_local_inventory(p.local_inventory),
    webshop_sizes = pg_temp.zero_webshop_sizes(p.webshop_sizes),
    local_available = false, aarhus_available = false,
    local_total_stock = 0, aarhus_total_stock = 0
from unavailable_candidates c where c.store = 'quint' and c.id = p.id;

drop function pg_temp.zero_local_inventory(jsonb);
drop function pg_temp.zero_webshop_sizes(jsonb);
commit;
