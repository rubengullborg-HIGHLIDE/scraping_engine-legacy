# DigitalOcean Deployment

This guide deploys HIGHLIDE's resumable daily inventory and weekly catalog cycles on a Linux droplet. The October 19 unified-store follow-up below supersedes the legacy separate Kaufmann schedules.

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
Twenty-five consecutive server/payload-error products stop the store as a probable
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
sudo cp deployment/systemd/highlide-store-refresh.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-refresh.timer /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
bash scripts/continual_scraping_on.sh
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
## Resumable inventory batches (manual pilot and scheduled modes)

`scripts/run_inventory_batches.py` provides one durable refresh cycle for the
fourteen stores: the eight original Shopify stores, AXEL, qUINT, Salling,
Magasin, Kaufmann and Weekday. Store-specific adapters retain their observation
rules and history destinations. This remains a single-host scheduler, not a
multi-droplet queue.

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

## Resumable weekly catalog service (October 19 follow-up)

The checked-in `highlide-store-catalog-sync.service` now invokes
`scripts/run_catalog_batches.py` directly. **Do not wrap this runner in flock**:
its coordinator lock protects its checkpoint, and it acquires the shared
inventory lock separately for each store batch. The legacy
`run_store_catalog_sync.sh --all` command still performs an uninterrupted full
pass; it is retained for manual use, not used by the updated service.

Each catalog invocation processes at most one batch of 10 discovery entries per due
store, rotating the starting store across interruptions. Importers can follow
linked colour families, so 10 entries is not a strict product/request cap.
The inventory lock is released between batches, with a five-second handoff gap.
If another job owns it, the runner exits successfully and resumes on the next
timer tick instead of waiting for hours. The timer runs five minutes after boot
and 15 minutes after the service becomes inactive. A fresh catalog cycle starts
only after the previous cycle finishes and seven days have elapsed since its
start. It is no longer anchored to Sunday. Kaufmann and Weekday now participate through their own catalog adapters. The old
Kaufmann timers must stay disabled.

Discovery order and collection metadata are frozen in manifests beside
`logs/catalog-cycle.json`. Price/inventory data are fetched afresh for each
batch, including CEJF and SuitClub's collection-feed products. A successful
batch advances the saved offset; an interrupted or failed batch repeats from
its beginning. Successfully committed rows remain saved. Failed sources are
conservatively deferred 24 hours because importer exit codes do not identify
Retry-After values. Other stores continue. The default per-batch subprocess
timeout is one hour. Do not delete a checkpoint to bypass a failure.

One `catalog_sync_runs` record per store and cycle is reused across batches.
Its details show discovery-page progress and retry times. Only successful
imports under the shared lock contribute catalog observations; intervening
inventory refreshes do not count as catalog discovery. Absence reconciliation
runs only after all discovery entries for that store succeeded, and retains the
existing coverage threshold and two-pass missing rule. Newly updated unseen
rows are conservatively excluded from absence marking. Reconciliation is saved
as a journal before writes; recovery replays the same patches/history. A newer
product observation blocks stale journal replay and needs operator review.
Product and history writes are recoverable, not one database transaction.

Deployment from the existing droplet checkout, after committing/pushing this
follow-up and with timers paused:

```bash
# Explicitly interrupt the old full catalog invocation and waiting refresh jobs.
sudo systemctl stop highlide-kaufmann-refresh.service highlide-store-refresh.service highlide-store-catalog-sync.service
# Verify no other manual jobs are running before updating files.
git pull --ff-only && .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
.venv/bin/python scripts/run_catalog_batches.py --help
sudo cp deployment/systemd/highlide-store-catalog-sync.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Run the pending read-only source smoke test before enabling timers:

```bash
flock --verbose -n -E 75 /tmp/highlide-inventory-refresh.lock \
  bash scripts/run_store_refresh.sh --store stoy --store skagen_clothing --limit 3 --dry-run
