-- Follow-up for databases that already applied 010_stoy_products.sql.
-- STOY does not publish dedicated highlights or care-instruction data.
alter table if exists public.stoy_products
  drop constraint if exists stoy_products_highlights_array,
  drop constraint if exists stoy_products_care_instructions_array;

alter table if exists public.stoy_products
  drop column if exists highlights,
  drop column if exists care_instructions;
