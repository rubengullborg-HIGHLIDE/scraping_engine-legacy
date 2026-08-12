# HIGHLIDE Scraping Engine

This repository contains the Python scraping and data-ingestion jobs for HIGHLIDE, a platform for showing clothing products available in smaller local fashion stores, starting in Aarhus, Denmark.

## Current Purpose

The scraper project has two separate ingestion concerns:

- Full catalog import: initial or occasional broad product imports.
- Inventory refresh: frequent dynamic refresh of price and size availability for already-known products.

Do not mix these two paths. Full import can collect names, descriptions, images, brand, category, materials, color, fit, and broad metadata. Refresh jobs should stay narrow and deterministic: current price and size availability for existing database rows.

## Project Structure

```text
.
├── AGENTS.md
├── requirements.txt
├── deployment/
│   ├── digitalocean.md
│   └── systemd/
│       ├── highlide-kaufmann-refresh.service
│       └── highlide-kaufmann-refresh.timer
├── migrations/
│   ├── 001_product_inventory_snapshots.sql
│   ├── 002_simplify_product_inventory_snapshots.sql
│   ├── 003_kaufmann_products.sql
│   ├── 004_kaufmann_aarhus_inventory.sql
│   ├── 005_clean_aarhus_inventory_interface.sql
│   ├── 006_kaufmann_inventory_snapshots.sql
│   ├── 007_romerhus_products.sql
│   ├── 008_lakor_products.sql
│   ├── 009_rains_products.sql
│   ├── 010_stoy_products.sql
│   ├── 011_stoy_remove_unpublished_fields.sql
│   ├── 012_shoechapter_products.sql
│   └── 013_shoechapter_nullable_aarhus_total_stock.sql
├── scrapers/
│   ├── base.py
│   ├── full_import/
│   │   ├── base.py
│   │   ├── kaufmann.py
│   │   ├── lakor.py
│   │   ├── rains.py
│   │   ├── romerhus.py
│   │   ├── shoechapter.py
│   │   ├── stoy.py
│   │   └── st_valentin.py
│   └── stores/
│       ├── kaufmann.py
│       └── st_valentin.py
└── scripts/
    ├── import_kaufmann_products.py
    ├── import_lakor_products.py
    ├── import_rains_products.py
    ├── import_romerhus_products.py
    ├── import_shoechapter_products.py
    ├── import_stoy_products.py
    ├── refresh_kaufmann_inventory.py
    └── refresh_inventory.py
```

## Scraper Boundaries

### Full Import

Location: `scrapers/full_import/`

These are store-specific catalog scrapers. They are allowed to parse broad product data:

- product URL
- name
- brand
- price at import time
- images
- description
- materials, color, fit, category
- size availability if visible
- store/source info

Kaufmann full import is currently wired through `scripts/import_kaufmann_products.py`. Other full-import scrapers are still saved implementations until dedicated runners are added.

### LAKOR Full Import

Location:

- `scripts/import_lakor_products.py`
- `scrapers/full_import/lakor.py`
- `migrations/008_lakor_products.sql`

LAKOR is a Shopify storefront. Import `all-clothing` catalogue products into
the dedicated `lakor_products` table. Shopify's collection feed omits product
type, so it is discovery only: hydrate each product from its `.js` feed and
keep only established clothing types. Do not import accessories, headwear,
posters, or gift cards.

Each Shopify product represents one colour and its Shopify variants represent
sizes. The stable row key is `source_parent_id + source_color_id`: the first
component of LAKOR's SKU (for example `L1015`) is `source_parent_id` and the
Shopify product id is `source_color_id`. Keep the exact SKU colour as `color`.
Follow every product-page colour-swatch link during import so a direct URL or
limited batch still imports all linked colours of a style. Derive
`color_group` deterministically from LAKOR's displayed swatch hex value and
retain that exact hex value in `raw` for debugging or future remapping.

LAKOR product pages expose product-specific sections. Store them without
mixing in LAKOR's generic brand marketing blocks:

