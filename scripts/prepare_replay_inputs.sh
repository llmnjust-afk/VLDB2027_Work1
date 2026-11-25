#!/usr/bin/env bash
# Fetch public SNAP snapshots and register new full-length streams.
set -euo pipefail
if [[ $# != 1 ]]; then
  echo "Usage: $0 INPUT_DIRECTORY" >&2
  exit 2
fi
project=$(cd "$(dirname "$0")/.." && pwd)
output=$1
mkdir -p "$output"
output=$(cd "$output" && pwd)
for command in curl gzip sha256sum; do command -v "$command" >/dev/null; done
test -x "$project/batchtruss"
test ! -e "$output/inputs.sha256"
manifest=$(mktemp "$output/manifest.XXXXXX")
trap 'rm -f "$manifest"' EXIT
printf 'dataset\tsource\tseed\tbatches\tbatch_size\tp_insert\n' > "$manifest"
while IFS=: read -r dataset archive; do
  url="https://snap.stanford.edu/data/$archive"
  graph="$output/$dataset.graph.txt"
  stream="$output/${dataset}_new_b1000_p0.5.txt"
  if [[ ! -s "$graph" ]]; then
    printf 'Downloading %s from %s\n' "$dataset" "$url" >&2
    curl --fail --silent --show-error --location --retry 2 "$url" | gzip -dc > "$graph.partial"
    mv "$graph.partial" "$graph"
  fi
  "$project/batchtruss" gen --graph "$graph" --out "$stream.partial" \
    --batches 200 --batch-size 1000 --p-insert 0.5 --seed 42
  mv "$stream.partial" "$stream"
  printf '%s\t%s\t42\t200\t1000\t0.5\n' "$dataset" "$url" >> "$manifest"
done <<'DATASETS'
email:email-Eu-core.txt.gz
wikivote:wiki-Vote.txt.gz
amazon:bigdata/communities/com-amazon.ungraph.txt.gz
DATASETS
cd "$output"
sha256sum -- *.graph.txt *_new_b1000_p0.5.txt > inputs.sha256.partial
mv "$manifest" input-manifest.tsv
mv inputs.sha256.partial inputs.sha256
