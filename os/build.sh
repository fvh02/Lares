#!/bin/sh
# Builds Lares OS: lares-os.raw (for the boot test) and lares-os-<version>.raw.zst (to write on a USB stick).
# Needs root and mkosi ≥ 25; see README.md.
set -eu
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' ../core/lares_core/__init__.py)
mkosi --image-version="$VERSION" -f build "$@"
zstd -T0 -19 -f -q lares-os.raw -o "lares-os-$VERSION.raw.zst"
ls -lh lares-os.raw "lares-os-$VERSION.raw.zst"
