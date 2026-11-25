#!/usr/bin/env bash
# Collect a four-thread comparison on the full streams used by replay_core.sh.
set -euo pipefail
cd "$(dirname "$0")/.."

inputs="$PWD/.build/vldb2027-inputs"
(cd "$inputs" && sha256sum -c inputs.sha256)
run=$(mktemp -d "$PWD/.build/vldb2027-scale-t4.XXXXXX")
printf 'Run directory: %s\n' "$run"
cp "$inputs/input-manifest.tsv" "$inputs/inputs.sha256" "$run/"

groups=()
for dataset in email wikivote amazon; do
  group="$run/$dataset-t4"
  printf 'Dataset: %s\n' "$dataset"
  python3 -B scripts/compare_replay.py \
    --graph "$inputs/$dataset.graph.txt" \
    --stream "$inputs/${dataset}_new_b1000_p0.5.txt" \
    --dataset "$dataset" --threads 4 --runs 3 \
    --methods full static msp --output "$group"
  groups+=("$group")
done

python3 -B scripts/summarize_replay.py "${groups[@]}" --latex "$run/tab_replay_t4.tex"
printf 'Verified four-thread table and raw results: %s\n' "$run"
