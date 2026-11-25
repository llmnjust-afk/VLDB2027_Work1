#!/usr/bin/env python3
"""Validate full replays before collecting untimed-oracle-free latency trials."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys


def digest(path):
    with path.open("rb") as file:
        hasher = hashlib.sha256()
        for block in iter(lambda: file.read(1 << 20), b""):
            hasher.update(block)
        return hasher.hexdigest()


def csv_rows(path):
    with path.open() as file:
        return list(csv.DictReader(file))


def check_rows(rows, counts, runs, verified):
    expected = [(run, batch, count) for run in range(runs)
                for batch, count in enumerate(counts, 1)]
    actual = [(int(row["run"]), int(row["batch"]), int(row["raw_ops"])) for row in rows]
    if actual != expected or any(row["verify_ok"] != ("1" if verified else "") for row in rows):
        raise ValueError("incomplete run, mismatched batch boundaries or invalid verification status")


def cpu_info():
    try:
        return subprocess.check_output(["lscpu"], text=True)
    except (OSError, subprocess.CalledProcessError):
        return platform.platform()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--stream", type=Path, required=True)
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=Path("batchtruss-msp"))
    parser.add_argument("--threads", type=int, default=4)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--separate-timing-processes", action="store_true",
                        help="start a fresh process from the initial graph for each timing replay")
    parser.add_argument("--methods", nargs="+", default=["full", "nomerge", "peredge", "static", "msp"],
                        choices=["full", "nomerge", "peredge", "static", "static-net", "msp"])
    args = parser.parse_args()
    if args.threads < 1 or args.runs < 1 or len(set(args.methods)) != len(args.methods):
        parser.error("use positive threads/runs and distinct methods")
    if "full" not in args.methods:
        parser.error("include full to anchor the comparison")

    graph, stream, binary = [path.resolve(strict=True) for path in
                             (args.graph, args.stream, args.binary)]
    counts = []
    with stream.open() as file:
        for line in file:
            fields = line.split()
            if fields and fields[0].lower() == "b":
                counts.append(int(fields[1]))
    if not counts:
        parser.error("benchmark needs at least one batch")

    out = args.output
    out.mkdir(parents=True)
    metadata = {
        "dataset": args.dataset,
        "inputs": {str(path): digest(path) for path in (graph, stream, binary)},
        "platform": platform.platform(),
        "affinity": sorted(os.sched_getaffinity(0)),
        "threads_requested": args.threads,
        "runs": args.runs,
        "separate_timing_processes": args.separate_timing_processes,
        "batch_count": len(counts),
        "operations": sum(counts),
        "methods": args.methods,
        "command": sys.argv,
        "clock": "steady_clock; update application and label maintenance included",
        "build": "separate; graph/stream loading, checks and output excluded from update latency",
    }
    (out / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (out / "hardware.txt").write_text(cpu_info())

    def run_command(name, tag, verify, repetitions):
        method = "batch" if name in {"full", "nomerge"} else name
        threads = 1 if name == "peredge" else args.threads
        raw = out / f"{name}.{tag}.batches.csv"
        summary = out / f"{name}.{tag}.runs.csv"
        command = [str(binary), method, "--graph", str(graph), "--stream", str(stream),
                   "--dataset", args.dataset, "--threads", str(threads),
                   "--runs", str(repetitions), "--batch-csv", str(raw)]
        if method == "batch":
            command += ["--ablate", name]
        command += ["--check-every", "1"] if verify else ["--timing-only"]
        (out / f"{name}.{tag}.argv.json").write_text(json.dumps(command, indent=2))
        with summary.open("w") as stdout, (out / f"{name}.{tag}.stderr").open("w") as stderr:
            subprocess.run(command, stdout=stdout, stderr=stderr, check=True)
        rows = csv_rows(raw)
        check_rows(rows, counts, repetitions, verify)
        return rows, csv_rows(summary)

    def run(name, tag, verify):
        if verify or not args.separate_timing_processes:
            return run_command(name, tag, verify, 1 if verify else args.runs)
        rows, builds = [], []
        for repetition in range(args.runs):
            batch_rows, build_rows = run_command(name, f"{tag}.{repetition}", False, 1)
            for row in batch_rows + build_rows:
                row["run"] = str(repetition)
            rows.extend(batch_rows)
            builds.extend(build_rows)
        for kind, records in (("batches", rows), ("runs", builds)):
            with (out / f"{name}.{tag}.{kind}.csv").open("w", newline="") as file:
                writer = csv.DictWriter(file, fieldnames=records[0])
                writer.writeheader()
                writer.writerows(records)
        return rows, builds

    checkpoints = None
    for method in args.methods:
        rows, _ = run(method, "verify", True)
        sizes = [int(row["final_m"]) for row in rows]
        if checkpoints is None:
            checkpoints = sizes
        if sizes != checkpoints:
            raise ValueError(f"verified graphs disagree for {method}")
        print(f"verified {method}: {len(sizes)} batches, {sum(counts)} operations", flush=True)

    stats = []
    for method in args.methods:
        rows, builds = run(method, "timing", False)
        if [int(row["final_m"]) for row in rows] != checkpoints * args.runs:
            raise ValueError(f"timed replay diverged from verified graph-size trace for {method}")
        times = sorted(float(row["elapsed_us"]) for row in rows)
        stats.append({"dataset": args.dataset, "method": method, "threads": builds[0]["threads"],
                      "runs": args.runs, "batches": len(counts), "ops": sum(counts),
                      "mean_ms": statistics.mean(times) / 1000,
                      "p50_ms": statistics.median(times) / 1000,
                      "p99_ms": times[min(len(times) - 1, len(times) * 99 // 100)] / 1000,
                      "build_s": statistics.mean(float(row["build_s"]) for row in builds),
                      "rss_peak_mb": max(int(row["rss_mb"]) for row in builds)})
        print(f"timed {method}: {len(rows)} recorded batches", flush=True)
    for row in stats:
        row["time_over_full"] = row["mean_ms"] / stats[args.methods.index("full")]["mean_ms"]
    with (out / "summary.csv").open("w") as file:
        writer = csv.DictWriter(file, fieldnames=stats[0])
        writer.writeheader()
        writer.writerows(stats)
    (out / "complete").write_text("Every replay verified before timing.\n")


if __name__ == "__main__":
    main()
