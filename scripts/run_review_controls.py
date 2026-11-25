#!/usr/bin/env python3
"""Compare ordered Static, Static-Net and Full with independent replay verification."""

import argparse
import fcntl
import math
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess

from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, cases, run_logged, write_summary
from experiment_replays import audit_paired_case, run_paired_case, run_replay
from revision_experiments import GENERATED, ROOT, TEMPORAL, load_json, machine, write_json


DATASETS = (*GENERATED, *TEMPORAL)
METHODS = ("full", "static", "static-net")
CERTIFIED = "full-certified"
FUSED = "full-fused"
DIAGNOSTIC_TAG = "peel-scans"
FLAGS = ("-O3", "-march=native", "-std=c++17", "-pthread")


def source_fingerprint():
    paths = [
        Path(__file__), ROOT / "src/main.cpp", *sorted((ROOT / "src").glob("*.h")),
        *(ROOT / "scripts" / name for name in (
            "compare_replay.py", "compare_star_resident.py", "experiment_replays.py",
            "revision_experiments.py",
        )),
    ]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def setup(output, args, environment):
    path = output / "protocol.json"
    binary = output / "build" / "batchtruss"
    if args.check_only:
        return load_json(path), binary

    identity, hardware = machine()
    allowed = identity["affinity"]
    cpu = allowed[0] if args.cpu is None else args.cpu
    if cpu not in allowed:
        raise ValueError(f"CPU {cpu} is outside allowed affinity {allowed}")
    os.sched_setaffinity(0, {cpu})
    runs = args.runs if args.runs is not None else (1 if args.smoke_only else 3)
    methods = list(METHODS)
    if args.certified:
        methods.append(CERTIFIED)
    if args.fused:
        methods.append(FUSED)
    plan = {
        "methods": methods,
        "timing_replays": runs,
        "test_mode": args.smoke_only,
        "affinity": [cpu],
        "machine": identity,
        "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
        "flags": list(FLAGS),
        "sources": source_fingerprint(),
        "cases": list(cases(args.datasets, args.smoke_only, output)),
        "timer_scope": "Per-batch ordered or net graph updates and label maintenance. "
                       "Graph loading, startup, output, and independent checks excluded.",
        "method_order": "Rotate each fresh-process timing replay.",
    }
    if path.exists():
        saved = load_json(path)
        if {key: value for key, value in saved.items() if key != "binary_sha256"} != plan:
            raise ValueError("experiment protocol or build inputs changed; choose a new --output")
        return saved, binary

    binary.parent.mkdir(exist_ok=True)
    staged = binary.with_suffix(".tmp")
    run_logged(["g++", *FLAGS, "-o", staged, ROOT / "src/main.cpp"],
               binary.parent / "build.stdout", binary.parent / "build.stderr", env=environment)
    staged.replace(binary)
    (output / "hardware.txt").write_text(hardware)
    plan["binary_sha256"] = digest(binary)
    write_json(path, plan)
    print(f"Run directory: {output} | {identity['host']} | CPU {cpu}", flush=True)
    return plan, binary


def summarize(directory, case, methods, runs):
    rows = []
    for method in methods:
        trials = []
        updates, kernel = [], []
        deletions, insertions = 0, 0
        for trial in range(runs):
            source = csv_rows(directory / f"{method}.timing.{trial}.batches.csv")
            trials.append([float(row["elapsed_us"]) / 1000 for row in source])
            if trial == 0:
                deletions = sum(int(row["deleted"]) for row in source)
                insertions = sum(int(row["inserted"]) for row in source)
            if method in ("static", "static-net"):
                for row in source:
                    elapsed, peel = float(row["elapsed_us"]), float(row["kernel_us"])
                    if not 0 <= peel <= elapsed:
                        raise ValueError(f"{directory}/{method}: invalid static kernel duration")
                    updates.append((elapsed - peel) / 1000)
                    kernel.append(peel / 1000)
        latencies = sorted(value for trial in trials for value in trial)
        rows.append({
            "dataset": case["dataset"], "scope": case["scope"], "method": method,
            "threads": 1, "batches": len(case["batch_operations"]),
            "raw_operations": sum(case["batch_operations"]),
            "applied_deletions": deletions, "applied_insertions": insertions,
            "replays": runs, "mean_ms_per_batch": mean(latencies),
            "replay_mean_sd_ms": stdev(mean(trial) for trial in trials) if runs > 1 else "",
            "pooled_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1],
            "mean_updates_ms": mean(updates) if updates else "",
            "mean_full_graph_kernel_ms": mean(kernel) if kernel else "",
        })
    for row in rows:
        row["latency_over_full"] = row["mean_ms_per_batch"] / rows[0]["mean_ms_per_batch"]
    return rows


