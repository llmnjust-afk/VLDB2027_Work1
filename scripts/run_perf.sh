#!/bin/bash
# Performance experiments: one worker per graph (parallel across graphs, sequential within).
# Grid at T=32 (sizes x mixes x 3 runs), thread scaling on b1000_p0.5, ablation/static/peredge at T=32.
# Usage: run_perf.sh [DATA] [OUT]
DATA=${1:-/data/lab/data}
OUT=${2:-/data/lab/results}
SCALE_THREADS=${SCALE_THREADS:-"1 2 4 8 16 32 64 128 256"}
mkdir -p "$OUT"
cd "$(dirname "$0")/.."
B=./batchtruss

GRAPHS=(
  "email:email-Eu-core.txt"
  "wikivote:wiki-Vote.txt"
  "enron:email-Enron.txt"
  "gnutella:p2p-Gnutella31.txt"
  "amazon:com-amazon.ungraph.txt"
  "youtube:com-youtube.ungraph.txt"
  "skitter:as-skitter.txt"
  "lj:soc-LiveJournal1.txt"
)

worker() {
  local gname=$1 graph=$2
  local S=/data/lab/streams
  local csv=$OUT/batch_${gname}_t32.csv
  touch "$csv"
  for bs in 1 10 100 1000 10000; do
    for pi in 1.0 0.5 0.0; do
      st=$S/${gname}_b${bs}_p${pi}.txt
      [ -s "$st" ] || continue
      grep -q "${gname}_b${bs}_p${pi}.txt," "$csv" 2>/dev/null && continue
      $B batch --graph "$graph" --stream "$st" --threads 32 --ablate full --runs 3 --dataset $gname >> "$csv" 2>/dev/null
      echo "[$gname] grid $(basename $st)"
    done
  done
  # thread scaling on b1000_p0.5
  st=$S/${gname}_b1000_p0.5.txt
  if [ -s "$st" ]; then
    ncsv=$OUT/batch_${gname}_scale.csv
    touch "$ncsv"
    for t in $SCALE_THREADS; do
      grep -q "${gname}_b1000_p0.5.txt,$t," "$ncsv" 2>/dev/null && continue
      $B batch --graph "$graph" --stream "$st" --threads $t --ablate full --runs 3 --dataset $gname >> "$ncsv" 2>/dev/null
      echo "[$gname] scale t$t"
    done
    # ablation nomerge at T=32
    nmcsv=$OUT/nomerge_${gname}_t32.csv; touch "$nmcsv"
    grep -q "${gname}_b1000_p0.5.txt," "$nmcsv" 2>/dev/null || \
      $B batch --graph "$graph" --stream "$st" --threads 32 --ablate nomerge --runs 3 --dataset $gname >> "$nmcsv" 2>/dev/null
    # static recompute on same stream
    scsv=$OUT/static_${gname}.csv; touch "$scsv"
    grep -q "${gname}_b1000_p0.5.txt," "$scsv" 2>/dev/null || \
      $B static --graph "$graph" --stream "$st" --threads 32 --dataset $gname >> "$scsv" 2>/dev/null
    echo "[$gname] nomerge+static b1000"
  fi
  # per-edge baseline on small graphs (reduced stream)
  case $gname in
    email|wikivote|enron|gnutella)
      for bs in 1 10 100 1000; do
        st=$S/${gname}_s_b${bs}_p0.5.txt
        [ -s "$st" ] || continue
        pcsv=$OUT/peredge_${gname}.csv; touch "$pcsv"
        grep -q "${gname}_s_b${bs}_p0.5.txt," "$pcsv" 2>/dev/null || \
          $B peredge --graph "$graph" --stream "$st" --dataset $gname >> "$pcsv" 2>/dev/null
        echo "[$gname] peredge b$bs"
      done ;;
  esac
  # build-only (empty stream)
  : > /tmp/empty_stream.txt
  ecsv=$OUT/build_${gname}.csv; touch "$ecsv"
  grep -q "empty," "$ecsv" 2>/dev/null || \
    $B static --graph "$graph" --stream /tmp/empty_stream.txt --threads 32 --dataset $gname >> "$ecsv" 2>/dev/null
  echo "[$gname] build done"
}

PIDS=()
for spec in "${GRAPHS[@]}"; do
  gname=${spec%%:*}; graph=$DATA/${spec##*:}
  [ -s "$graph" ] || { echo "MISSING $graph"; continue; }
  worker "$gname" "$graph" &
  PIDS+=($!)
done
wait
echo "PERF DONE"
