#!/usr/bin/env python3
"""Verify and compare Full, Static, and MSP with matched thread budgets."""

import argparse
import fcntl
import math
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess
import sys

from compare_replay import csv_rows, digest
from compare_star_resident import write_summary
from experiment_replays import audit_paired_case, run_paired_case
from revision_experiments import (
    LATENCY_CLOCK, LATENCY_SCOPE, ROOT, build_binary, input_files, load_json,
    machine, read_batches, write_json, write_stream,
)


DATASETS = ("amazon", "wikivote")
THREADS = (1, 4, 8)
METHODS = ("full", "static", "msp")
PILOT_BATCHES = 3


def prepare_cases(output, smoke):
    cases = {}
    for dataset in DATASETS:
        label, graph, stream = input_files(dataset)
        batches = read_batches(stream)
        if [len(batch) for batch in batches] != [1000] * 200:
            raise ValueError(f"{dataset}: expected 200 registered batches of 1,000 events")
        if smoke:
            batches = batches[:PILOT_BATCHES]
            stream = output / "inputs" / f"{dataset}.stream"
            write_stream(stream, batches)
        input_data = {
            "dataset": label, "graph": str(graph), "stream": str(stream),
            "graph_sha256": digest(graph), "stream_sha256": digest(stream),
            "batch_operations": [len(batch) for batch in batches],
        }
        for threads in THREADS:
            cases[f"{dataset}-t{threads}"] = input_data | {"threads": threads}
    return cases


def verify_plan(output, plan):
    names = [f"{dataset}-t{threads}" for dataset in DATASETS for threads in THREADS]
    if (list(plan["cases"]) != names or plan["methods"] != list(METHODS)
            or plan["thread_budgets"] != list(THREADS)
            or len(set(plan["affinity"])) != max(THREADS)
            or plan["timing_replays"] < 1):
        raise ValueError("run protocol does not match the configured experiment")
    binary = output / "build" / "batchtruss-msp"
    build = load_json(output / "build-manifest.json")
    if plan["binary_sha256"] != build["binary_sha256"] or digest(binary) != plan["binary_sha256"]:
        raise ValueError("binary SHA-256 does not match the run protocol")
    for dataset in DATASETS:
        reference = plan["cases"][f"{dataset}-t1"]
        target_batches = PILOT_BATCHES if plan["test_mode"] else 200
        if reference["dataset"] != dataset or reference["batch_operations"] != [1000] * target_batches:
            raise ValueError(f"{dataset}: unexpected dataset or stream length")
        for name in ("graph", "stream"):
            if digest(Path(reference[name])) != reference[f"{name}_sha256"]:
                raise ValueError(f"{dataset}: {name} SHA-256 does not match the run protocol")
        for threads in THREADS:
            if plan["cases"][f"{dataset}-t{threads}"] != (reference | {"threads": threads}):
                raise ValueError(f"{dataset}: input differs between thread budgets")
    return binary


def summarize(directory, case, replays):
    result = []
    for method in METHODS:
        trials, build, rss = [], [], []
        for trial in range(replays):
            tag = f"{method}.timing.{trial}"
            batches = csv_rows(directory / f"{tag}.batches.csv")
            trials.append([float(row["elapsed_us"]) / 1000 for row in batches])
            run, = csv_rows(directory / f"{tag}.runs.csv")
            build.append(float(run["build_s"]))
            rss.append(int(run["rss_mb"]))
        latencies = sorted(sample for trial in trials for sample in trial)
        result.append({
            "dataset": case["dataset"], "threads": case["threads"], "method": method,
            "batches": len(case["batch_operations"]), "operations": sum(case["batch_operations"]),
            "replays": replays, "mean_ms_per_batch": mean(latencies),
            "replay_mean_sd_ms": stdev(mean(trial) for trial in trials) if replays > 1 else "",
            "pooled_p95_ms": latencies[math.ceil(len(latencies) * .95) - 1],
            "mean_build_s": mean(build), "peak_process_rss_mib": max(rss),
        })
    for row in result:
        row["latency_over_full"] = row["mean_ms_per_batch"] / result[0]["mean_ms_per_batch"]
    return result


