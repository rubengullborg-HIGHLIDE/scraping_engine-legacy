"""File-based persistence shared by the Weekday local import and refresh runners."""
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import tempfile

from scrapers.full_import.weekday import require


def now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text())


def atomic_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + '.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextmanager
def locked(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / '.lock').open('a') as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError('Another Weekday job is using this work directory') from exc
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def fingerprint(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def cached(directory, kind, key, fetch, offline=False):
    require(key.isdigit(), 'Cache key must be a source SKU')
    path = Path(directory) / kind / (key + '.json')
    if path.exists():
        return read_json(path)
    require(not offline, 'Missing offline cache: ' + str(path))
    value = fetch()
    record = {'observed_at': now(), 'value': value}
    atomic_json(path, record)
    return record
