"""Exercise the actual selector on a tiny fixture, without containers or API keys."""
from pathlib import Path
import argparse
import json
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out-dir', type=Path, default=ROOT / '.work' / 'selector-demo')
    args = parser.parse_args()
    out = args.out_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        sys.executable, str(ROOT / 'optimizer/tools/select_prefetch_paths.py'),
        '--candidates', str(ROOT / 'examples/candidates.json'),
        '--out-yaml', str(out / 'hints.yaml'), '--out-list', str(out / 'estargz.list'),
        '--stats-json', str(out / 'selector_stats.json'), '--budget-mb', '0.008',
    ], cwd=ROOT, check=True)
    chosen = (out / 'estargz.list').read_text(encoding='utf-8').splitlines()
    stats = json.loads((out / 'selector_stats.json').read_text(encoding='utf-8'))
    if chosen != ['/app/hot.so'] or stats['chosen_bytes'] > stats['budget_bytes']:
        raise RuntimeError(f'Unexpected selector result: {chosen}')
    if stats['non_regular_candidates_skipped'] != 1:
        raise RuntimeError('The directory candidate was not rejected')
    print(json.dumps({'chosen': chosen, 'directory_rejected': True,
                      'artifacts': ['hints.yaml', 'estargz.list', 'selector_stats.json'],
                      'scope': 'Synthetic selector demonstration; not a performance benchmark'}, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