- `Historien` is `description`.
- `Highlights` is a list in `highlights`.
- `Specifikationer` is retained in `specifications`; map colour, material,
  fit, wash instruction, and country of origin into dedicated fields.
- Preserve model notes and size-guide measurements in `model_info` and
  `size_guide`.

Shopify supplies the live online `available` flag per size, and
`compare_at_price` supplies the former price during a sale. It does not expose
online-stock counts.

LAKOR's public variant endpoint is:

```text
GET /variants/{shopify_variant_id}?section_id=store-availability
```

It provides per-size local availability notices for LAKOR's own stores:

```text
lakor-aarhus       LAKOR Shop Aarhus
lakor-copenhagen   LAKOR Shop KBH
lakor-aalborg      LAKOR Shop Aalborg
```

It never provides local quantities. Use `local_inventory` with
`stock_known: false`, `total_stock: null`, and `stock: null`. When a notice
only says "vores butikker", retain that positive overall signal in
`local_available` and `raw`. LAKOR uses its generic “i vores butikker” wording
for availability in all three tracked shops; map that size to all store keys.
When availability is restricted, the endpoint explicitly names the remaining
shops instead. It never provides local quantities.

Useful commands:

```bash
python scripts/import_lakor_products.py --discover-only --preview 10
python scripts/import_lakor_products.py --dry-run --limit 1 --no-delay
python scripts/import_lakor_products.py --url https://www.lakor.dk/products/sport-cola-stripe-short-sleeve-shirt-harbor-blue --dry-run --no-delay
```

### Rains Full Import

Location:

- `scripts/import_rains_products.py`
- `scrapers/full_import/rains.py`
- `migrations/009_rains_products.sql`

Rains is a Shopify storefront where each product is a style, the first variant
option is colour, and the second is size. Import one `rains_products` row per
colour. Use the Rains style number as `source_parent_id` and the normalized
style-colour SKU prefix as `source_color_id`, for example `19030 + 19030-177`.

Do not rely only on the broad `mens-clothing` and `mens-outerwear`
collections. The default discovery set combines and deduplicates all men's
clothing navigation subcollections, including knitwear, woven, fleece,
bottoms, and every outerwear subtype. Collection feeds are discovery only;
hydrate each style from its product `.js` feed and product HTML.

The public inventory endpoint returns all Danish stores in one request:

```text
GET https://rains-locations-api.vercel.app/api/get-inventory?locale=dk
```

Normalize Shopify SKUs such as `19030\\177\\L` to the inventory endpoint's
`19030-177-L` form. Missing SKUs in a warehouse mean zero stock. Store exact
quantities for all sizes in `local_inventory` under these stable keys:

```text
rains-aarhus         Rains Aarhus, Klostertorv
rains-copenhagen     Rains København, Amagertorv
rains-frederiksberg  Rains Frederiksberg, Gammel Kongevej
```

Keep source warehouse identifiers and addresses under `raw`. Use
`local_total_stock`, `local_available`, `aarhus_total_stock`, and
`aarhus_available` as query-friendly summaries. Shopify's `available` flag is
webshop availability only and belongs in `webshop_sizes`.

Rains product HTML also exposes descriptions, materials, functional details,
features, care instructions, the full category path, model measurements, and
size-guide measurements. Preserve compare-at prices as `list_price` only when
they exceed the current price. Full imports write incrementally in bounded
Supabase batches instead of waiting until the entire catalogue is hydrated.

Useful commands:

```bash
python scripts/import_rains_products.py --discover-only --preview 10
python scripts/import_rains_products.py --dry-run --limit 1 --no-delay
python scripts/import_rains_products.py --url https://www.dk.rains.com/products/fleece-zip-jacket-male --dry-run --no-delay
python scripts/import_rains_products.py
```

### STOY Full Import

Location:

- `scripts/import_stoy_products.py`
- `scrapers/full_import/stoy.py`
- `migrations/010_stoy_products.sql`
- `migrations/011_stoy_remove_unpublished_fields.sql` (only for databases
  that already applied the original STOY migration)