def peel_scan_diagnostics(directory, case, methods, verified, binaries, environment, audit_only):
    completion = directory / "peel-scans.complete.json"
    if not audit_only and not completion.exists():
        for method in methods:
            prefix = directory / f"{method}.{DIAGNOSTIC_TAG}"
            run_replay(directory, case, method, DIAGNOSTIC_TAG, binaries, environment,
                       extra_flags=("--peel-scan-csv", f"{prefix}.counts.csv",
                                    "--phase-csv", f"{prefix}.phases.csv"))
    elif load_json(completion) != {"methods": list(methods)}:
        raise ValueError(f"{directory}: invalid peel diagnostic manifest")

    expected = [(0, batch) for batch in range(1, len(case["batch_operations"]) + 1)]
    totals = []
    baseline = None
    for method in methods:
        trace, _ = audit_replay(directory, method, DIAGNOSTIC_TAG, case["batch_operations"], False)
        if trace != verified[method]:
            raise ValueError(f"{directory}: {method} peel diagnostic diverged from verification")
        prefix = directory / f"{method}.{DIAGNOSTIC_TAG}"
        rows = csv_rows(Path(f"{prefix}.counts.csv"))
        replay = csv_rows(Path(f"{prefix}.batches.csv"))
        phases = csv_rows(Path(f"{prefix}.phases.csv"))
        for table in (rows, phases):
            if [(int(row["run"]), int(row["batch"])) for row in table] != expected:
                raise ValueError(f"{prefix}: incomplete diagnostics")
        values = [tuple(int(row[key]) for key in (
            "region_edges", "certified_edges", "scanned_edges", "scan_degree_sum"
        )) for row in rows]
        for i, (region, certified, scanned, degree_sum) in enumerate(values):
            if (region != int(replay[i]["region_edges"]) or min(certified, scanned, degree_sum) < 0
                    or region != certified + scanned or (method != CERTIFIED and certified)
                    or (method == FUSED and (float(phases[i]["recount_us"]) or degree_sum != baseline[i][3]))
                    or (baseline is not None and (region != baseline[i][0]
                                                  or degree_sum > baseline[i][3]))):
                raise ValueError(f"{prefix}: invalid peel scan counts in batch {i + 1}")
        if baseline is None:
            baseline = values
        region, certified, scanned, degree_sum = map(sum, zip(*values))
        region_passes = 1 if method == FUSED else 2
        totals.append({
            "dataset": case["dataset"], "scope": case["scope"], "method": method,
            "batches": len(values), "region_edge_visits": region, "certified_edge_visits": certified,
            "serial_peel_edge_scans": scanned, "serial_peel_scan_degree_sum": degree_sum,
            # The baseline enumerates each region edge once during its serial peel.
            # Its measured degree sum is therefore the mass of one complete region pass.
            "region_stage_edge_scans": region_passes * region + scanned,
            "region_stage_scan_degree_sum": region_passes * sum(v[3] for v in baseline) + degree_sum,
            "diagnostic_mean_closure_recount_ms": mean(
                (float(row["closure_us"]) + float(row["recount_us"])) / 1000 for row in phases),
            "diagnostic_mean_peel_ms": mean(float(row["peel_us"]) / 1000 for row in phases),
        })
    if not audit_only and not completion.exists():
        write_json(completion, {"methods": list(methods)})
    return totals


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true", help="fixtures and first three batches")
    mode.add_argument("--check-only", action="store_true", help="audit saved inputs and replay results")
    parser.add_argument("--datasets", nargs="+", choices=DATASETS, default=DATASETS)
    parser.add_argument("--runs", type=int, help="fresh-process timing replays (default 3; smoke 1)")
    parser.add_argument("--certified", action="store_true",
                        help="include the exact low-support variant and separate peel diagnostics")
    parser.add_argument("--fused", action="store_true",
                        help="include fused closure/recount and separate scan diagnostics")
    parser.add_argument("--cpu", type=int, help="logical CPU (default is first allowed)")
    parser.add_argument("--output", type=Path, help="directory for resumable outputs")
    args = parser.parse_args()
    if (args.runs is not None and args.runs < 1) or len(set(args.datasets)) != len(args.datasets):
        parser.error("use positive --runs and distinct --datasets")
    if args.check_only and args.output is None:
        parser.error("--check-only requires --output")
    name = "vldb2027-review-controls-smoke" if args.smoke_only else "vldb2027-review-controls"
    output = (args.output or ROOT / ".build" / name).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, binary = setup(output, args, environment)
        if digest(binary) != plan["binary_sha256"]:
            raise ValueError("executable does not match the saved experiment protocol")
        methods, runs = tuple(plan["methods"]), plan["timing_replays"]
        if methods not in (METHODS, (*METHODS, CERTIFIED), (*METHODS, FUSED),
                           (*METHODS, CERTIFIED, FUSED)) or runs < 1:
            raise ValueError("unexpected saved methods or replay count")
        binaries = {"full": binary}
        rows, diagnostics = [], []
        diagnostic_methods = ["full", *methods[len(METHODS):]]
        for case in plan["cases"]:
            for name in ("graph", "stream"):
                if digest(Path(case[name])) != case[f"{name}_sha256"]:
                    raise ValueError(f"{case['dataset']}: {name} has changed")
            directory = output / case["dataset"]
            if args.check_only:
                verified = audit_paired_case(directory, case, methods, runs)
                print(f"VERIFIED {case['dataset']}", flush=True)
            else:
                verified = run_paired_case(directory, case, methods, runs, binaries, environment)
            rows.extend(summarize(directory, case, methods, runs))
            if len(diagnostic_methods) > 1:
                diagnostics.extend(peel_scan_diagnostics(
                    directory, case, diagnostic_methods, verified, binaries, environment,
                    args.check_only))
        write_summary(output, rows)
        if diagnostics:
            write_summary(output, diagnostics, filename="peel-scan-summary.csv")
    print(f"Review control benchmark complete: {output / 'summary.csv'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
