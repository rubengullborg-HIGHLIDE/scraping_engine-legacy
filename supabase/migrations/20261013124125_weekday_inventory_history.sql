begin;
alter table public.store_inventory_history
    drop constraint store_inventory_history_store_check;
alter table public.store_inventory_history
    add constraint store_inventory_history_store_check check (
        store in ('rains', 'romerhus', 'suitclub', 'cejf', 'skagen_clothing',
                  'shoechapter', 'stoy', 'lakor', 'axel', 'quint',
                  'salling', 'magasin', 'weekday')
    );
commit;
