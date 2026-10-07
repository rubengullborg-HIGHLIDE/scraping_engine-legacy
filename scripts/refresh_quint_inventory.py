from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.refresh_axel_quint_inventory import main

if __name__ == '__main__':
    raise SystemExit(main('quint'))