```

Then explicitly start the catalog service for a first **writing** tick and
inspect `logs/store_catalog_sync.log` plus the saved checkpoint. This executes
one batch per configured store, not an entire weekly pass:

```bash
sudo systemctl start --no-block highlide-store-catalog-sync.service
tail -n 40 logs/store_catalog_sync.log
```

After verifying that tick and the inventory pilot, restore schedules with
`bash scripts/continual_scraping_on.sh`. The old interrupted run's database
record is not automatically repaired; inspect it separately. Its already saved
products will be refreshed again by the new cycle. Keep checkpoint files and
manifests together when backing up or moving the worker.

Local validation uses Python 3.9.6; the prior droplet output showed Python 3.12.
This runner uses POSIX file locks/process groups and is intended for Linux
systemd deployment. It adds no dependencies. The unified-store follow-up requires the Weekday
catalog-run constraint migration described below. Native
systemd execution and production writes must be verified on the droplet.

## Unified Kaufmann/Weekday follow-up (required before enabling new services)

Both batch runners now select all 14 stores by default, including Magasin.
Kaufmann and Weekday both write to `store_inventory_history` after the
unification migration. Existing Kaufmann intervals are copied with their counts
and lifecycle fields intact; the original table is retained as a read-only
archive. The old table name becomes a read-only compatibility view over the
shared history (history row IDs are reassigned). Operational daily cycles use one shared
`store_inventory_refresh_runs` row for all selected stores, including Kaufmann.
They no longer create separate Kaufmann daily operational rows. Backstage must
read Kaufmann health from the shared cycle record and add Weekday/new stores to
its store list; do not interpret the last legacy Kaufmann run as current health.
No Backstage code has been changed here.

Kaufmann's daily adapter calls the existing HTTP variation refresh engine and
retains its two-observation missing-colour confirmation rule. A first missing
colour updates the confirmation counter without fabricating an inventory
history observation. Its weekly adapter freezes the union of the current
sitemap and known pages, sweeps existing rows including unavailable variants,
and browser-hydrates only new products or newly discovered available colours.
There is no second generic catalog-absence tombstone pass.

Weekday uses `inventory_checked_at`, `scraped_at`, and `updated_at`. Price and
inventory are persisted together only after both succeed; partial responses
retain the previous observation and leave the job pending. Local JSON proof
files may still contain separate source-request timestamps; database rows do not. Blocked sources defer all
remaining daily Weekday batches for 24 hours. The weekly cycle checks known
unavailable colours as well as newly discovered and linked colours. New rows
must pass existing positive-Aarhus, exact-inventory and image requirements.
Absence from the online listing never marks physical Weekday stock unavailable.

Special catalog batches save patches, history and new rows before writes. A
completed batch result is replayed without repeating missing-colour counters.
Interrupted writes resume from that journal. Newer observations block stale
replay instead of being overwritten; such a conflict needs operator review.
Keep both per-batch journals and manifests alongside the cycle checkpoint.
Do not run an independent full import for the same store concurrently.

The shared inventory timer now resumes **one batch** every 30 seconds after its
service exits. It returns without waiting when the lock is busy. After 24 hours since cycle start, the next invocation starts a new cycle,
carrying unfinished rows and retry deadlines forward. The shared catalog
timer retains its 15-minute resume interval and seven-day cycle interval.
`continual_scraping_on.sh` disables the two legacy Kaufmann timers before
starting the two shared timers. Their old service files remain available for
explicit diagnostics; do not start them alongside a shared cycle.

Before deploying, stop all scraper writers and apply both reviewed migrations
through the normal Supabase migration workflow:

- `supabase/migrations/20261019133636_weekday_catalog_batch_runs.sql`
- `supabase/migrations/20261019134922_unify_inventory_observations.sql`

The second migration requires PostgreSQL 15+ and was tested on temporary copies
in PostgreSQL 17.6. It locks affected tables, preserves each Kaufmann interval
with an exact-value copy check, and rolls back on mismatch. It removes Weekday's
two separate timestamps after retaining the earlier one as the common timestamp.
The distinction between those old timestamps is lost. Take a database backup
before applying. Neither migration has been applied to live tables by this work.

Use fresh cycle checkpoint paths for this schema version (daily version 3,
catalog version 2); old checkpoints are rejected. Preserve old checkpoints and
journals for inspection rather than deleting them. Do not replay old Weekday
payloads or dedicated Kaufmann history payloads after migration. Kaufmann's
standalone history configuration now uses `STORE_INVENTORY_HISTORY_TABLE`.
The compatibility view is read-only: old history writers must remain stopped.
A code-only rollback is incompatible with the new Weekday schema; restore the
pre-migration database backup together with the matching code if rollback is
needed. The archive is a frozen backup, not a second current history source.

With all old jobs stopped and changes committed/pushed:

```bash
cd /root/scraping_engine
bash scripts/continual_scraping_off.sh
sudo systemctl stop highlide-kaufmann-refresh.service highlide-kaufmann-weekly-sweep.service highlide-store-refresh.service highlide-store-catalog-sync.service
git pull --ff-only && .venv/bin/python -m unittest discover -s tests -p 'test_*.py'
sudo cp deployment/systemd/highlide-store-refresh.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-refresh.timer /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.service /etc/systemd/system/
sudo cp deployment/systemd/highlide-store-catalog-sync.timer /etc/systemd/system/
sudo systemctl daemon-reload
```

Repeat the read-only STOY/Skagen pilot, then bounded writing pilots before
restoring timers. To exercise one inventory batch of each new adapter:

```bash
.venv/bin/python scripts/run_inventory_batches.py --store kaufmann --store weekday \
  --checkpoint logs/kaufmann-weekday-pilot.json --batch-pages 2 --max-batches 2
