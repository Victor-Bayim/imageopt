#!/usr/bin/env python3
"""
Convert a hints.yaml file produced by the cpp-sampler pipeline into a plain
estargz.list format. The generated list contains one file path per line
sorted by priority groups P0→P3. Entries are deduplicated and trailing
slashes are removed to avoid directory entries. Globs (e.g. *.so, */) are
preserved as-is; directories ending with slash are omitted since estargz
prefetch lists only accept files or wildcard patterns.  

Usage:
    python3 hints_to_estargz_list.py --in hints.yaml --out estargz.list

Requirements:
    This script depends only on the standard library. It can optionally
    fall back to a tiny YAML parser if PyYAML is not available, but using
    PyYAML will yield better error messages for malformed YAML.

Example:
    Given a hints.yaml like:

        P0:
          - /lib/x86_64-linux-gnu/libc.so.6
          - /etc/ld.so.cache
        P1:
          - /lib/x86_64-linux-gnu/libm.so.6
        P2: []
        P3:
          - /dev/null

    Running this script will produce estargz.list containing:

        /lib/x86_64-linux-gnu/libc.so.6
        /etc/ld.so.cache
        /lib/x86_64-linux-gnu/libm.so.6
        /dev/null

    Use the resulting estargz.list with ctr-remote or stargzify to embed
    prefetch hints into an eStargz/Nydus image.
"""

import argparse
import re
from collections import OrderedDict

try:
    import yaml  # type: ignore
    _HAVE_PYYAML = True
except ImportError:
    _HAVE_PYYAML = False


def _tiny_yaml_load(stream: str) -> dict:
    """Fallback YAML loader for a very restricted subset used in hints.yaml."""
    data: OrderedDict[str, list[str]] = OrderedDict()
    current_key = None
    for raw_line in stream.splitlines():
        line = raw_line.rstrip()
        # Skip empty lines and comments
        if not line or line.lstrip().startswith('#'):
            continue
        if re.match(r'^\s*P[0-9]:', line):
            key = line.split(':', 1)[0].strip()
            data[key] = []
            current_key = key
        elif re.match(r'^\s*-\s+', line) and current_key is not None:
            value = line.split('-', 1)[1].strip()
            data[current_key].append(value)
        else:
            raise ValueError(f"Unsupported YAML format: {line}")
    return data


def load_hints(path: str) -> "OrderedDict[str, list[str]]":
    """Load hints.yaml returning an ordered mapping of priority -> list of paths."""
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    if _HAVE_PYYAML:
        try:
            data = yaml.safe_load(content)
        except Exception as e:
            raise RuntimeError(f"Failed to parse {path}: {e}")
    else:
        data = _tiny_yaml_load(content)
    ordered = OrderedDict()
    for key in sorted(data.keys(), key=lambda k: int(k[1:]) if k[1:].isdigit() else 99):
        ordered[key] = list(data.get(key, []) or [])
    return ordered


def convert_to_list(hints: "OrderedDict[str, list[str]]") -> list[str]:
    """Flatten and deduplicate hint entries preserving priority order."""
    seen: set[str] = set()
    result: list[str] = []
    for priority in hints:
        for path in hints[priority]:
            # Skip None or non-string entries
            if not path or not isinstance(path, str):
                continue
            # Skip directories (end with slash)
            if path.endswith('/'):
                continue
            normalized = path.rstrip('/')
            if normalized and normalized not in seen:
                seen.add(normalized)
                result.append(normalized)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--in', dest='inp', required=True, metavar='PATH', help='Path to hints.yaml produced by the pipeline')
    parser.add_argument('--out', dest='outp', required=True, metavar='PATH', help='Path to output estargz.list file')
    args = parser.parse_args()

    hints = load_hints(args.inp)
    flattened = convert_to_list(hints)
    with open(args.outp, 'w', encoding='utf-8') as out_f:
        for entry in flattened:
            out_f.write(entry + '\n')
    print(f"Wrote {len(flattened)} entries to {args.outp}")


if __name__ == '__main__':
    main()

