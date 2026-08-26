# DigitalOcean Deployment

This guide deploys Kaufmann's lightweight HTTP inventory and lifecycle refresh on a Linux droplet.

Assumed server path:

```text
/opt/highlide/scraping_engine
```

## 1. Install System Packages

Ubuntu/Debian:

```bash
sudo apt-get update
sudo apt-get install -y git python3 python3-venv python3-pip
```

## 2. Put The Project On The Server

Clone or copy the repository to:

```bash
sudo mkdir -p /opt/highlide
sudo chown -R "$USER":"$USER" /opt/highlide
cd /opt/highlide
git clone <your-repo-url> scraping_engine
cd scraping_engine
```

If the repo is already there:

```bash
cd /opt/highlide/scraping_engine
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

Create `/opt/highlide/scraping_engine/.env` from `.env.example`:

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

```bash
mkdir -p logs
bash scripts/run_kaufmann_refresh.sh --dry-run --url https://www.kaufmann.dk/produkt/boss-orange-196321 --no-delay
bash scripts/run_kaufmann_refresh.sh --limit 1 --no-delay
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
sudo systemctl daemon-reload
sudo systemctl enable --now highlide-kaufmann-refresh.timer highlide-kaufmann-weekly-sweep.timer
```

Check timer status:

```bash
systemctl list-timers highlide-kaufmann-refresh.timer highlide-kaufmann-weekly-sweep.timer
systemctl status highlide-kaufmann-refresh.timer
systemctl status highlide-kaufmann-weekly-sweep.timer
```

Run manually:

```bash
sudo systemctl start highlide-kaufmann-refresh.service
sudo systemctl start highlide-kaufmann-weekly-sweep.service
```

Read service status:

```bash
systemctl status highlide-kaufmann-refresh.service
```

Tail logs:

```bash
tail -f /opt/highlide/scraping_engine/logs/kaufmann_refresh.log
journalctl -u highlide-kaufmann-refresh.service -f
```

Both services use the same `flock`, so they cannot run in parallel. The refresh reads Kaufmann's public variation endpoint and does not launch Playwright. The `12h` timeout is a conservative safety ceiling.

## 7. Cron Fallback

If you prefer cron:

```cron
15 2 * * 1-6 cd /opt/highlide/scraping_engine && /usr/bin/flock -n /tmp/highlide-kaufmann-refresh.lock /bin/bash scripts/run_kaufmann_refresh.sh >> logs/kaufmann_refresh.log 2>&1
15 2 * * 0 cd /opt/highlide/scraping_engine && /usr/bin/flock -n /tmp/highlide-kaufmann-refresh.lock /bin/bash scripts/run_kaufmann_refresh.sh --include-unavailable >> logs/kaufmann_refresh.log 2>&1
```

## 8. Operational Notes

- Active products refresh Monday through Saturday. Sunday's sweep also rechecks soft-tombstoned products so a product can become active again.
- Both timers start at `02:15` in the server's local timezone, with up to `15m` randomized delay.
- Set the droplet timezone with `sudo timedatectl set-timezone Europe/Copenhagen` if you want the timer interpreted as Copenhagen time.
- `kaufmann_products` remains the current/live table.
- `kaufmann_inventory_snapshots` receives one row per Kaufmann variant per UTC day.
- The refresh does not rewrite stable catalog fields or `raw`.
- `publication_status = 'active'` is the publishable state. `unavailable` rows are retained as soft tombstones rather than deleted.
