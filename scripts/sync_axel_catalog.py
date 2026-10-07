"""Full axel catalogue import and lifecycle reconciliation (Sunday workflow)."""
from pathlib import Path
import logging
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.sync_store_catalogs import parse_args, sync_catalogs

if __name__ == '__main__':
    sys.argv[1:1] = ['--store', 'axel']
    args = parse_args()
    if args.store != ['axel']:
        raise ValueError('Dedicated catalogue command accepts only axel')
    logging.basicConfig(level=args.log_level, format='%(asctime)s %(levelname)s %(message)s')
    raise SystemExit(sync_catalogs(args))