STOY is a Shopify storefront. Import the union of its canonical men's
`all-clothing-for-men` and `footwear-for-men` collections into the dedicated
`stoy_products` table, deduplicating any overlap. This intentionally includes
shoes but excludes accessories, home, fragrance, and other non-product categories.

Each Shopify product is one colour and its Shopify variants are sizes. The
stable row key is `source_parent_id + source_color_id`: use STOY's manufacturer
style reference as `source_parent_id` when it is available, and the Shopify
product id as `source_color_id`. Keep the full manufacturer style code and
other Shopify identifiers in `source_product_number`, `webshop_sizes`, and
`raw`, not in the clean inventory object.

STOY server-renders the **"Se tilgængelighed i vores butikker"** panel in each
product page. It exposes size-level availability for these two stores but no
quantities:

```text
stoy-aarhus       STOY Aarhus, Store Torv 4
stoy-copenhagen   STOY København, Landemærket 8
```

Use `local_inventory` with `stock_known: false`, `total_stock: null`, and
`stock: null`. Do not infer stock counts from a positive availability dot or
from Shopify's online `available` flag. The latter belongs only in
`webshop_sizes`. Store the rendered source labels and addresses under `raw`.
Use `local_available` and `aarhus_available` as query-friendly summaries;
the total-stock columns remain zero because quantities are unknown.

Product pages include STOY's color swatch, composition, country of origin,
fit guidance, category taxonomy, related colours, collection membership, and
images. Store the source product metafields in `specifications`; STOY does not
publish a separate highlights or care-instructions field, so do not create
placeholder columns for those. When present, parse the product-specific
`Størrelse & Pasform` accordion into `model_info` and `size_guide`.

Useful commands:

```bash
python scripts/import_stoy_products.py --discover-only --preview 10
python scripts/import_stoy_products.py --dry-run --limit 1 --no-delay
python scripts/import_stoy_products.py --url https://stoy.com/da/products/example --dry-run --no-delay
python scripts/import_stoy_products.py
```

### Shoe Chapter Full Import

Location:

- `scripts/import_shoechapter_products.py`
- `scrapers/full_import/shoechapter.py`
- `migrations/012_shoechapter_products.sql`
- `migrations/013_shoechapter_nullable_aarhus_total_stock.sql` (only for
  databases that already applied the original Shoe Chapter migration)

Shoe Chapter is a Shopify storefront. Import men's footwear from the
`collections/men` collection into the dedicated `shoechapter_products` table.
The collection includes non-footwear lifestyle items such as socks and
magazines, so the importer keeps Shopify `Sneakers` products only.

Each Shopify product is one colour and its Shopify variants are sizes. The
stable row key is `source_parent_id + source_color_id`: derive
`source_parent_id` from the variant SKU with the size suffix removed, and use
the Shopify product id as `source_color_id`. Product pages link other colours
under `Andre farver`; follow those links during direct URL imports so a single
test URL imports the full colour family.

Shoe Chapter product pages render exact Aarhus shop stock in the product main
section:

```text
shoechapter-aarhus   Shoe Chapter Aarhus, Store Torv 6
```

The rendered inventory text exposes per-size counts such as `Kun 1 enhed
tilbage`. Store this as known stock in `local_inventory` with
`stock_known: true` and exact `stock` values when every available size has a
count. Some available sizes render only as `På lager`; when this happens, keep
that size available with `stock: null`, set the store-level exact
`total_stock` and `aarhus_total_stock` to null, and set `local_total_stock` to
the sum of known exact size counts as a lower bound. Shopify's `available`
flag is webshop availability only and belongs in `webshop_sizes`.

Product pages expose description, feature bullets, related colours, tags,
images, colour text, fit guidance, and brand-specific size-guide tables. Store
the feature bullets in `highlights`, size tables in `size_guide`, and source
debug metadata in `raw`. Do not create placeholder `model_info`,
`care_instructions`, or `collections` columns unless the site starts exposing
real data for them.

Useful commands:

