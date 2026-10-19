# DigitalOcean Deployment

This guide deploys HIGHLIDE's sequential inventory refresh jobs on a Linux droplet.

### October 19 safety and compatibility changes

Before deploying, run the offline suite and check the actual service wrapper:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
bash scripts/run_store_refresh.sh --help
```

The wrapper requires `--source-transport system-curl`; the shared runner must
accept it and retain Weekday in its store registry. Daily generic refreshes
select `publication_status = active` and `aarhus_available = true`. Broader
restock checks and resumable scheduling are separate follow-up work; do not
remove the daily filter as an incidental deployment change.

STOY and Skagen now reject incomplete store/size inventory instead of writing
false unavailability. These errors retain previous inventory and history, and
remain visible as failed products. Skagen discovery now uses the verified men's
`alt` collection and explicit men's tag, excludes Caps as well as the existing
non-clothing types, and fails explicitly on an empty or invalid collection.
These changes do not repair observations already written by older versions.

The checked-in systemd units match the current droplet path:

```text
/root/scraping_engine
```

## 1. Install System Packages

### Resumable initial Salling/Magasin imports

Use the initial-import batch runner rather than the weekly lifecycle sync for
large first imports. It uses the shared inventory lock, releases it between
100-page batches, and saves an atomic checkpoint only after database writes
succeed. Restart the same command after an interruption. Individual failed pages
are deferred while later catalogue batches continue; source access/transport
errors, database-write failures and timeouts stop the runner. Existing colour rows are
upserted by their source identity. Partial imports never reconcile missing
catalogue products.

Department-store imports write approximately ten colour rows at a time by
default (`--write-batch-size 10`), keeping complete colour families together.
Every successful write logs the saved checkpoint totals. Ctrl+C in the batch
runner signals the importer once and waits up to 90 seconds for fully parsed
pending families to be written and checkpointed before releasing the lock.
Discovery or a partially fetched family may need to be repeated. A forced kill
can lose pending in-memory progress, but confirmed database rows remain intact.

The checkpoint's `failures` object retains each deferred URL, its error type,
message and attempt count. A catalogue pass with deferred pages reports an
incomplete result. Add `--retry-failed` to the batch runner after fixing their
errors to make them eligible once again; saved pages remain skipped. This
initial-import workflow never treats a deferred page as a missing product.
An HTTP 5xx response that remains after the source transport's bounded retries
defers only that product. Empty or malformed variation JSON also defers the
product with HTTP status, response content type and byte count recorded in its
error. Magasin can return empty HTTP 200 responses for stale catalogue links.
Five consecutive server/payload-error products stop the store as a probable
outage; a successful product resets this count. Verification challenges, access blocks,
rate limits, connection errors and database-write failures still stop the run.

```bash
.venv/bin/python -u scripts/run_department_import_batches.py \
  --store salling --checkpoint logs/salling_initial_checkpoint.json \
  --batch-pages 100
```

For recovery from the October 13 initial Salling import only, add
`--seed-import-log logs/department_catalog_sync.log` when creating the
checkpoint. This recovers successfully hydrated pages preceding confirmed
write markers, conservatively replaying the last possibly split colour family.
Discovery is frozen in the checkpoint, so later listing changes cannot shift
the remaining work. Use a new checkpoint for each later full catalogue import.

The same runner supports `--store magasin`. On October 14, Magasin's public
catalogue returned a Cloudflare verification page (403) from the droplet with
Requests, curl and standard Chromium, but worked from the development computer.
Run it from a network with verified public access until droplet access is
resolved; a 403 stops the store and retains saved inventory. Background runs on
the development computer require it to remain awake and connected.

The batch runner accepts `--request-delay 1.5` to space source requests at
least 1.5 seconds apart rather than the default three seconds. Size inventory
still needs separate source requests; do not infer shop stock from online
availability to speed up the import. HTTP 403 and 429 stop the store.
Validated related colour URLs are checkpointed together, including URLs outside
the current batch, so subsequent batches skip already imported colour families.
Magasin range prices use the published minimum as the colour row's starting
price, retain the full range in `raw.source_product.price`, and store exact
`current_price`/`list_price` per size in `webshop_sizes`. A top-level former price
is retained only when both range bounds share that former price.
Magasin's size picker can include nonexistent colour/size combinations with
empty product IDs and URLs, `showProduct: false` and `selectable: false`.
These placeholders are retained in raw variations but excluded from both local
and webshop size inventory, even if their source `inStock` flag is true.
Missing IDs on published or selectable size options remain parser errors.

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
```

