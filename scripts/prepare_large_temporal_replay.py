#!/usr/bin/env python3
"""Prepare a later Stack Overflow replay from the registered SNAP trace."""

import argparse
from datetime import date, datetime, time, timezone
import fcntl
from pathlib import Path
from time import perf_counter

from compare_replay import digest
from revision_experiments import ROOT, load_json, write_json
from run_temporal_windows import (
    DAY, STACK_OVERFLOW, STACK_OVERFLOW_URL, experiment_case, prepare, workload_stats,
)


RAW_SHA256 = "fc04264de1652bbf91c10eb8ec2465df082fc0aec2012e5f4a8edc6400f0d7a3"
DEFAULT_START = date(2015, 9, 3)
DEFAULT_OUTPUT = ROOT / ".build/vldb2027-main3-stackoverflow-w90d-b14d-inputs"


def describe(output, plan, raw=None):
    end = plan["end_utc"]
    (window,) = plan["window_days"]
    projection = prepare(output, raw, end, plan)
    case, checkpoints, _ = experiment_case(
        projection, window, end, dataset="stackoverflow",
        start=plan["start_utc"], step=plan["step_seconds"],
    )
    return workload_stats(case, checkpoints)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--raw", type=Path, default=STACK_OVERFLOW)
    parser.add_argument("--start-date", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--replay-days", type=int, default=28)
    parser.add_argument("--retention-days", type=int, default=90)
    parser.add_argument("--checkpoint-days", type=int, default=14)
    parser.add_argument("--check-only", action="store_true",
                        help="verify the recorded inputs and workload metadata")
    args = parser.parse_args()
    output = args.output.resolve()
    protocol = output / "protocol.json"
    report = output / "workloads.json"

    if args.check_only:
        plan = load_json(protocol)
        if plan["dataset_key"] != "stackoverflow" or plan["raw_sha256"] != RAW_SHA256:
            raise ValueError("saved workload does not use the registered Stack Overflow trace")
        stats = describe(output, plan)
        if load_json(report) != [stats]:
            raise ValueError("saved workload statistics differ from the verified checkpoints")
    else:
        lengths = args.retention_days, args.replay_days, args.checkpoint_days
        if min(lengths) <= 0 or args.replay_days % args.checkpoint_days:
            parser.error("positive lengths and a whole number of checkpoints are required")
        raw = args.raw.resolve()
        if digest(raw) != RAW_SHA256:
            raise ValueError(f"unexpected Stack Overflow source checksum for {raw}")
        start = int(datetime.combine(args.start_date, time.min, timezone.utc).timestamp())
        end = start + args.replay_days * DAY
        plan = {
            "dataset": "Stack Overflow",
            "dataset_key": "stackoverflow",
            "dataset_source": STACK_OVERFLOW_URL,
            "raw": str(raw),
            "raw_sha256": RAW_SHA256,
            "start_utc": start,
            "end_utc": end,
            "projection_end_utc": end,
            "step_seconds": args.checkpoint_days * DAY,
            "window_days": [args.retention_days],
            "edge_window_interval": "(t-w,t]",
            "checkpoint_interval": f"(t-{args.checkpoint_days}d,t]",
        }
        output.mkdir(parents=True, exist_ok=True)
        with (output / ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if protocol.exists() and load_json(protocol) != plan:
                raise ValueError(f"workload options differ from {protocol}, use a fresh output path")
            started = perf_counter()
            stats = describe(output, plan, raw)
            write_json(protocol, plan)
            write_json(report, [stats])
            print(f"Projection and input checks completed in {perf_counter() - started:.1f} s", flush=True)
    print(f"{stats['batches']} verified snapshots, {stats['operations']:,} transitions, "
          f"{stats['mean_active_edges']:,.0f} mean active edges, "
          f"{stats['mean_active_triangles']:,.0f} mean active triangles, "
          f"{stats['cancelled_operation_fraction']:.2%} cancelled operations")
    print(report)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError) as error:
        raise SystemExit(f"ERROR: {error}") from error