```bash
python scripts/import_shoechapter_products.py --discover-only --preview 10
python scripts/import_shoechapter_products.py --dry-run --limit 1 --no-delay
python scripts/import_shoechapter_products.py --url https://shoechapter.com/products/new-balance-u991ac2-wind-chime-brilliant-white --dry-run --no-delay
python scripts/import_shoechapter_products.py
```

### Skagen Clothing Full Import

Location:

- `scripts/import_skagen_clothing_products.py`
- `scrapers/full_import/skagen_clothing.py`
- `migrations/014_skagen_clothing_products.sql`

Skagen Clothing is a Shopify storefront. Discover products from the broad
`alt-toj-til-maend` collection and import them into the dedicated
`skagen_clothing_products` table. The source collection currently contains
203 clothing products after excluding the seven non-clothing product types:
Mystery Box, Accessories, Beanie, and Gavekort. Filter on product type rather
than the `ACCESSORIES` tag or collection because Skagen also classifies three
real tank tops as accessories.

Each Shopify product represents one colour and its variants represent sizes.
Follow the product-page colour swatches so a direct URL or limited discovery
batch imports all linked colours. Use the common stable swatch-handle prefix as
`source_parent_id` because Skagen's SKU prefixes are inconsistent across some
linked colours; fall back to the normalized SKU style prefix for products with
no linked colours. Use the Shopify product id as `source_color_id`, and retain
the normalized SKU reference, all variant SKUs, and colour-link source data in
`raw`.

The product page server-renders exact, size-level inventory in its
`data-variant-inventories` JSON. The "Se butik" drawer reads this same JSON and
does not make a separate stock request. Fetch product HTML with cache-busting
and no-cache request headers, and store exact quantities for:

```text
skagen-aarhus       Skagen Clothing Aarhus, Store Torv 14
skagen-copenhagen   Skagen Clothing Copenhagen, Klosterstræde 10
```

Do not count the internal Viby J warehouse (`8260`) or `Fiktiv location`
(`8240`) in clean local inventory or summary totals. Preserve all original
locations in `raw`. `local_total_stock` is the sum of known exact stock across
the two tracked shops; `aarhus_total_stock` is exact when the inventory payload
is present and otherwise null.

Skagen product pages expose description paragraphs, bullet highlights,
material compositions, fit language, a three-point fit indicator, model text,
an image-based Size Guide, tags, images, and product type. Store these fields
without adding placeholder fields such as country of origin, which Skagen does
not publish consistently.

Useful commands:

```bash
python scripts/import_skagen_clothing_products.py --discover-only --preview 10
python scripts/import_skagen_clothing_products.py --dry-run --limit 1 --no-delay
python scripts/import_skagen_clothing_products.py --url https://skagen-clothing.dk/products/example --dry-run --no-delay
python scripts/import_skagen_clothing_products.py
```

### Rømerhus Full Import

Location:

- `scripts/import_romerhus_products.py`
- `scrapers/full_import/romerhus.py`
- `migrations/007_romerhus_products.sql`

Rømerhus is the BESTSELLER Stores Shopify storefront. Import men's products
into the dedicated `romerhus_products` table. Each Shopify product is one
colour and its Shopify variants are sizes. Its unique key is
`source_parent_id + source_color_id`, using the BESTSELLER style reference
when available and the Shopify product id respectively.

Do not infer local stock from Shopify's online `available` flag. Exact Click &
Collect quantities are available through the public storefront endpoints:

```text
GET  /apps/bestseller-functions/locations
POST /apps/bestseller-functions/variant-stock
```

Store both locations in `local_inventory` using stable keys:

```text
romerhus-aarhus       BESTSELLER Aarhus - Rømerhus
gammeltorv-copenhagen BESTSELLER København – Gammeltorv
```