def run_all(output, plan, binary, audit_only, runtime):
    replays = plan["timing_replays"]
    status = {name: {"state": "pending"} for name in plan["cases"]}
    status_path = output / "status.json"
    rows = []
    for name, case in plan["cases"].items():
        directory = output / name
        try:
            if audit_only:
                audit_paired_case(directory, case, METHODS, replays)
                print(f"VERIFIED {name}: {len(case['batch_operations'])} batches", flush=True)
            else:
                status[name] = {"state": "running"}
                write_json(status_path, status)
                threads = str(case["threads"])
                env = runtime | {"OMP_NUM_THREADS": threads, "OMP_THREAD_LIMIT": threads}
                run_paired_case(directory, case, METHODS, replays, {"full": binary}, env)
            rows.extend(summarize(directory, case, replays))
            status[name] = {"state": "verified"}
        except (OSError, KeyError, ValueError, RuntimeError) as error:
            status[name] = {"state": "failed", "error": str(error)}
            print(f"FAILED {name}: {error}", file=sys.stderr, flush=True)
        write_json(status_path, status)
    if any(config["state"] != "verified" for config in status.values()):
        raise ValueError(f"incomplete experiment; see {status_path}")
    one_thread = {(row["dataset"], row["method"]): row["mean_ms_per_batch"]
                  for row in rows if row["threads"] == 1}
    for row in rows:
        row["speedup_vs_one_thread"] = one_thread[row["dataset"], row["method"]] / row["mean_ms_per_batch"]
    write_summary(output, rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true",
                      help=f"run only the first {PILOT_BATCHES} registered batches per configuration")
    mode.add_argument("--check-only", action="store_true", help="audit existing outputs and regenerate summary")
    parser.add_argument("--runs", type=int, help="fresh process timing replays (default 3; smoke 1)")
    parser.add_argument("--output", type=Path, help="run directory for compatible resumes and audit")
    args = parser.parse_args()
    if args.check_only and args.runs is not None:
        parser.error("--check-only reads the replay count from the saved protocol")
    runs = args.runs if args.runs is not None else (1 if args.smoke_only else 3)
    if runs < 1:
        parser.error("--runs must be positive")

    name = "vldb2027-matched-threads-smoke" if args.smoke_only else "vldb2027-matched-threads"
    output = (args.output or ROOT / ".build" / name).resolve()
    runtime = {}
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        path = output / "protocol.json"
        if args.check_only:
            plan = load_json(path)
        else:
            identity, hardware = machine()
            if identity["thread_budget"] < max(THREADS):
                raise ValueError(f"1, 4, and 8 threads require 8 CPUs; effective budget is {identity['thread_budget']}")
            cpus = identity["affinity"][:max(THREADS)]
            os.sched_setaffinity(0, set(cpus))
            runtime = {key: value for key, value in os.environ.items()
                       if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
            omp = {"OMP_DYNAMIC": "FALSE", "OMP_PROC_BIND": "FALSE"}
            runtime.update(omp)
            recorded = {key: value for key, value in identity["runtime_environment"].items()
                        if key in runtime}
            identity["runtime_environment"] = recorded | omp
            plan = {
                "machine": identity, "affinity": cpus, "thread_budgets": list(THREADS),
                "methods": list(METHODS), "timing_replays": runs, "test_mode": args.smoke_only,
                "separate_timing_processes": True, "method_order": "rotate each timing replay",
                "clock": LATENCY_CLOCK, "timer_scope": LATENCY_SCOPE,
                "cases": prepare_cases(output, args.smoke_only),
                "runner_sha256": digest(Path(__file__)),
            }
            saved = load_json(path) if path.exists() else None
            if saved and plan != {key: value for key, value in saved.items() if key != "binary_sha256"}:
                raise ValueError("run protocol changed; choose a new --output")
            print(f"Run directory: {output} | {identity['host']} | CPUs {cpus} | "
                  f"quota {identity['cpu_quota']} | memory limit {identity['memory_limit_bytes']}", flush=True)
            (output / "hardware.txt").write_text(hardware)
            binary = build_binary(output, max(THREADS), identity)
            plan["binary_sha256"] = digest(binary)
            if saved and saved != plan:
                raise ValueError("built executable differs from the saved protocol")
            write_json(path, plan)
        binary = verify_plan(output, plan)
        run_all(output, plan, binary, args.check_only, runtime)
    print(f"Matched-thread benchmark complete: {output / 'summary.csv'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
