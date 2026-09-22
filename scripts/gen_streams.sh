#!/bin/bash
set -e
DATA=${1:-/data/lab/data}
OUT=${2:-/data/lab/streams}
mkdir -p "$OUT"
cd "$(dirname "$0")/.."
B=./batchtruss

gen() {
  local name=$1 graph=$2 nb=$3 bs=$4 pi=$5 seed=$6
  if [ -s "$OUT/$name.txt" ]; then echo "skip $name"; return; fi
  $B gen --graph "$graph" --out "$OUT/$name.txt" --batches $nb --batch-size $bs --p-insert $pi --seed $seed
  echo "gen $name done"
}

mk() {
  local name=$1 graph=$2 ops=$3 bs=$4 pi=$5
  local nb=$(( ops / bs )); [ $nb -lt 20 ] && nb=20
  gen "$name" "$graph" $nb $bs $pi 42
}

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

for spec in "${GRAPHS[@]}"; do
  name=${spec%%:*}; graph=$DATA/${spec##*:}
  [ -s "$graph" ] || { echo "MISSING $graph"; continue; }
  for bs in 1 10 100 1000 10000; do
    for pi in 1.0 0.5 0.0; do
      mk "${name}_b${bs}_p${pi}" "$graph" 200000 $bs $pi
    done
  done
  for pi in 0.75 0.25; do
    mk "${name}_b1000_p${pi}" "$graph" 200000 1000 $pi
  done
  # smaller streams for the slow sequential per-edge baseline on small graphs only
  case $name in
    email|wikivote|enron|gnutella)
      for bs in 1 10 100 1000; do
        for pi in 1.0 0.5 0.0; do
          mk "${name}_s_b${bs}_p${pi}" "$graph" 50000 $bs $pi
        done
      done ;;
  esac
done
echo "ALL STREAMS DONE"
