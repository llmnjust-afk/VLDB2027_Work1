#!/bin/bash
# Performance experiments: one worker per graph (parallel across graphs, sequential within).
# Grid at T=32 (sizes x mixes), thread scaling on b1000_p0.5, ablation/static/peredge at T=32.
# Giant hub graphs (lj, skitter, youtube) run 3 repetitions and p=0.5 only on the B<=10 legs.
# Resume is count-based: a stream is skipped once it has >= RUNS completed rows.
# Usage: run_perf.sh [DATA] [OUT]
DATA=${1:-/data/lab/data}
OUT=${2:-/data/lab/results}
SCALE_THREADS=${SCALE_THREADS:-"1 8 32 256"}
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
  local RUNS=5
  case $gname in lj|skitter|youtube) RUNS=3 ;; esac
  local S=/data/lab/streams
  local csv=$OUT/batch_${gname}_t32.csv
  touch "$csv"
  for bs in 1 10 100 1000 10000; do
    local mixes="1.0 0.5 0.0"
    case $bs in 10|100) mixes="0.5" ;; esac
    case $gname in lj|skitter|youtube) mixes="0.5" ;; esac
    for pi in $mixes; do
      local st=$S/${gname}_b${bs}_p${pi}.txt
      [ -s "$st" ] || continue
      [ "$(grep -c "${gname},batch,32,full,${gname}_b${bs}_p${pi}," "$csv" 2>/dev/null)" -ge "$RUNS" ] && continue
      $B batch --graph "$graph" --stream "$st" --threads 32 --ablate full --runs $RUNS --dataset $gname >> "$csv" 2>/dev/null
      echo "[$gname] grid $(basename $st)"
    done
  done
  for pi in 0.75 0.25; do
    local st=$S/${gname}_b1000_p${pi}.txt
    [ -s "$st" ] || continue
    [ "$(grep -c "${gname},batch,32,full,${gname}_b1000_p${pi}," "$csv" 2>/dev/null)" -ge "$RUNS" ] && continue
    $B batch --graph "$graph" --stream "$st" --threads 32 --ablate full --runs $RUNS --dataset $gname >> "$csv" 2>/dev/null
    echo "[$gname] grid $(basename $st)"
  done
  # thread scaling on b1000_p0.5
  local st=$S/${gname}_b1000_p0.5.txt
  if [ -s "$st" ]; then
    local ncsv=$OUT/batch_${gname}_scale.csv
    touch "$ncsv"
    for t in $SCALE_THREADS; do
      [ "$(grep -c "${gname},batch,$t,full,${gname}_b1000_p0.5," "$ncsv" 2>/dev/null)" -ge "$RUNS" ] && continue
      $B batch --graph "$graph" --stream "$st" --threads $t --ablate full --runs $RUNS --dataset $gname >> "$ncsv" 2>/dev/null
      echo "[$gname] scale t$t"
    done
    # ablation nomerge at T=32 (last run dumps the final graph for the static baseline)
    local nmcsv=$OUT/nomerge_${gname}_t32.csv; touch "$nmcsv"
    local final=/data/lab/final_${gname}.txt
    [ "$(grep -c "${gname},batch,32,nomerge,${gname}_b1000_p0.5," "$nmcsv" 2>/dev/null)" -ge "$RUNS" ] || \
      $B batch --graph "$graph" --stream "$st" --threads 32 --ablate nomerge --runs $RUNS --dataset $gname --graph-out "$final" >> "$nmcsv" 2>/dev/null
    # static recompute baseline on the FINAL graph of the b1000_p0.5 stream
    local scsv=$OUT/static_${gname}.csv; touch "$scsv"
    grep -q "static" "$scsv" 2>/dev/null || \
      $B static --graph "$final" --threads 32 --dataset $gname >> "$scsv" 2>/dev/null
    # ablation allseeds at T=32 on the b1000_p0.5 workload
    local ascsv=$OUT/allseeds_${gname}_t32.csv; touch "$ascsv"
    [ "$(grep -c "${gname},batch,32,allseeds,${gname}_b1000_p0.5," "$ascsv" 2>/dev/null)" -ge "$RUNS" ] || \
      $B batch --graph "$graph" --stream "$st" --threads 32 --ablate allseeds --runs $RUNS --dataset $gname >> "$ascsv" 2>/dev/null
    echo "[$gname] nomerge+static+allseeds b1000"
  fi
  # per-edge baseline on small graphs (reduced stream)
  case $gname in
    email|wikivote|enron|gnutella)
      for bs in 1 10 100 1000; do
        local st=$S/${gname}_s_b${bs}_p0.5.txt
        [ -s "$st" ] || continue
        local pcsv=$OUT/peredge_${gname}.csv; touch "$pcsv"
        grep -q "${gname},peredge,1,-,${gname}_s_b${bs}_p0.5," "$pcsv" 2>/dev/null && continue
        $B peredge --graph "$graph" --stream "$st" --dataset $gname >> "$pcsv" 2>/dev/null
        echo "[$gname] peredge b$bs"
      done ;;
  esac
  # build-only (empty stream)
  : > /tmp/empty_stream.txt
  local ecsv=$OUT/build_${gname}.csv; touch "$ecsv"
  grep -q "static" "$ecsv" 2>/dev/null || \
    $B static --graph "$graph" --threads 32 --dataset $gname >> "$ecsv" 2>/dev/null
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