`local_inventory` follows the clean `{"stores": {...}}` interface. Keep source
location ids and raw variant-stock data in `raw`; use `local_total_stock`,
`local_available`, `aarhus_total_stock`, and `aarhus_available` for summaries.
The default discovery set covers the men's clothing-category collections and
excludes news, brand, and accessories collections because they can contain
non-clothing duplicates such as caps. Pass `--collection nyheder-test` when a
new-arrivals-only import is intentionally wanted. Catalogue collection feeds
are used only for discovery; the importer then reads each product's Shopify
product feed to retain the full description, price, type, colour, and material
data. Keep the default polite delay enabled for a full import.

Useful commands:

```bash
python scripts/import_romerhus_products.py --discover-only --preview 10
python scripts/import_romerhus_products.py --dry-run --limit 1 --no-delay
python scripts/import_romerhus_products.py --url https://bestseller-stores.dk/products/t-shirts-tops_t-shirt_relaxed-fit_black_16104335_5221603 --dry-run --no-delay
```

### Kaufmann Full Import

Location:

- `scripts/import_kaufmann_products.py`
- `scrapers/full_import/kaufmann.py`
- `migrations/003_kaufmann_products.sql`

Kaufmann products should be imported into the dedicated `kaufmann_products` table, not the frontend-facing `products` table.

The Kaufmann importer discovers product URLs from:

```text
https://www.kaufmann.dk/sitemap.xml
```

That sitemap points to a compressed child sitemap ending in `.xml.gz`; the importer follows and decompresses it. As of July 8, 2026 it discovers 4,878 Kaufmann product URLs.

The importer uses Playwright to read Kaufmann's live Alpine state:

```js
Alpine.store('productStore')
```

Kaufmann color variants are keyed by `colorId`. The stable unique key for rows in `kaufmann_products` is:

```text
source_parent_id + source_color_id
```

For each color variant, the importer stores:

- product/style metadata: URL, name, brand, description, materials, fit, images, price
- top-level `source_product_number` is currently null for Kaufmann variant rows; Kaufmann exposes product numbers at size level, so keep those source refs in `webshop_sizes` or `raw`, not in the frontend-facing inventory shape
- `category` is currently null unless a future scraper revision extracts a reliable Kaufmann category/breadcrumb
- `color`: the exact Kaufmann color name, for example `SORT`, `HVID`, `NAVY`
- `color_group`: Kaufmann's grouped color family, for example `Sort`, `Hvid`, `Blå`
- `webshop_sizes`: online-shop stock, kept only as reference
- `aarhus_inventory`: clean reusable Aarhus store inventory for product detail pages
- `aarhus_total_stock`
- `aarhus_available`

`aarhus_inventory` is a store-agnostic JSON interface. Keep source-specific ids such as Kaufmann `warehouse_id`, `productNumber`, and size variant ids out of this object; put them in `raw` if they are useful for debugging or future scraper work.

```json
{
  "stores": {
    "bruuns-galleri": {
      "name": "KAUFMANN Aarhus, Bruuns Galleri",
      "stock_known": true,
      "available": true,
      "total_stock": 4,
      "sizes": {
        "S": { "available": false, "stock": 0 },
        "M": { "available": false, "stock": 0 },
        "L": { "available": true, "stock": 2 }
      }
    }
  }
}
```

Use stable ASCII slugs as store keys, for example `aarhus-c`, `bruuns-galleri`, or `storcenter-nord`. Put the display label in `name`. If a future store only exposes whether a size is available but not exact counts, use `stock_known: false` and `stock: null`.

Keep `aarhus_total_stock` and `aarhus_available` as query-friendly summary columns. Do not recreate split top-level JSON columns such as `aarhus_sizes` or `aarhus_store_stock`.

The Aarhus stores currently tracked are:

```text
bruuns-galleri    KAUFMANN Aarhus, Bruuns Galleri
storcenter-nord   KAUFMANN Aarhus, Storcenter Nord
aarhus-c          KAUFMANN Aarhus, Strøget - Regina
```

Do not treat Kaufmann's top-level `availability` as local stock. That field is webshop availability. Local store inventory is under each size option's `stock` object, keyed by store/warehouse.

Useful commands:

