# DigitalOcean Deployment

This guide deploys HIGHLIDE's sequential inventory refresh jobs on a Linux droplet.

The checked-in systemd units match the current droplet path:

```text
/root/scraping_engine
```

## 1. Install System Packages

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
KAUFMANN_INVENTORY_SNAPSHOTS_TABLE=kaufmann_inventory_snapshots
```

Use the server-side Supabase secret key. Do not use a frontend publishable key for this job.

## 5. Smoke Test

Before deploying the non-Kaufmann jobs to a new Supabase environment, apply
`migrations/018_store_inventory_snapshots.sql` and
`migrations/019_store_product_lifecycle.sql`. Apply
`migrations/020_product_discovery_tracking.sql` before deploying catalogue-run
tracking and Kaufmann discovery.

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

All inventory services use the same global `flock`, so they cannot run in parallel. The non-Kaufmann service waits for an active Kaufmann run to finish and then refreshes its stores sequentially. The Kaufmann refresh reads the public variation endpoint and does not launch Playwright.

## 7. Cron Fallback

If you prefer cron:

```cron
15 2 * * 1-6 cd /root/scraping_engine && /usr/bin/flock -n /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_kaufmann_refresh.sh >> logs/kaufmann_refresh.log 2>&1
15 2 * * 0 cd /root/scraping_engine && /usr/bin/flock -n /tmp/highlide-inventory-refresh.lock /bin/bash scripts/run_kaufmann_weekly.sh >> logs/kaufmann_refresh.log 2>&1
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

- Every successful non-Kaufmann product update also writes a lean row to `store_inventory_snapshots`. The unique key is `store + product_id + checked_bucket`, so retries replace that product's observation for the same UTC day instead of adding duplicates. Snapshot writes are batched in groups of 50.

- The snapshot table stores only dynamic analysis fields: prices, webshop sizes, clean local inventory, total-stock summaries, availability summaries, the source identity, and refresh status. It does not duplicate descriptions, images, materials, or other catalog metadata.

- With the current 2,266 non-Kaufmann rows, a complete daily run produces at most 2,266 snapshot rows, or about 827,090 rows per year. Add a retention or archival policy later if multi-year JSON history becomes larger than the analysis requires.

- Active products refresh Monday through Saturday. Sunday's sweep also rechecks soft-tombstoned products so a product can become active again.
- The two Kaufmann timers start at `02:15`; the other-store inventory timer starts at `08:15`; and the weekly catalog timer starts Sunday at `12:15`. Randomized delays and the shared lock prevent simultaneous scraper workloads.
- Set the droplet timezone with `sudo timedatectl set-timezone Europe/Copenhagen` if you want the timer interpreted as Copenhagen time.
- `kaufmann_products` remains the current/live table.
- `kaufmann_inventory_snapshots` receives one row per Kaufmann variant per UTC day.
- The refresh does not rewrite stable catalog fields or `raw`.
- `publication_status = 'active'` is the publishable state. `unavailable` rows are retained as soft tombstones rather than deleted.
- Kaufmann's Sunday job runs the lightweight all-row variation sweep first. In addition to lifecycle updates, that sweep records known pages whose response contains an unknown colour ID. The catalogue step then full-imports only those affected pages plus URLs newly found in the sitemap. This detects new colours on old URLs without a second all-page pass or a weekly Playwright import of every known page.
- Every successful full catalogue job writes a summary to `catalog_sync_runs`, including seen, new, reactivated, absent, confirmed-missing, and failed counts. Rows from one eight-store invocation share a `batch_id`.
- Every product table already has an immutable `first_seen_at` timestamp. New-product filters should use this field; ordinary catalogue and inventory upserts do not rewrite it.

Useful snapshot query:

```sql
select
  checked_bucket,
  checked_at,
  refresh_status,
  current_price,
  list_price,
  local_total_stock,
  aarhus_total_stock,
  aarhus_available
from public.store_inventory_snapshots
where store = 'stoy'
  and product_id = 1
order by checked_at desc;
```
