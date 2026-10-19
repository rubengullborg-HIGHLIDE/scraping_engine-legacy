-- Run in one PostgreSQL session. Only temporary objects are written; rollback
-- leaves product/history/run tables unchanged. Uses the installed trigger code.
begin;
create temporary table inventory_history_check
  (like public.store_inventory_history including all) on commit drop;

do $test$
declare definition text;
begin
  select pg_get_functiondef('public.merge_store_inventory_history_observation()'::regprocedure)
    into definition;
  definition := replace(definition,
    'FUNCTION public.merge_store_inventory_history_observation()',
    'FUNCTION pg_temp.merge_inventory_history_check()');
  definition := replace(definition, 'public.store_inventory_history',
    'pg_temp.inventory_history_check');
  execute definition;
end
$test$;

create trigger merge_history before insert on inventory_history_check
  for each row execute function pg_temp.merge_inventory_history_check();

insert into inventory_history_check
  (id, store, product_id, state_hash, observed_from, observed_through,
   last_observed_bucket, observation_count, refresh_status, current_price, local_inventory)
values
  (1,'axel',1,'pending','2026-10-18 10:00Z','2026-10-18 10:00Z','2026-10-18',1,'ok',100,'{"stores":{}}'),
  (2,'axel',1,'pending','2026-10-18 10:00Z','2026-10-18 10:00Z','2026-10-18',1,'ok',100,'{"stores":{}}'),
  (3,'axel',1,'pending','2026-10-18 11:00Z','2026-10-18 11:00Z','2026-10-18',1,'ok',90,'{"stores":{}}'),
  (4,'axel',1,'pending','2026-10-18 11:00Z','2026-10-18 11:00Z','2026-10-18',1,'ok',90,'{"stores":{}}'),
  (5,'axel',1,'pending','2026-10-19 11:00Z','2026-10-19 11:00Z','2026-10-19',1,'ok',90,'{"stores":{}}');

do $test$
begin
  if (select count(*) from inventory_history_check) <> 2
     or (select sum(observation_count) from inventory_history_check) <> 3 then
    raise exception 'History replay validation failed';
  end if;
end
$test$;

select current_price, observed_from, observed_through, observation_count
  from inventory_history_check order by id;
rollback;
