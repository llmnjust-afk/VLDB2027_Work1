#!/usr/bin/env bash
# Run the complete real-data replays for the core single-thread comparison.
set -euo pipefail
cd "$(dirname "$0")/.."

inputs="$PWD/.build/vldb2027-inputs"
binary="$PWD/batchtruss-msp"
if [[ ! -x "$binary" ]]; then
  echo "Missing batchtruss-msp. Build it with: make batchtruss-msp" >&2
  exit 1
fi
(cd "$inputs" && sha256sum -c inputs.sha256)

run=$(mktemp -d "$PWD/.build/vldb2027-core.XXXXXX")
printf 'Run directory: %s\n' "$run"
cp "$inputs/input-manifest.tsv" "$inputs/inputs.sha256" "$run/"

groups=()
for dataset in email wikivote amazon; do
  group="$run/$dataset-t1"
  printf 'Dataset: %s\n' "$dataset"
  python3 -B scripts/compare_replay.py \
    --graph "$inputs/$dataset.graph.txt" \
    --stream "$inputs/${dataset}_new_b1000_p0.5.txt" \
    --binary "$binary" --dataset "$dataset" --threads 1 --runs 5 \
    --methods full static msp --output "$group"
  groups+=("$group")
done

python3 -B scripts/summarize_replay.py "${groups[@]}" --latex "$run/tab_replay_core.tex"
printf 'Verified tables and raw results: %s\n' "$run"
