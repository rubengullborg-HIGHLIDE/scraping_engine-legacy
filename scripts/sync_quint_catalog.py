"""Full quint catalogue import and lifecycle reconciliation (Sunday workflow)."""
from pathlib import Path
import logging
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.sync_store_catalogs import parse_args, sync_catalogs

if __name__ == '__main__':
    sys.argv[1:1] = ['--store', 'quint']
    args = parse_args()
    if args.store != ['quint']:
        raise ValueError('Dedicated catalogue command accepts only quint')
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    raise SystemExit(sync_catalogs(args))
