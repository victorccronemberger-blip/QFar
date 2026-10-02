"""Build/search the original Ego4D inventory, offline, without submitting media."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from moneymin.ego4d_library import audit_sensor, index_library, search_library

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', type=Path, default=Path('data/ego4d'))
    parser.add_argument('--output', type=Path, default=Path('data/ego4d/library.sqlite3'))
    parser.add_argument('--search', help='Search original scenarios and annotations (FTS5 syntax).')
    parser.add_argument('--audit-sensors', action='store_true')
    args = parser.parse_args()
    if args.audit_sensors:
        result = [audit_sensor(path) for path in sorted(args.directory.glob('*_imu.csv'))]
    else:
        result = search_library(args.output, args.search) if args.search else index_library(args.directory, args.output)
    print(json.dumps(result, indent=2, ensure_ascii=False))
