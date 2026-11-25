#!/usr/bin/env bash
# Run one registered main4 follow-up on the same host as prior measurements.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

group=${1:-}
cpu=${MAIN4_CPU:-0}
source_inputs=.build/vldb2027-main3-adaptive-online/prepared
heldout=.build/vldb2027-main4-heldout

audit_unified() {
  python3 -B scripts/run_unified_dynamic.py --check-only --output "$1"
}

case "$group" in
  heldout)
    python3 -B scripts/run_unified_dynamic.py --cases \
      --prepared stackoverflow-2014-heldout-b1d "$source_inputs/stackoverflow-2014-test-b1d" 7 \
      --prepared stackoverflow-2014-heldout-b14d "$source_inputs/stackoverflow-2014-test-b14d" 7 \
      --include-pruned --profile-pruned --runs 3 --cpu "$cpu" --output "$heldout"
    audit_unified "$heldout"
    ;;
  labels)
    taskset -c "$cpu" python3 -B scripts/generate_main4_diagnostics.py \
      --measure-missing --extra-root "$heldout"
    ;;
  routing)
    out=.build/vldb2027-main4-adaptive-pruned
    python3 -B scripts/run_adaptive_online.py --pruned-routes --prepared-root "$source_inputs" \
      --runs 3 --cpu "$cpu" --output "$out"
    python3 -B scripts/run_adaptive_online.py --check-only --output "$out"
    ;;
  cancellation)
    out=.build/vldb2027-main4-cancellation
    python3 -B scripts/run_main4_cancellation.py --pairs 20 --runs 3 --cpu "$cpu" --output "$out"
    python3 -B scripts/run_main4_cancellation.py --check-only --output "$out"
    ;;
  scale)
    out=.build/vldb2027-main4-scale
    early=.build/vldb2027-main4-scale-early
    late=.build/vldb2027-main4-scale-late
    taskset -c "$cpu" python3 -B scripts/prepare_large_temporal_replay.py \
      --start-date 2011-07-07 --retention-days 90 --checkpoint-days 7 --replay-days 56 --output "$early"
    taskset -c "$cpu" python3 -B scripts/prepare_large_temporal_replay.py \
      --start-date 2015-09-03 --retention-days 90 --checkpoint-days 7 --replay-days 56 --output "$late"
    python3 -B scripts/run_unified_dynamic.py --cases \
      --prepared stackoverflow-2011-w90d-b7d "$early" 90 \
      --prepared stackoverflow-2015-w90d-b7d "$late" 90 \
      --include-pruned --profile-pruned --runs 3 --cpu "$cpu" --output "$out"
    audit_unified "$out"
    ;;
  -h|--help)
    printf 'Usage: bash %s {heldout|labels|routing|cancellation|scale}\n' "$0"
    printf 'Run heldout before labels. Set MAIN4_CPU to change the pinned core.\n'
    exit 0
    ;;
  *)
    printf 'Unknown experiment %s. Run with --help.\n' "$group" >&2
    exit 2
    ;;
esac
printf 'MAIN4 %s COMPLETE\n' "$group"
