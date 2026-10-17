#!/usr/bin/env python3
"""Prepare a small public-source-only context; does not submit a paid build."""
import argparse
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = {'ayugram-inline.patch': 'experimental/ayugram-inline/ayugram-inline.patch',
         'build_linux.sh': 'tools/ayugram_remote/build_linux.sh'}


def prepare(destination):
    destination = Path(destination)
    if not destination.is_absolute() or destination.exists() or destination.is_symlink():
        raise ValueError('Choose a new absolute directory for the build context')
    sources = {name: (ROOT / relative).read_bytes() for name, relative in FILES.items()}
    destination.mkdir(mode=0o700)
    for name, contents in sources.items():
        (destination/name).write_bytes(contents)
    (destination/'build_linux.sh').chmod(0o700)
    receipt = {name: hashlib.sha256(contents).hexdigest() for name, contents in sources.items()}
    (destination/'context.json').write_text(json.dumps(receipt, indent=2)+'\n')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    prepare(args.destination)
    print('Source-only context prepared. No cloud build was submitted; no sessions were included.')
