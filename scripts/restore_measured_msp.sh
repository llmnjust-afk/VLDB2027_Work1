#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
dest=${1:-"$root/baselines/AccTrussDecomposition/msp"}
manifest="$root/provenance/msp-measured-source.sha256"
patch_file="$root/provenance/msp-local.patch"
work=$(mktemp -d /tmp/vldb2027-restore-msp.XXXXXX)

if [[ -e "$dest" || -L "$dest" ]]; then
  printf 'Destination exists: %s\n' "$dest" >&2
  exit 1
fi
if [[ $# -ge 2 ]]; then
  upstream=$2
else
  upstream="$work/upstream"
  mkdir "$upstream"
  curl --fail --location --retry 2 \
    https://codeload.github.com/RapidsAtHKUST/AccTrussDecomposition/tar.gz/a8faa445ccb45c18383087db488eff9d1836d8d1 \
    -o "$work/upstream.tar.gz"
  tar -xzf "$work/upstream.tar.gz" --strip-components=1 -C "$upstream"
fi

mkdir "$work/rebuilt"
python3 -B - "$manifest" "$upstream/msp" "$work/rebuilt" <<'PY'
import shutil
import sys
from pathlib import Path

manifest, source, target = map(Path, sys.argv[1:])
for line in manifest.read_text().splitlines():
    _, name = line.split(maxsplit=1)
    path = Path(name)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"Unsafe manifest path: {name}")
    output = target / path
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / path, output)
PY

if [[ -s "$patch_file" ]]; then
  patch -s -d "$work/rebuilt" -p1 < "$patch_file"
fi
(cd "$work/rebuilt" && sha256sum -c --quiet "$manifest")
mkdir -p "$(dirname "$dest")"
mv -T -- "$work/rebuilt" "$dest"
printf 'Measured MSP source restored and verified at %s\n' "$dest"
