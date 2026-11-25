#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
run="$PWD/.build/vldb2027-revision-phase1"
max_threads=8
smoke_only=false
email_full=false

usage() {
  cat <<'EOF'
Usage: bash scripts/run_revision_phase1.sh [--run-dir DIR] [--max-threads 1|2|4|8|16]
                                          [--smoke-only | --with-email-full]

Run setup, verification pilots, formal replays, and reporting in order.
Use the same run directory on the same node to resume completed configurations.
--smoke-only stops after the K4 test, the three-batch pilots, and their report.
--with-email-full also collects three new timing replays of the full Email ablation.
EOF
}

while (( $# )); do
  case "$1" in
    --run-dir|--max-threads)
      if (( $# < 2 )) || [[ -z "$2" || "$2" == --* ]]; then
        printf '%s requires a value\n' "$1" >&2
        exit 2
      fi
      if [[ "$1" == --run-dir ]]; then
        run=$2
      else
        max_threads=$2
      fi
      shift 2
      ;;
    --smoke-only) smoke_only=true; shift ;;
    --with-email-full) email_full=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 2 ;;
  esac
done

case "$max_threads" in
  1|2|4|8|16) ;;
  *) printf 'Invalid --max-threads value: %s\n' "$max_threads" >&2; exit 2 ;;
esac

if "$smoke_only" && "$email_full"; then
  printf '%s\n' 'Choose either --smoke-only or --with-email-full.' >&2
  exit 2
fi

mkdir -p "$run"
run=$(cd "$run" && pwd -P)
exec 9>"$run/.phase1.lock"
if ! flock -n 9; then
  printf 'Phase 1 already running in %s\n' "$run" >&2
  exit 1
fi
printf 'Run directory: %s\n' "$run"

log_step() {
  local step=$1
  shift
  "$@" 2>&1 | tee -a "$run/$step.log"
}

runner=(python3 -B scripts/revision_experiments.py --run "$run")
log_step env "${runner[@]}" env --jobs 4
log_step smoke "${runner[@]}" smoke
log_step pilot "${runner[@]}" pilot --prefix-batches 3

report=(-B scripts/revision_report.py --run "$run")
groups=(smoke pilot)
if "$smoke_only"; then
  log_step summarize python3 "${report[@]}" summarize --groups "${groups[@]}" --no-plots
  printf 'Smoke test complete: %s\n' "$run"
  exit 0
fi

mkdir -p "$run/mpl"
export MPLCONFIGDIR="$run/mpl"
report_python=python3
plot_options=()
failures=()
if ! "$report_python" -B -c 'import matplotlib.figure' >/dev/null 2>&1; then
  report_python="$HOME/.conda/envs/dl/bin/python3"
  if ! "$report_python" -B -c 'import matplotlib.figure' >/dev/null 2>&1; then
    report_python=python3
    plot_options=(--no-plots)
    failures+=("Matplotlib unavailable")
    printf '%s\n' 'Matplotlib unavailable. The run will still export CSV and LaTeX.' >&2
  fi
fi

formal() {
  local group=$1
  shift
  groups+=("$group")
  if ! log_step "$group" "${runner[@]}" "$@"; then
    failures+=("$group")
  fi
}

formal main main
formal batch-size batch-size
formal threads threads --max-threads "$max_threads"
formal ablation-full ablation
formal ablation-prefix-5 ablation --prefix-batches 5
if "$email_full"; then
  formal ablation-full-fresh ablation --fresh --datasets email
fi

audit_options=()
if ! log_step check python3 "${report[@]}" check --groups "${groups[@]}"; then
  failures+=("result audit")
  audit_options=(--allow-incomplete)
fi
if ! log_step summarize "$report_python" "${report[@]}" summarize --star \
    --groups "${groups[@]}" "${plot_options[@]}" "${audit_options[@]}"; then
  failures+=("report generation")
fi
if (( ${#failures[@]} )); then
  printf 'Review %s for incomplete steps:\n' "$run" >&2
  printf '  %s\n' "${failures[@]}" >&2
  exit 1
fi
printf 'Phase 1 complete. Result directory: %s\n' "$run"