## 2. Put The Project On The Server

Clone or copy the repository to the checked-in service path:

```bash
cd /root
git clone <your-repo-url> scraping_engine
cd scraping_engine
```

If the repo is already there:

```bash
cd /root/scraping_engine
git pull
```

## 3. Create Python Environment

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m playwright install chromium
```

If Playwright reports missing Linux browser dependencies, run:

```bash
.venv/bin/python -m playwright install-deps chromium
```

## 4. Configure Secrets

Create `/root/scraping_engine/.env` from `.env.example`:

```bash
cp .env.example .env
nano .env
```

Required:

```bash
SUPABASE_URL=https://your-project-ref.supabase.co
SUPABASE_SECRET_KEY=your-supabase-secret-key
KAUFMANN_PRODUCTS_TABLE=kaufmann_products
KAUFMANN_INVENTORY_HISTORY_TABLE=kaufmann_inventory_history
KAUFMANN_INVENTORY_REFRESH_RUNS_TABLE=kaufmann_inventory_refresh_runs
STORE_INVENTORY_HISTORY_TABLE=store_inventory_history
STORE_INVENTORY_REFRESH_RUNS_TABLE=store_inventory_refresh_runs
```

Use the server-side Supabase secret key. Do not use a frontend publishable key for this job.

## 5. Smoke Test

Before deploying the non-Kaufmann jobs to a new Supabase environment, apply
`migrations/018_store_inventory_snapshots.sql` and
`migrations/019_store_product_lifecycle.sql`. Apply
`migrations/020_product_discovery_tracking.sql` before deploying catalogue-run
tracking and Kaufmann discovery.
The production database must also have
`supabase/migrations/20260902090029_compact_kaufmann_inventory_history.sql`
and
`supabase/migrations/20260902090721_retire_legacy_kaufmann_snapshots.sql`
before deploying the compact Kaufmann refresh code. Apply the compact store
inventory-history migration before deploying the updated non-Kaufmann refresh
and catalog-sync code.

```bash
mkdir -p logs
bash scripts/run_kaufmann_refresh.sh --dry-run --url https://www.kaufmann.dk/produkt/boss-orange-196321 --no-delay
bash scripts/run_kaufmann_refresh.sh --limit 1 --no-delay
.venv/bin/python scripts/sync_kaufmann_catalog.py --discover-only --preview 3
.venv/bin/python scripts/sync_kaufmann_catalog.py --dry-run --limit 3 --no-delay
bash scripts/run_store_refresh.sh --all --limit 1 --dry-run --no-delay
bash scripts/run_store_refresh.sh --all --limit 1 --no-delay
bash scripts/run_store_catalog_sync.sh --store cejf --limit 1 --dry-run --no-delay
```

The write test should log:

```text
Kaufmann refresh complete. refreshed_variants=...
```

## 6. Install Systemd Timer

```bash
sudo cp deployment/systemd/highlide-kaufmann-refresh.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-kaufmann-refresh.timer /etc/systemd/system/
sudo cp deployment/systemd/highlide-kaufmann-weekly-sweep.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-kaufmann-weekly-sweep.timer /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-refresh.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-refresh.timer /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now highlide-kaufmann-refresh.timer highlide-kaufmann-weekly-sweep.timer highlide-store-refresh.timer highlide-store-catalog-sync.timer
```

After the systemd unit files have been installed once, all continual scraping
timers can be controlled from the repository with:

```bash
./scripts/continual_scraping_on.sh
./scripts/continual_scraping_off.sh
```

The off script disables future scheduled runs but deliberately allows any
scraper service already in progress to finish safely.

Check timer status:

```bash
systemctl list-timers highlide-kaufmann-refresh.timer highlide-kaufmann-weekly-sweep.timer highlide-store-refresh.timer highlide-store-catalog-sync.timer
systemctl status highlide-kaufmann-refresh.timer
systemctl status highlide-kaufmann-weekly-sweep.timer
systemctl status highlide-store-refresh.timer
systemctl status highlide-store-catalog-sync.timer
```

Run manually:

```bash
sudo systemctl start highlide-kaufmann-refresh.service
sudo systemctl start highlide-kaufmann-weekly-sweep.service
sudo systemctl start highlide-store-refresh.service
sudo systemctl start highlide-store-catalog-sync.service
```

Read service status:

```bash
systemctl status highlide-kaufmann-refresh.service
systemctl status highlide-store-refresh.service
systemctl status highlide-store-catalog-sync.service
```

Tail logs:

```bash
tail -f /root/scraping_engine/logs/kaufmann_refresh.log
tail -f /root/scraping_engine/logs/store_refresh.log
tail -f /root/scraping_engine/logs/store_catalog_sync.log
journalctl -u highlide-kaufmann-refresh.service -f
journalctl -u highlide-store-refresh.service -f
journalctl -u highlide-store-catalog-sync.service -f
```

All inventory services use the same global `flock`, so they cannot run in parallel. The non-Kaufmann service waits for an active Kaufmann run to finish and then refreshes its stores sequentially. Both Kaufmann services wait up to 12 hours for the lock instead of skipping a busy slot. Lock exhaustion uses exit code 75 and systemd retries after 15 minutes. The 25-hour service timeout accommodates lock waiting and the existing refresh budget. A scraper partial/error result of 1 is not automatically restarted. The Kaufmann refresh reads the public variation endpoint and does not launch Playwright.

## 7. Cron Fallback

If you prefer cron:

```cron
15 2 * * 1-6 cd /root/scraping_engine && /usr/bin/flock --verbose -w 43200 -E 75 /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_kaufmann_refresh.sh >> logs/kaufmann_refresh.log 2>&1
15 2 * * 0 cd /root/scraping_engine && /usr/bin/flock --verbose -w 43200 -E 75 /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_kaufmann_weekly.sh >> logs/kaufmann_refresh.log 2>&1
15 8 * * * cd /root/scraping_engine && /usr/bin/flock -w 43200 /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_store_refresh.sh --all >> logs/store_refresh.log 2>&1
15 12 * * 0 cd /root/scraping_engine && /usr/bin/flock -w 86400 /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_store_catalog_sync.sh --all >> logs/store_catalog_sync.log 2>&1
```

## 8. Operational Notes

- The Python script has no internal weekly clock. Systemd supplies the schedule and command-line mode:

  | Schedule | Service | Arguments | Rows checked |
  | --- | --- | --- | --- |
  | Monday-Saturday | `highlide-kaufmann-refresh.service` | none | `publication_status = 'active'` |
  | Sunday | `highlide-kaufmann-weekly-sweep.service` | sitemap diff, then `--include-unavailable` | new Kaufmann pages, then every existing Kaufmann product |
  | Daily | `highlide-store-refresh.service` | `--all` | active rows in all eight non-Kaufmann tables |
  | Sunday | `highlide-store-catalog-sync.service` | `--all` | complete catalogs for all eight non-Kaufmann stores |

- `highlide-store-refresh.timer` starts at `08:15` daily. Its one Python process refreshes Rains, Rømerhus, SuitClub, CEJF, Skagen Clothing, Shoe Chapter, STOY, and LAKOR sequentially.

- `highlide-store-catalog-sync.timer` starts at `12:15` every Sunday. It runs the eight existing full importers sequentially, so newly listed products are inserted and existing products receive current broad catalog metadata.

- A listed product with no stock remains `publication_status = 'active'`; its dynamic availability is simply false. A product becomes `missing` only after it was absent from two consecutive successful weekly full imports. A seen product resets to active automatically, including a previously missing or unavailable product.

- Catalog reconciliation is skipped and the service exits unsuccessfully if fewer than 50% of the previously publishable rows were refreshed. This protects against a broken or temporarily truncated collection feed.

- Every successful non-Kaufmann product update submits one observation to `store_inventory_history`. The database extends the latest interval when prices and local inventory are unchanged, or inserts a new interval when that state changes. A same-day retry does not increase `observation_count`. Writes are batched in groups of 50.

- Store history contains prices, clean local inventory, total-stock summaries, availability summaries, and refresh status. It deliberately excludes webshop sizes, URLs, source identity, descriptions, images, materials, and other catalog metadata.

- `store_inventory_refresh_runs` records one compact audit row per sequential daily invocation, including selected stores, test limits/offsets, whether history was enabled, counts, status, and write failures.

- Active products refresh Monday through Saturday. Sunday's sweep also rechecks soft-tombstoned products so a product can become active again.
- The two Kaufmann timers start at `02:15`; the other-store inventory timer starts at `08:15`; and the weekly catalog timer starts Sunday at `12:15`. Randomized delays and the shared lock prevent simultaneous scraper workloads.
- Set the droplet timezone with `sudo timedatectl set-timezone Europe/Copenhagen` if you want the timer interpreted as Copenhagen time.
- `kaufmann_products` remains the current/live table.
- `kaufmann_inventory_history` stores one interval per consecutive Kaufmann
  dynamic state and extends unchanged intervals.
- `kaufmann_inventory_refresh_runs` records one row per refresh invocation.
- The refresh does not rewrite stable catalog fields or `raw`.
- `publication_status = 'active'` is the publishable state. `unavailable` rows are retained as soft tombstones rather than deleted.
- Kaufmann's Sunday job runs the lightweight all-row variation sweep first. In addition to lifecycle updates, that sweep records known pages whose response contains an unknown colour ID. The catalogue step then full-imports only those affected pages plus URLs newly found in the sitemap. This detects new colours on old URLs without a second all-page pass or a weekly Playwright import of every known page.
- Every successful full catalogue job writes a summary to `catalog_sync_runs`, including seen, new, reactivated, absent, confirmed-missing, and failed counts. Rows from one eight-store invocation share a `batch_id`.
- Every product table already has an immutable `first_seen_at` timestamp. New-product filters should use this field; ordinary catalogue and inventory upserts do not rewrite it.

Useful history query:

```sql
select
  observed_from,
  observed_through,
  observation_count,
  refresh_status,
  current_price,
  list_price,
  local_total_stock,
  aarhus_total_stock,
  aarhus_available
