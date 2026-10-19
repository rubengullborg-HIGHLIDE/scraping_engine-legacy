begin;
alter table public.catalog_sync_runs drop constraint catalog_sync_runs_store_check;
alter table public.catalog_sync_runs add constraint catalog_sync_runs_store_check check (
  store in ('kaufmann', 'weekday', 'rains', 'romerhus', 'suitclub', 'cejf',
            'skagen_clothing', 'shoechapter', 'stoy', 'lakor', 'axel', 'quint', 'salling', 'magasin')
);
commit;
