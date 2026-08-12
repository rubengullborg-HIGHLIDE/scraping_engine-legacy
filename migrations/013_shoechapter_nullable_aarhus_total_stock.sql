alter table if exists public.shoechapter_products
  alter column aarhus_total_stock drop not null,
  alter column aarhus_total_stock drop default;

with known_totals as (
  select
    p.id,
    coalesce(
      sum((size_item.value ->> 'stock')::integer)
        filter (where jsonb_typeof(size_item.value -> 'stock') = 'number'),
      0
    )::integer as known_stock_sum
  from public.shoechapter_products p
  left join lateral jsonb_each(
    coalesce(
      p.local_inventory -> 'stores' -> 'shoechapter-aarhus' -> 'sizes',
      '{}'::jsonb
    )
  ) as size_item(key, value) on true
  group by p.id
)
update public.shoechapter_products p
set
  local_total_stock = known_totals.known_stock_sum,
  aarhus_total_stock = case
    when coalesce((p.local_inventory -> 'stores' -> 'shoechapter-aarhus' ->> 'stock_known')::boolean, false)
      then known_totals.known_stock_sum
    else null
  end
from known_totals
where p.id = known_totals.id;

comment on column public.shoechapter_products.local_total_stock is
  'Sum of known exact per-size local stock counts. For Shoe Chapter this can be a lower bound when some available sizes are rendered only as På lager.';

comment on column public.shoechapter_products.aarhus_total_stock is
  'Exact Aarhus total stock only when every available Aarhus size has a count; null when the rendered page only gives partial counts.';
