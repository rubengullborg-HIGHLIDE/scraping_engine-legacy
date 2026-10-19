# Inventory refresh validation — 19 October 2026

Implemented locally; nothing deployed. No production products, history or run
records were written by these checks. Source reads used current public stores;
database reads used the connected production project. History replay was tested
using temporary objects and rolled back.

## Behaviour and dashboard compatibility

- A batch defaults to 20 distinct pages/styles. Selected sibling colours stay
  together. The worker switches stores after the batch commits; there is no
  fixed elapsed-time switch. A single large style can exceed a useful time budget.
- A whole refresh cycle now has one `store_inventory_refresh_runs` ID across
  batches and restarts. Aggregate counters update after each committed batch.
  `row_limit` is null, `row_offset` is zero, and completion requires all pending
  work to finish. Per-batch diagnostics stay in the checkpoint.
- Recovery means restarting interrupted work. Saved product/history observations
  are replayed before fetching new work. A product with a newer `updated_at`
  blocks replay, preventing an old saved observation from overwriting newer data.
- The batch runner remains opt-in: it is a separate command, and existing systemd
  services have not been rewired. Automatic cycle scheduling, Kaufmann/Weekday
  integration and parallel ownership are still outstanding.
- Daily selection remains active colours known to have Aarhus availability.
  Every size and its dynamic price/availability is checked for selected colours,
  including currently unavailable sizes. Magasin/Salling no longer traverse
  unselected sibling colours. Full imports still enumerate the full family.
- AXEL/qUINT daily refreshes use their stored parent IDs and one variation JSON
  request per style, like Kaufmann. The source DKK price label must match the
  numeric price. Existing strict local-stock validation remains in place.

Read-only inspection of `HIGHLIDE-backstage/server/scrapers.mjs` confirmed that
limited runs are excluded from scheduled-refresh health. Its actual normalizer
and report builder accepted a generated completed cycle as one full-scope run,
with aggregate counters and health entries for both selected supported stores.
Its seven scraper-report tests also passed. No Backstage files were changed.

Before continuous operation, Backstage still needs:

- AXEL, qUINT, Salling, Magasin and Weekday added to its store-health/filter list.
- Progress-aware unfinished-run alerts: it currently warns after 12 hours from
  start and does not read `updated_at`. A resumed cycle can span pauses, so its
  elapsed duration is not active worker time.

## Local source measurements

Samples span small, middle and large groups by existing size count; the
department-store samples deliberately include the largest group. They are
coverage/stress samples, not a statistically representative runtime average.
Three-second source pacing was retained. Times below exclude production writes
and initial database selection. Response sizes are decoded body bytes, not
compressed network traffic.

| Final refresh path | Styles | Selected colours | Existing size entries | Source calls | Elapsed | Staged product/history pairs |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| AXEL, direct variation JSON | 20 | 30 | 139 | 20 | 57.56 s | 30 |
| qUINT, direct variation JSON | 20 | 27 | 238 | 20 | 59.21 s | 27 |
| Salling, selected colours | 3 | 9 | 225 | 214 | 640.28 s | 9 |
| Magasin, selected colours | 3 | 7 | 118 | 125 | 372.75 s | 7 |

All four samples completed with zero source failures or skipped rows. All 73
product/history pairs matched on product ID, observation timestamp, current and
list prices, clean local inventory, availability and Aarhus stock total.
History intentionally excludes `webshop_sizes`, including per-size webshop
prices; these checks do not claim a new size-price history schema.

The intermediate AXEL/qUINT implementation fetched HTML plus variation JSON.
The same samples took 117.61/119.63 seconds and 40 calls each. Using stored parent
IDs reduced calls to 20 and approximately halved paced elapsed time. Decoded
response bodies fell from 13,061,217 to 231,544 bytes for AXEL and from 16,964,293
to 3,236,706 bytes for qUINT. These comparisons are against the intermediate
two-request HTTP path, not a full browser-run benchmark.

Separate live browser/direct-HTTP comparisons matched parent/colour identity,
prices, webshop sizes, local inventory, Aarhus availability/totals and publication
status for one AXEL page and one two-colour qUINT page. Direct requests took
0.35/0.73 seconds; browser reads took 0.85/1.17 seconds. These are spot checks,
not resource profiling or a guarantee of droplet latency.

## Kaufmann edge case found and fixed

A 20-page dry run took 74.41 seconds including initial database selection and
logging, with three-second inter-page delays. Nineteen pages parsed; the
`allan-clark-197807` variation endpoint returned HTTP 200 with no colours.
An independent read reproduced that payload and confirmed its product page
returned HTTP 404.

