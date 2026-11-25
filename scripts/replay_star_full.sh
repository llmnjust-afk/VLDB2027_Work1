#!/usr/bin/env bash
# Compare the released Star artifact against an existing single-thread core run.
set -euo pipefail
if (( $# != 1 )); then
  echo "Usage: $0 CORE_RUN_DIRECTORY" >&2
  exit 2
fi
core=$(cd "$1" && pwd -P)
cd "$(dirname "$0")/.."

inputs="$PWD/.build/vldb2027-inputs"
star="$PWD/.build/TrussMaintenance"
source_manifest="$PWD/scripts/star-source.sha256"
datasets=(email wikivote amazon)
for dataset in "${datasets[@]}"; do
  test -f "$core/$dataset-t1/complete"
done
(cd "$inputs" && sha256sum -c "$core/inputs.sha256")
(cd "$star" && sha256sum -c --quiet "$source_manifest")

run=$(mktemp -d "$PWD/.build/vldb2027-star-full.XXXXXX")
printf 'Run directory: %s\n' "$run"
cp "$core/input-manifest.tsv" "$core/inputs.sha256" "$source_manifest" "$run/"

pairs=()
for dataset in "${datasets[@]}"; do
  printf 'Dataset: %s\n' "$dataset"
  output="$run/$dataset-t1"
  python3 -B scripts/compare_star.py --star-root "$star" \
    --graph "$inputs/$dataset.graph.txt" \
    --stream "$inputs/${dataset}_new_b1000_p0.5.txt" \
    --runs 3 --output "$output"
  pairs+=(--pair "$core/$dataset-t1" "$output")
done

python3 -B scripts/summarize_star.py "${pairs[@]}" --latex "$run/tab_star_full.tex"
printf 'Verified Star table and raw results: %s\n' "$run"
