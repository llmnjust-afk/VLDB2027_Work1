#!/usr/bin/env bash
# Build, verify, and collect new same-input experiments in one fresh run directory.
set -euo pipefail
cd "$(dirname "$0")/.."
mode=${1:-smoke}
if [[ "$mode" != smoke && "$mode" != full ]]; then
  echo "Usage: $0 {smoke|full}" >&2
  exit 2
fi
for tool in gcc g++ make python3 sha256sum lscpu; do command -v "$tool" >/dev/null; done
if [[ "$mode" == full ]]; then
  for tool in git curl gzip; do command -v "$tool" >/dev/null; done
fi

sha256sum -c verification/vldb2027/source-current.sha256 --quiet
sha256sum -c verification/vldb2027/historical-results.sha256 --quiet
mkdir -p .build
run=$(mktemp -d "$PWD/.build/vldb2027-run.XXXXXX")
echo "Run directory: $run"
g++ --version > "$run/compiler.txt"
lscpu > "$run/hardware.txt"
make -j "$(nproc)" all batchtruss-msp 2>&1 | tee "$run/build.log"
bash run_matrix.sh | tee "$run/regression.log"
python3 -B scripts/compare_replay.py --graph data/synth1.txt --stream data/s1_b10_p0.5.txt \
  --dataset synth1 --threads 1 --runs 3 --output "$run/smoke"
python3 -B scripts/summarize_replay.py "$run/smoke" --latex "$run/tab_smoke.tex"
if [[ "$mode" == smoke ]]; then exit 0; fi

inputs="$PWD/.build/vldb2027-inputs"
if [[ ! -s "$inputs/inputs.sha256" ]]; then
  bash scripts/prepare_replay_inputs.sh "$inputs" 2>&1 | tee "$run/input-prepare.log"
fi
(cd "$inputs" && sha256sum -c inputs.sha256)
cp "$inputs/input-manifest.tsv" "$inputs/inputs.sha256" "$run/"

star="$PWD/.build/TrussMaintenance"
revision=8e9af42f2dfcbaa402edb5d052a1a5ff24bff5d0
star_manifest="$PWD/scripts/star-source.sha256"
(
  if [[ ! -e "$star/.git" && ! -d "$star/code" ]]; then
    GIT_TERMINAL_PROMPT=0 git clone https://github.com/jinrdfh/TrussMaintenance.git "$star"
  fi
  if [[ -e "$star/.git" ]]; then
    git -C "$star" checkout --detach "$revision"
    test "$(git -C "$star" rev-parse HEAD)" = "$revision"
  else
    printf 'Using local Star source snapshot for %s\n' "$revision"
  fi
  (cd "$star" && sha256sum -c --quiet "$star_manifest")
) 2>&1 | tee "$run/star-prepare.log"
printf '%s\n' "$revision" > "$run/star-source.txt"
cp "$star_manifest" "$run/star-source.sha256"
for target in index ballDel ballsIns; do
  make -C "$star/code/$target" -j 4 2>&1 | tee "$run/star-build-$target.log"
done
python3 -B scripts/compare_star.py --star-root "$star" --graph tests/fixtures/k4.graph \
  --stream tests/fixtures/k4-mixed.stream --runs 1 --output "$run/star-smoke"

threads=$(nproc)
if (( threads > 32 )); then threads=32; fi
single=()
multi=()
pairs=()
for dataset in email wikivote amazon; do
  graph="$inputs/$dataset.graph.txt"
  stream="$inputs/${dataset}_new_b1000_p0.5.txt"
  test -s "$graph" -a -s "$stream"
  python3 -B scripts/compare_replay.py --graph "$graph" --stream "$stream" \
    --dataset "$dataset" --threads 1 --runs 5 --output "$run/$dataset-t1"
  single+=("$run/$dataset-t1")
  if (( threads > 1 )); then
    python3 -B scripts/compare_replay.py --graph "$graph" --stream "$stream" \
      --dataset "$dataset" --threads "$threads" --runs 5 --output "$run/$dataset-t$threads"
    multi+=("$run/$dataset-t$threads")
  fi
  python3 -B scripts/compare_star.py --star-root "$star" --graph "$graph" --stream "$stream" \
    --runs 5 --output "$run/star-$dataset-t1"
  pairs+=(--pair "$run/$dataset-t1" "$run/star-$dataset-t1")
done
python3 -B scripts/summarize_replay.py "${single[@]}" "${multi[@]}" \
  --latex "$run/tab_replay_server.tex"
python3 -B scripts/summarize_star.py "${pairs[@]}" --latex "$run/tab_star_server.tex"
echo "Verified tables and raw results: $run"
