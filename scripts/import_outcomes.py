"""Per-product outcome policy shared by every catalogue importer.

A weekly catalogue batch must not fail because one product in it failed:

- A product page answering 404/410 is gone. Skip it; reconciliation counts it
  as not seen, which is correct.
- Any other per-product error is recorded and skipped. The importer then exits
  with ``EXIT_PARTIAL`` so the runner advances, but does not advance catalogue
  misses for that store this cycle (the sweep is not a complete observation).
- A store rate limit (``StoreRateLimited`` or any HTTP 429) still stops the
  whole batch so it is retried later.
- If every product in the batch failed, the failure is probably systemic
  (site down, markup change everywhere), so exit ``EXIT_FAILED`` and retry.
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from scrapers.source_http import StoreRateLimited

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_PARTIAL = 3

GONE_STATUSES = {404, 410}


def is_gone(exc: BaseException) -> bool:
    """True for a confirmed 404/410 from the source."""
    if getattr(exc, "gone", False):
        return True
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in GONE_STATUSES


def is_rate_limited(exc: BaseException) -> bool:
    return getattr(getattr(exc, "response", None), "status_code", None) == 429


class ItemOutcomes:
    def __init__(self, store: str, log: logging.Logger):
        self.store = store
        self.log = log
        self.succeeded = 0
        self.gone: list[str] = []
        self.failed: list[str] = []

    @contextmanager
    def item(self, key: str) -> Iterator[None]:
        """Run one product's hydration; swallow and record per-item failures."""
        try:
            yield
        except StoreRateLimited:
            raise
        except Exception as exc:
            if is_rate_limited(exc):
                # Importer sessions have no 429 hook; a throttled source must
                # stop the batch for a retry, never count as product failures.
                raise StoreRateLimited(f"{self.store}: HTTP 429 while importing {key}") from exc
            if is_gone(exc):
                self.gone.append(key)
                self.log.warning("%s product %s is gone (HTTP %s); skipped.", self.store, key,
                                 getattr(getattr(exc, "response", None), "status_code", "404"))
            else:
                self.failed.append(key)
                self.log.exception("%s product %s failed; skipped for this batch.", self.store, key)
        else:
            self.succeeded += 1

    def exit_code(self) -> int:
        if self.failed and not self.succeeded and not self.gone:
            self.log.error("%s: every product in this batch failed; treating as a source failure.", self.store)
            return EXIT_FAILED
        if self.failed:
            self.log.warning("%s: %s product(s) failed, %s gone, %s imported. Exiting partial.",
                             self.store, len(self.failed), len(self.gone), self.succeeded)
            return EXIT_PARTIAL
        return EXIT_OK