from public.store_inventory_history
where store = 'stoy'
  and product_id = 1
order by observed_through desc;
```
## Resumable inventory batches (opt-in; not deployed)

`scripts/run_inventory_batches.py` provides one durable refresh cycle for the
twelve stores registered in `STORE_SPECS`: the eight original Shopify stores,
AXEL, qUINT, Salling and Magasin. Kaufmann and Weekday still use their existing
runners. This is a single-host step toward the shared scheduler, not the
continuous service or multi-droplet queue yet.

From the repository root, a bounded pilot after deployment is:

```bash
.venv/bin/python scripts/run_inventory_batches.py \
  --store stoy --store skagen_clothing \
  --checkpoint logs/inventory-cycle.json \
  --batch-pages 20 --max-batches 2
```

This command **writes product inventory, history and run records**. It is not
a dry run. Repeat the same command to resume. Remove `--max-batches 2` to process
all currently due work; the checkpoint configuration otherwise stays the same.
Each batch contains up to 20 distinct pages/styles, with all selected sibling
colours kept together. A large style can therefore contain more than 20 rows.
The runner rotates between selected stores after each batch. It retains the
existing active, Aarhus-available selection scope; restock checks are still a
separate concern.

The checkpoint freezes selected row identities, so updates to availability or
sort order cannot make offsets skip work. Successful writes remove those row
IDs from the pending work. Failed rows are deferred for one hour by default;
each batch is attempted at most once per invocation. Rate limiting defers all
remaining batches for that store for at least the configured retry interval and
the source Retry-After value, or 24 hours when that value is unavailable. Other due batches can continue. The runner
exits rather than sleeping when no work is due. Exit 0 means the cycle is
complete; exit 1 also covers an intentionally bounded invocation or deferred
work, so it does not necessarily mean a database failure.

Once all pending work completes, the same command with `--new-cycle` takes a
fresh snapshot. It refuses to replace an unfinished cycle. Repeating a completed
checkpoint without that flag does no further work.

Before database writes, the runner durably saves validated patches, their exact
history observations and the run UUID. It advances progress only after products,
history and the final run record succeed. Restarting replays an unfinished
journal before fetching more source data. Consecutive identical observations are
merged by the existing history trigger, retaining the original UTC timestamps.
These writes are recoverable but **not one database transaction**: readers can
temporarily see product updates before history catches up.

One complete cycle has one row in `store_inventory_refresh_runs`, using the
same ID across batches and restarts. It lists all selected stores, has a null
`row_limit` and zero `row_offset`, and updates aggregate counters after each
committed batch. `stores_completed` counts fully drained store queues. It stays
running with no finish time while work remains, including deferred retries.
Once complete it is succeeded, or partial if any batch recorded failures or a
database write failed. Database failures retain the recovery journal and mark
the cycle failed with `write_failed=true` when the run table is reachable.
Counters measure attempts/observations, so failures retried successfully remain
visible. The local checkpoint holds per-batch results, errors, retry times and
the active batch; source parsers log failing URLs. There is no periodic heartbeat
while a batch is fetching or automatic stale-run reconciliation while offline.

This preserves Backstage's whole-run overview: its current server excludes
limited runs from scheduled-refresh health. Its existing 12-hour unfinished-run
alert still applies, and elapsed cycle duration includes pauses between retries.
Backstage's store list currently needs AXEL, qUINT, Salling, Magasin and Weekday
added separately. No dashboard files were changed here.

The checkpoint format is version 2. A pre-deployment version-1 checkpoint is not
silently converted: it used per-batch database run IDs. If one contains pending
observations, finish or explicitly reconcile those before switching formats.

Daily refreshes select colours with known Aarhus availability and check all
their sizes and prices, including sizes currently sold out. Magasin and Salling
receive that explicit colour selection, so they avoid additional requests for
unselected sibling colours. Full catalogue imports still enumerate all colours.
AXEL/qUINT now use the stored parent ID to request public variation JSON directly
for daily refreshes, like Kaufmann: one source request per known style, with no
product-page or Chromium request. The source's formatted DKK price must agree
with its numeric price; missing currency/identity/stock data fails validation.
Chromium remains available for full imports. The variation response
already includes sibling colours, so receiving them adds no extra request;
only the selected colour rows are parsed and written during refresh.

Kaufmann's empty variation response (`hasVariants=false`, `totalOptions=0`, empty
colour list) now triggers a product-page confirmation. Only a confirmed page
404/410 marks the existing rows unavailable; a live or malformed page retains
the existing parse-error behaviour. The observed removal case was verified in
dry-run mode, without changing live inventory.

Operational requirements:

- Use the existing virtual environment, Supabase secret credentials and source
  dependencies. No new Python packages or database migration are required.
  Offline tests ran on Python 3.9.6; droplet runtime verification is pending.
- The runner holds `/tmp/highlide-inventory-refresh.lock` itself. Do not wrap it
  in another `flock` on that path. An occupied lock causes immediate exit.
- Disable other inventory/catalog writers for the selected stores during a
  cycle. The lock coordinates only cooperating processes on one host. It does
  not coordinate local imports with a droplet or multiple droplets.
- Keep the checkpoint on persistent disk, outside Git. It is saved with mode
  0600 and contains product data but no credentials. Do not delete it with an
  unfinished journal. Loss of that file also loses recovery information.
- If a product was deleted or has a newer `updated_at` than a journal patch,
  recovery stops before overwriting it. Investigate the competing writer and
  reconcile the saved observations; do not simply discard the checkpoint.
- Existing request timeouts still apply. Batch size bounds pages/styles, not
  elapsed time. Service-level watchdogs, automated cycle scheduling, Kaufmann /
  Weekday integration and database-backed worker ownership remain follow-ups.
- Source access from the actual droplet, particularly Magasin, still needs a
  limited live pilot. This runner does not resolve source blocking.

Validation:

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python scripts/run_inventory_batches.py --help
```