```

This writes inventory/history; exit 1 can mean unfinished work. Complete the
pilot checkpoint before enabling another inventory cycle for these stores.
For one small catalog batch per new store, after the migration is applied:

```bash
.venv/bin/python scripts/run_catalog_batches.py --store kaufmann --store weekday \
  --checkpoint logs/kaufmann-weekday-catalog-pilot.json --batch-pages 2
```

This also writes products, history and run records. Do not start a second catalog
cycle for those stores while this pilot is unfinished: continue its exact
configuration, or review and explicitly retire the pilot before the all-store
service starts. Frozen store lists/batch sizes cannot be changed mid-cycle.
Once pilots and run/history records have been checked, enable the shared timers
with `bash scripts/continual_scraping_on.sh`.

## October 20: Sunday starts and catalog lock contention

The catalog service now passes `--scheduled`. New scheduled cycles begin only
on Sundays at or after 12:15 UTC (the former timer time on the UTC droplet).
Unfinished cycles retain their cycle ID, manifests, offsets and retries and
resume on any day. On completion, legacy seven-day due timestamps are aligned
to the Sunday of that due week. The 15-minute resume timer stays in place.
A fresh installation on a weekday waits for Sunday; manual CLI execution
without `--scheduled` still permits an initial out-of-schedule cycle.
Do not delete the current checkpoint or restart discovery to apply this change.

If inventory owns the shared lock, catalog now waits for its release instead
of skipping the next 15 minutes. This addresses the observed repeated lock-busy
exits. Catalog still releases the lock between stores and sleeps five seconds;
inventory continues to skip busy locks safely. A waiting catalog process is
normal and does not mean it is making product requests. The service's existing
14-hour limit bounds the invocation; no additional lock file is introduced.

Deployment: with code committed/pushed, wait for the catalog service to be
inactive (or stop it cleanly), pull the code, run the offline tests, copy the
updated catalog service file, and run `sudo systemctl daemon-reload`. Keep
existing checkpoints and timer state. An already running Python process keeps
its loaded code until its invocation ends; do not expect hot reload.

Read-only October 20 diagnosis: Magasin returns a Cloudflare challenge from
both Requests and ordinary Chromium on 46.101.137.153; the same listing works
locally. Ordinary curl 8.5.0 IPv4 HTTP/1.1 and HTTP/2 also return a
Cloudflare challenge. The droplet has no default IPv6 route, and curl IPv6
hostname resolution fails. No proxy environment variables were configured in
the diagnostic SSH session. No proxy was tested and no retailer contacted.
The approved-access draft remains on hold at the user’s request.
Skagen's failing variant 55575926997368 publishes Aarhus stock -1; the current
parser correctly retains saved inventory rather than accepting this invalid
quantity. Its exact business meaning has not been verified with the retailer.

Daily rollover fix (local code; deploy before relying on this behavior): after
24 hours the scheduled runner closes the old cycle as partial if work remains,
archives it under `<checkpoint>.cycles/<cycle_id>.json`, and creates a fresh
cycle containing pending rows plus currently eligible products. Pending rows
come first, duplicates are removed, and store cooldowns survive rollover.
Prepared replacement UUIDs are checkpointed before closing the old database
run, so a lost response can be retried. Any pending write journal must commit
first; database failures or stale-write conflicts still stop rollover for
operator investigation. Version 3 checkpoints remain compatible. Keep these
archives with checkpoints; they contain product data and have mode 0600.
No database migration or new dependency is required for rollover.