The client now confirms that specific empty-variation response against the
product page. Only page HTTP 404/410 produces an unavailable observation;
an empty response with a live/malformed page still fails safely. The affected
page was rerun in dry-run mode: five existing colour rows would be marked
unavailable, and `failed_pages` was zero. The complete 20-page sample was not
repeated after this isolated fix.

## History and automated verification

On PostgreSQL 17.6, a copy of the installed history trigger was exercised in a
temporary table. Five inserts covered an original observation, its replay, a
price change, its replay and the following day's unchanged observation. The
result was two history states with observation counts 1 and 2. Replays did not
create duplicate states or increment daily counts. The transaction was rolled
back. Reproduction SQL is in `tests/store_history_replay.sql`.

The Python suite passed all 254 tests on local Python 3.9.6. Backstage checks used
Node 26.12.0. Droplet runtime, source access, REST write latency and end-to-end
production writes still require a deployment pilot. The current local Python
environment emits urllib3's existing LibreSSL compatibility warning.

## Capacity implications and next step

At measurement time the database held:

| Daily scope | Styles | Colours | Size entries |
| --- | ---: | ---: | ---: |
| AXEL | 527 | 636 |  — |
| qUINT | 143 | 167 | — |
| Salling | 1,302 | 1,779 | 14,853 |
| Magasin | 1,514 | 1,961 | 12,537 |

Salling's sample included one style with seven selected colours and 217 size
entries. Magasin's 125 calls corresponded to seven colour-detail calls plus 118
size-stock calls. Other products can require additional size-price requests.

A simple planning scenario—one call per Salling size and one colour call plus
one stock call per Magasin size—uses roughly 12.4 and 12.1 hours respectively at
three-second pacing, before other stores, retries, database writes or catalogue
work. This is not a full-cycle timing prediction: cache reuse, source changes
and price ranges alter the actual request count.

One serial worker therefore has little reliable daily capacity margin. The next
step should bound batches by source work as well as style count, then support
independently paced store workers. A single large style may need smaller colour
work units to yield sooner. This does not require skipping sold-out sizes.

Compare two workers on the existing droplet against separate droplets only
after measuring its CPU, memory and source access. Two workers on one machine
avoid another host but share resources; separate droplets isolate workloads
and add operational cost. Both require ownership/locking changes: the current
runner holds one global host lock and must not be launched as a multi-worker
system as-is. Magasin access from the actual droplet remains unverified here.

## STOY droplet smoke-test follow-up

The Cow Dancer Mesh Tee product reproduced the reported mismatch locally:
Shopify returned S, M, L, XTRA LARGE, while both physical-store drawers returned
S, M, L, XL. All four sizes were present; the completeness guard was comparing
two source spellings for the same size. The parser now maps this verified
Shopify alias to XL for validation and webshop-size output. Missing-size checks
remain strict, and duplicate sizes introduced by normalization are rejected.
The captured live payload parsed at DKK 690 with M/L available in Aarhus and
S/XL unavailable. This is a point-in-time source observation, not a database
write. The full offline suite passed 269 tests after the fix.

## Unified Kaufmann and Weekday batches

Both inventory and catalog batch registries now contain 14 stores. The shared
inventory service runs one bounded batch per tick; the catalog service keeps
its existing per-store tick logic with specialized Kaufmann/Weekday workers.
Legacy Kaufmann timers are disabled by the enable script.

Validation: 280 offline tests passed, including shared Kaufmann history
routing, first-missing-colour behavior without false stock history, Weekday
partial-observation retention and timestamp-based replay protection, shared
registries, and special catalog journal recovery without repeating source
checks/counters. All 71 Python source files compiled in memory; shell syntax
checks passed. Native systemd execution was not available on this macOS host.
The Weekday catalog-run constraint change accepted Weekday and Kaufmann records
on a temporary clone of the production run table, then rolled back. No live
schema or product/history/run data was changed.

Deployment prerequisites remain: commit/push code, apply
`20261019133636_weekday_catalog_batch_runs.sql` and
`20261019134922_unify_inventory_observations.sql`, install both updated shared
service/timer pairs, and run bounded droplet pilots. Backstage still needs its
Kaufmann health lookup switched to the shared daily cycle records, and its
store registry updated. The new runners use shared inventory history and do not create duplicate
per-batch or separate Kaufmann daily operational records.

### History unification validation

The unification migration passed against temporary copies on the connected
PostgreSQL database, ending in ROLLBACK. Twenty existing Kaufmann intervals
were copied and compared across all retained fields, timestamps and counts.
Replaying the latest identical observation created no duplicate interval and
left the observation count unchanged. The compatibility view rejected write
access for service_role. Weekday accepted exact replay and rejected stale and
incomplete updates. Live tables, schemas and permissions were not changed.
The full migration has not been applied; its full-volume duration is unmeasured.
The offline suite remains 280 passing tests.