```bash
python scripts/import_kaufmann_products.py --discover-only --preview 10
python scripts/import_kaufmann_products.py --dry-run --limit 1 --no-delay
python scripts/import_kaufmann_products.py --limit 100
python scripts/import_kaufmann_products.py --offset 100 --limit 100
```

Run large imports in batches with polite delays. A full Kaufmann import can involve thousands of product pages and multiple color variants per page.

Current Kaufmann import status as of July 8, 2026:

- The initial full Kaufmann import has completed.
- `kaufmann_products` contains 8,023 color-variant rows from 4,878 distinct product pages.
- 1,258 color variants have `aarhus_available = true`.
- 828 distinct product pages have at least one Aarhus-available color variant.
- The table contains 103 brands.
- Summed local Aarhus stock across all imported color variants is 20,297 units.
- `aarhus_inventory` uses the clean `{"stores": {...}}` interface on all rows.
- No persisted import log is guaranteed unless the script was run with shell redirection into `logs/`.

Useful status SQL:

```sql
select
  count(*) as total_variant_rows,
  count(distinct canonical_url) as total_product_pages,
  count(*) filter (where aarhus_available) as aarhus_available_variant_rows,
  count(distinct canonical_url) filter (where aarhus_available) as product_pages_with_any_aarhus_available_variant,
  max(scraped_at) as newest_scraped_at
from public.kaufmann_products;
```

### Inventory Refresh

Location:

- `scripts/refresh_inventory.py`
- `scripts/refresh_kaufmann_inventory.py`
- `scrapers/base.py`
- `scrapers/stores/`

This path reads existing product rows from Supabase and updates only dynamic fields. It must not create duplicate product rows.

The stable update key is the database product row `id`, because the job first reads products and then patches the same row. For long-term product identity across tables, prefer `store + source_variant_id` or `store + normalized variant URL` once full import captures those fields.

The existing generic refresh path is still oriented around the frontend-facing `products` table. For Kaufmann, the next planned task is to create a dedicated refresh cron script for `kaufmann_products`.

Kaufmann refresh is handled by `scripts/refresh_kaufmann_inventory.py`. It intentionally updates only dynamic fields on existing `kaufmann_products` rows:

- Read existing distinct `canonical_url` or `source_parent_id` values from `kaufmann_products`.
- Re-scrape each product page with the Kaufmann full-import parser.
- Patch existing rows by database `id`; do not create duplicate rows.
- Skip newly discovered color variants during refresh. Add them through full import instead.
- Refresh only dynamic fields: `current_price`, `list_price`, `webshop_sizes`, `aarhus_inventory`, `aarhus_total_stock`, `aarhus_available`, `scraped_at`, and `updated_at`.
- Keep the clean `aarhus_inventory` interface stable for the frontend.
- Keep source-specific ids under `raw` from full import when useful, but do not rewrite `raw` during daily refresh.
- If a known product page is empty, 404, or gone, mark its existing variants unavailable with empty inventory instead of deleting rows.
- Write logs to `logs/kaufmann_refresh.log` when run as cron.
- Use UTC timestamps in the database.

Kaufmann refresh snapshots are stored in `kaufmann_inventory_snapshots`. This table is intentionally lean because it will grow daily:

```text
kaufmann_inventory_snapshots
├── kaufmann_product_id
├── source_parent_id
├── source_color_id
├── canonical_url
├── source_url
├── checked_at
├── checked_bucket
├── refresh_status
├── current_price
├── list_price
├── webshop_sizes
├── aarhus_inventory
├── aarhus_total_stock
└── aarhus_available
```

It stores one snapshot per Kaufmann variant per UTC day via `unique (kaufmann_product_id, checked_bucket)`. It does not store full `raw`, images, descriptions, brand metadata, or other stable catalog fields.

Useful Kaufmann refresh commands:

```bash
python scripts/refresh_kaufmann_inventory.py --dry-run --url https://www.kaufmann.dk/produkt/boss-orange-196321 --no-delay
python scripts/refresh_kaufmann_inventory.py --limit 5 --no-delay
python scripts/refresh_kaufmann_inventory.py
```

