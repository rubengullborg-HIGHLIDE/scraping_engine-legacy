-- PostgreSQL 15+ (security-invoker view). Stop all scraper writers before applying.
begin;
set local lock_timeout = '10s';
lock table public.weekday_products, public.kaufmann_inventory_history,
    public.store_inventory_history in access exclusive mode;

alter table public.store_inventory_history
    drop constraint store_inventory_history_store_check,
    add constraint store_inventory_history_store_check check (
        store in ('rains', 'romerhus', 'suitclub', 'cejf', 'skagen_clothing',
                  'shoechapter', 'stoy', 'lakor', 'axel', 'quint',
                  'salling', 'magasin', 'weekday', 'kaufmann')),
    add column source_available boolean,
    add column publication_status text check (
        publication_status is null or publication_status in ('active', 'unavailable', 'missing')),
    add column status_reason text;

create or replace function public.merge_store_inventory_history_observation()
returns trigger
language plpgsql
security invoker
set search_path = ''
as $$
declare
  previous_row public.store_inventory_history%rowtype;
begin
  new.local_inventory := coalesce(
    new.local_inventory,
    '{"stores": {}}'::jsonb
  );
  new.state_hash := md5((
    jsonb_build_object(
      'refresh_status', new.refresh_status,
      'current_price', new.current_price,
      'list_price', new.list_price,
      'local_inventory', new.local_inventory,
      'local_total_stock', new.local_total_stock,
      'local_available', new.local_available,
      'aarhus_total_stock', new.aarhus_total_stock,
      'aarhus_available', new.aarhus_available
    ) || case when new.store = 'kaufmann' then jsonb_build_object(
      'source_available', new.source_available,
      'publication_status', new.publication_status,
      'status_reason', new.status_reason
    ) else '{}'::jsonb end
    )::text
  );

  select history.*
  into previous_row
  from public.store_inventory_history as history
  where history.store = new.store
    and history.product_id = new.product_id
  order by history.observed_through desc, history.id desc
  limit 1
  for update;

  if found and previous_row.state_hash = new.state_hash then
    update public.store_inventory_history
    set
      observed_through = greatest(previous_row.observed_through, new.observed_through),
      last_observed_bucket = greatest(
        previous_row.last_observed_bucket,
        new.last_observed_bucket
      ),
      observation_count = previous_row.observation_count + case
        when new.last_observed_bucket > previous_row.last_observed_bucket then 1
        else 0
      end,
      updated_at = greatest(previous_row.updated_at, new.updated_at)
    where id = previous_row.id;

    return null;
  end if;

  return new;
end
$$;


-- Disable only interval merging during the historical copy: merging would lose
-- original interval boundaries and observation counts.
alter table public.store_inventory_history disable trigger store_inventory_history_merge_observation;
do $$ begin
    if exists (select 1 from public.store_inventory_history where store = 'kaufmann') then
        raise exception 'Shared Kaufmann history already exists; reconcile before migrating';
    end if;
end $$;
insert into public.store_inventory_history (
    store, product_id, state_hash, local_inventory, local_total_stock, local_available,
    observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at
)
select 'kaufmann', kaufmann_product_id, state_hash, aarhus_inventory, aarhus_total_stock, aarhus_available,
    observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at
from public.kaufmann_inventory_history order by id;

-- Hash the mapped state exactly as future observations will be hashed.
update public.store_inventory_history h set state_hash = md5((jsonb_build_object(
    'refresh_status', h.refresh_status, 'current_price', h.current_price, 'list_price', h.list_price,
    'local_inventory', h.local_inventory, 'local_total_stock', h.local_total_stock,
    'local_available', h.local_available, 'aarhus_total_stock', h.aarhus_total_stock,
    'aarhus_available', h.aarhus_available
) || jsonb_build_object('source_available', h.source_available,
    'publication_status', h.publication_status, 'status_reason', h.status_reason))::text)
where store = 'kaufmann';

-- Compare every retained value, including duplicate intervals (EXCEPT ALL).
do $$ begin
    if exists (
        (select kaufmann_product_id, aarhus_inventory, observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at from public.kaufmann_inventory_history
         except all
         select product_id, local_inventory, observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at from public.store_inventory_history where store = 'kaufmann')
        union all
        (select product_id, local_inventory, observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at from public.store_inventory_history where store = 'kaufmann'
         except all
         select kaufmann_product_id, aarhus_inventory, observed_from, observed_through, last_observed_bucket, observation_count, refresh_status, current_price, list_price, aarhus_total_stock, aarhus_available, source_available, publication_status, status_reason, created_at, updated_at from public.kaufmann_inventory_history)
    ) then
        raise exception 'Kaufmann history copy mismatch; rolling back';
    end if;
end $$;
alter table public.store_inventory_history enable trigger store_inventory_history_merge_observation;
comment on table public.store_inventory_history is
    'Authoritative change-based inventory history for all stores, including migrated Kaufmann history.';

-- Keep the original rows/ids for rollback, with no application write access.
alter table public.kaufmann_inventory_history rename to kaufmann_inventory_history_archive;
revoke all on public.kaufmann_inventory_history_archive from public, anon, authenticated, service_role;
grant select on public.kaufmann_inventory_history_archive to service_role;
comment on table public.kaufmann_inventory_history_archive is
    'Frozen pre-unification backup. Current and historical observations are in store_inventory_history.';
create view public.kaufmann_inventory_history with (security_invoker = true) as
select id, product_id as kaufmann_product_id, state_hash,
    observed_from, observed_through, last_observed_bucket, observation_count,
    refresh_status, current_price, list_price, local_inventory as aarhus_inventory,
    aarhus_total_stock, aarhus_available, source_available, publication_status,
    status_reason, created_at, updated_at
from public.store_inventory_history where store = 'kaufmann';
revoke all on public.kaufmann_inventory_history from public, anon, authenticated, service_role;
grant select on public.kaufmann_inventory_history to service_role;

-- A common timestamp only advances after BOTH price and stock are verified.
alter table public.weekday_products add column inventory_checked_at timestamptz;
-- Migration must bypass the old UPDATE trigger, which rewrites updated_at.
alter table public.weekday_products disable trigger guard_weekday_product_observations;
update public.weekday_products
set inventory_checked_at = least(price_observed_at, inventory_observed_at);
alter table public.weekday_products
    alter column inventory_checked_at set not null;
create or replace function public.guard_weekday_product_observations()
returns trigger language plpgsql security invoker set search_path = '' as $$
begin
    if not new.inventory_complete or cardinality(new.inventory_warnings) > 0 then
        raise exception 'Incomplete Weekday inventory cannot replace a complete observation';
    end if;
    if new.inventory_checked_at < old.inventory_checked_at or new.updated_at < old.updated_at then
        raise exception 'Stale Weekday observation rejected';
    end if;
    new.first_seen_at := old.first_seen_at;
    new.scraped_at := greatest(old.scraped_at, new.scraped_at);
    return new;
end;
$$;
alter table public.weekday_products enable trigger guard_weekday_product_observations;
drop index public.weekday_products_aarhus_available_idx;
alter table public.weekday_products drop column price_observed_at, drop column inventory_observed_at;
create index weekday_products_aarhus_available_idx
    on public.weekday_products (aarhus_available, inventory_checked_at);
notify pgrst, 'reload schema';
commit;
