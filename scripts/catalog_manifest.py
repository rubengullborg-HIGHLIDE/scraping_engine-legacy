"""Freeze discovery ordering and metadata across bounded catalogue imports."""
import json
import os
from pathlib import Path


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(state, stream, ensure_ascii=False)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    descriptor = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def add_manifest_argument(parser):
    parser.add_argument('--catalog-manifest', help='Internal weekly runner discovery manifest; reused across batches.')


def manifest_items(args, store, discover):
    filename = getattr(args, 'catalog_manifest', None)
    if not filename:
        return discover()
    if args.url or getattr(args, 'checkpoint', None):
        raise ValueError('Catalog manifest cannot be combined with URLs or an import checkpoint')
    path = Path(filename)
    state = json.loads(path.read_text()) if path.exists() else None
    if state is None:
        items = discover()
        state = {'version': 1, 'store': store, 'items': items}
    if state.get('version') != 1 or state.get('store') != store:
        raise ValueError('Catalog manifest version/store mismatch')
    items = state.get('items')
    if not isinstance(items, list) or not items or any(
        not isinstance(item, (str, dict)) or not item for item in items
    ):
        raise ValueError('Empty or invalid discovery manifest; reconciliation prohibited')
    if not path.exists():
        save_state(path, state)
    return items