Recommended Kaufmann daily cron entry:

```cron
15 2 * * * cd /opt/highlide/scraping_engine && .venv/bin/python scripts/refresh_kaufmann_inventory.py >> logs/kaufmann_refresh.log 2>&1
```

Current deployment status as of July 30, 2026:

- The Kaufmann refresh has been deployed on the DigitalOcean droplet at `~/scraping_engine`.
- The systemd timer `highlide-kaufmann-refresh.timer` is enabled and active.
- The droplet timezone is UTC, so the configured `02:15` timer runs around `04:15` Copenhagen summer time, plus up to `15m` randomized delay.
- A manual full service run was started successfully and logs showed progress such as `[15/4878] Refreshing ...`.
- Early observed droplet load during the run was about 77% CPU and 50% memory.
- Logs are written to `~/scraping_engine/logs/kaufmann_refresh.log` on the droplet.

July 31, 2026 refresh incident:

- The July 30 and July 31 Kaufmann refresh runs did not complete the full table.
- On July 31, Supabase showed about 5,457 of 8,023 variant rows updated, covering about 3,269 of 4,878 product pages.
- The remaining 2,566 variants still had July 8 `updated_at` values.
- `kaufmann_inventory_snapshots` showed similar partial coverage: 4,671 variants on July 30 and about 5,462 variants on July 31 at the time of inspection.
- The service has `TimeoutStartSec=12h`, so long runs can be killed by systemd before reaching the end.
- The original refresh script ordered work by `id`, so each daily run repeated the first part of the catalogue and never prioritized the stale tail if it timed out.
- The script has since been changed to read existing variants ordered by `updated_at.asc,id.asc`, so the oldest/stalest product pages refresh first.
- Product and snapshot writes have also been changed from per-variant REST writes to page-level batched upserts, reducing Supabase HTTP write overhead.
- The first batched write attempt failed on the droplet with `400 Bad Request ... kaufmann_products?on_conflict=id` because the upsert rows only included `id` plus dynamic fields. Supabase/PostgREST still needs required NOT NULL identity columns for the insert side of an upsert. `scripts/refresh_kaufmann_inventory.py` now includes existing `source_parent_id`, `source_color_id`, and `source_url` in each product upsert row while still only changing dynamic values in practice.

Useful droplet commands:

```bash
systemctl list-timers highlide-kaufmann-refresh.timer
systemctl status highlide-kaufmann-refresh.timer
systemctl status highlide-kaufmann-refresh.service
tail -f ~/scraping_engine/logs/kaufmann_refresh.log
journalctl -u highlide-kaufmann-refresh.service -n 100
```

Parallel refresh guidance:

- It is okay to run different store refresh scripts in parallel later if they touch separate store tables/rows and the droplet has enough CPU and memory.
- Do not run multiple full Kaufmann refresh services at the same time. The systemd service uses `flock` to prevent overlap.
- If Kaufmann ever needs parallelization, partition deliberately with `--offset` and `--limit`, use separate service names and lock files, and monitor Supabase/API load. Do not let two workers refresh the same `kaufmann_product_id` set for the same `checked_bucket`.
- With the current observed load, keep Kaufmann at one worker unless runtime becomes a real problem.

## Current Database Assumptions

The current production shape is a single `products` table with fields similar to:

- `id`
- `url`
- `navn`
- `pris`
- `brand`
- `description`
- `materials`
- `color`
- `fit`
- `sizes`
- `images`
- `store`
- `category`

The refresh job is configured through environment variables so it can map to this Danish/current schema.

There is also a dedicated Kaufmann import table:

```text
kaufmann_products
├── source_parent_id
├── source_color_id
├── source_url
├── canonical_url
├── source_product_number
├── name
├── brand
├── color
├── color_group
├── current_price
├── list_price
├── description
├── materials
├── fit
├── category
├── images
├── webshop_sizes
├── aarhus_inventory
├── aarhus_total_stock
├── aarhus_available
└── raw
```

`kaufmann_products` has RLS enabled. Do not add broad public policies without checking frontend access requirements.

