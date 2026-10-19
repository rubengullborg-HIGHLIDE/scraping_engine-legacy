"""Shared source-store rate-limit policy; never used for database requests."""
from urllib3.util.retry import Retry
from email.utils import parsedate_to_datetime
import time


class StoreRateLimited(RuntimeError):
    """Stop a source store for this run, without changing unobserved inventory."""

    stats = None
    retry_after_seconds = None


class SourcePayloadError(ValueError):
    """A source request succeeded but omitted the expected usable payload."""


class SourceRetry(Retry):
    def is_retry(self, method, status_code, has_retry_after=False):
        # Removing 429 from status_forcelist alone is insufficient: urllib3 also
        # retries it when Retry-After is present. Leave the source alone instead.
        if status_code == 429:
            return False
        return super().is_retry(method, status_code, has_retry_after)


def stop_on_source_rate_limit(response, *args, **kwargs):
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "not supplied")
        response.close()
        error = StoreRateLimited(
            f"HTTP 429 from {response.url}; Retry-After={retry_after}. "
            "No further requests to this store during this run; saved stock retained."
        )
        try:
            value = response.headers.get('Retry-After', '').strip()
            error.retry_after_seconds = (int(value) if value.isdecimal()
                                         else max(0, parsedate_to_datetime(value).timestamp() - time.time()))
        except (TypeError, ValueError, OverflowError):
            pass
        raise error
    return response