Recommended current `.env` mappings:

```bash
SUPABASE_PRODUCTS_TABLE=products
SUPABASE_PRODUCT_ID_COLUMN=id
SUPABASE_PRODUCT_URL_COLUMN=url
SUPABASE_ACTIVE_COLUMN=skip
SUPABASE_CURRENT_PRICE_COLUMN=pris
SUPABASE_SIZE_STATUS_COLUMN=sizes
SUPABASE_JSON_TEXT_COLUMNS=sizes
SUPABASE_INVENTORY_HISTORY_TABLE=product_inventory_snapshots
SUPABASE_HISTORY_BUCKET_MINUTES=120
```

Do not commit `.env`. Use `.env.example` for non-secret examples.

## Supabase Credentials

Use the Supabase project URL and the server-side secret key:

```bash
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SECRET_KEY=your-sb-secret-key
```

Do not use the publishable key for cron/server refresh jobs. Do not expose the secret key to frontend code.

## History Table

The current history table is intentionally simple:

```text
product_inventory_snapshots
├── product_id
├── store
├── product_url
├── checked_at
├── checked_bucket
├── price
└── sizes
```

`checked_at` is stored in UTC. Denmark time should be handled in the frontend or reporting layer.

The refresh job upserts one row per product per `checked_bucket`, currently defaulting to 120 minutes. This makes cron retries idempotent inside the same two-hour window.

## Local Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chromium
```

Dry run:

```bash
python scripts/refresh_inventory.py --dry-run --limit 3 --no-delay
```

Limited write test:

```bash
python scripts/refresh_inventory.py --limit 3 --no-delay
```

Production-style run:

```bash
python scripts/refresh_inventory.py
```

## Cron Deployment

Recommended DigitalOcean cron entry:

```cron
0 */2 * * * cd /opt/highlide/scraping_engine && .venv/bin/python scripts/refresh_inventory.py >> logs/refresh_inventory.log 2>&1
```

Recommended Kaufmann refresh cron entry:

```cron
15 2 * * * cd /opt/highlide/scraping_engine && .venv/bin/python scripts/refresh_kaufmann_inventory.py >> logs/kaufmann_refresh.log 2>&1
```

Make sure the server has:

- Python virtualenv installed
- `requirements.txt` installed
- Playwright Chromium installed
- `.env` present with Supabase secret credentials
- `logs/` directory created

Prefer the systemd timer files in `deployment/systemd/` for the Kaufmann daily refresh on DigitalOcean. They include no-overlap locking with `flock`, daily scheduling, and a 12-hour timeout for long Playwright runs. See `deployment/digitalocean.md` for the full server runbook.

## Known Limitation: Color Variants

Many fashion store pages represent one clothing style with multiple color variants. Each color can have different size availability.

The current `products` table stores product rows with a `color` field, but not a stable source variant id. Kaufmann refresh currently tries to select the matching color using the row color text before extracting sizes. This is a heuristic and can be wrong when the store page exposes only family-level product data or image-only color swatches.

For Kaufmann, the full-import path now solves this by importing one row per color variant into `kaufmann_products`, using Kaufmann's `colorId` as `source_color_id`. Prefer this table for Kaufmann catalog experiments and local Aarhus inventory modeling.

The better future model is:

```text
products
  shared style/catalog fields

product_variants
  product_id
  store
  color
  source_variant_id
  variant_url
  images
  current_price

variant_inventory_snapshots
  variant_id
  checked_at
  checked_bucket
  price
  sizes
```

Until then, treat size availability on multi-color products as best-effort.

## Engineering Notes

- Keep refresh deterministic. Do not use AI for price or stock refresh.
- Prefer explicit store-specific selectors over a generic parser until there are enough stores to justify abstraction.
- Keep delays polite. Small local stores should not be hit aggressively.
- Store raw/debug data only when actively debugging; keep long-term history narrow.
- Use UTC timestamps in the database.
- Avoid schema churn in the frontend-facing `products` table unless the frontend is updated with it.
