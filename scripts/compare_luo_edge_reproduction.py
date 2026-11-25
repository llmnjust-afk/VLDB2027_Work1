#!/usr/bin/env python3
"""Audit and compare Full with the independent edge-only Luo reproduction."""

import argparse
import fcntl
import math
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess

from compare_replay import csv_rows, digest
from compare_star_resident import run_logged, write_summary
from experiment_replays import audit_paired_case, run_paired_case
from revision_experiments import ROOT, load_json, machine, read_batches, write_json, write_stream


METHODS = ("full", "luo-edge")
SOURCES = {"full": "src/main.cpp", "luo-edge": "src/luo_edge_replay.cpp"}
FLAGS = ("-O3", "-march=native", "-std=c++17", "-pthread")
PAPER = "10.1109/TC.2022.3174594"
SMOKE_BATCHES = 3
TIMER_SCOPE = (
    "steady_clock per batch, including ordered net reduction, update grouping and exact "
    "label maintenance; graph/stream loading, initial construction, verification and output excluded"
)


def prepare_case(args, output, threads):
    graph, source = args.graph.resolve(strict=True), args.stream.resolve(strict=True)
    batches = read_batches(source)
    stream = source
    if args.smoke_only:
        batches = batches[:SMOKE_BATCHES]
        stream = output / "inputs" / "smoke.stream"
        write_stream(stream, batches)
    stream_hash = digest(stream)
    return {
        "dataset": args.dataset,
        "scope": "prefix" if args.smoke_only else "full",
        "graph": str(graph), "graph_sha256": digest(graph),
        "stream": str(stream), "stream_sha256": stream_hash,
        "source_stream": str(source), "source_stream_sha256": stream_hash if source == stream else digest(source),
        "batch_operations": [len(batch) for batch in batches], "threads": threads,
    }


def source_hashes():
    paths = [
        Path(__file__).resolve(), ROOT / "scripts/compare_replay.py",
        ROOT / "scripts/compare_star_resident.py", ROOT / "scripts/experiment_replays.py",
        ROOT / "scripts/revision_experiments.py",
        *(ROOT / name for name in SOURCES.values()),
        *sorted((ROOT / "src").glob("*.h")),
    ]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def configure(args, output, protocol, binaries, replays, threads):
    identity, hardware = machine()
    allowed = identity["affinity"]
    start = allowed[0] if args.cpu is None else args.cpu
    if start not in allowed:
        raise ValueError(f"CPU {start} is outside the allowed affinity {allowed}")
    offset = allowed.index(start)
    cpus = allowed[offset:offset + threads]
    if len(cpus) != threads or threads > identity["thread_budget"]:
        raise ValueError(f"{threads} threads exceed the available CPU budget from CPU {start}")
    os.sched_setaffinity(0, set(cpus))
    env = {key: val for key, val in os.environ.items() if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
    updates = {"LC_ALL": "C", "OMP_NUM_THREADS": str(threads), "OMP_DYNAMIC": "FALSE"}
    env.update(updates)
    identity["runtime_environment"] = {
        key: value for key, value in identity["runtime_environment"].items() if key in env
    } | updates
    plan = {
        "citation_doi": PAPER,
        "implementation": "independent edge-only C++ reproduction of Luo et al.",
        "case": prepare_case(args, output, threads),
        "machine": identity, "affinity": cpus,
        "methods": list(METHODS), "timing_replays": replays,
        "timer_scope": TIMER_SCOPE, "method_order": "rotate each independent timing replay",
        "build": {
            "sources": source_hashes(), "flags": list(FLAGS),
            "compiler": subprocess.check_output(["g++", "--version"], text=True, env=env).splitlines()[0],
        },
    }
    print(f"Run directory: {output} | {identity['host']} | CPUs {cpus}", flush=True)
    if protocol.is_file():
        saved = load_json(protocol)
        if plan != {key: value for key, value in saved.items() if key != "executable_sha256"}:
            raise ValueError("inputs, sources or run protocol changed; choose a new --output")
        return saved, env

    (output / "hardware.txt").write_text(hardware)
    (output / "build").mkdir(exist_ok=True)
    for method, source in SOURCES.items():
        command = ["g++", *FLAGS, source, "-o", binaries[method]]
        run_logged(command, output / "build" / f"{method}.log",
                   output / "build" / f"{method}.stderr", env=env)
    if source_hashes() != plan["build"]["sources"]:
        raise ValueError("source files changed during the build; retry once edits finish")
    plan["executable_sha256"] = {method: digest(path) for method, path in binaries.items()}
    write_json(protocol, plan)
    return plan, env


def audit_inputs(plan, binaries):
    if plan["citation_doi"] != PAPER or plan["methods"] != list(METHODS):
        raise ValueError("saved methods or citation do not match the Luo experiment")
    case = plan["case"]
    replays = plan["timing_replays"]
    if replays < 0 or case["threads"] < 1 or not case["batch_operations"]:
        raise ValueError("saved protocol has invalid thread, batch or replay counts")
    for name in ("graph", "stream"):
        if digest(Path(case[name])) != case[f"{name}_sha256"]:
            raise ValueError(f"{name} differs from the saved input checksum")
    if {method: digest(path) for method, path in binaries.items()} != plan["executable_sha256"]:
        raise ValueError("replay executable checksum differs from the saved build")


def summarize(directory, case, replays):
    rows = []
    for method in METHODS:
        trials, builds, peaks = [], [], []
        for trial in range(replays):
            tag = f"{method}.timing.{trial}"
            trials.append([float(row["elapsed_us"]) / 1e3
                           for row in csv_rows(directory / f"{tag}.batches.csv")])
            run, = csv_rows(directory / f"{tag}.runs.csv")
            build, rss = float(run["build_s"]), int(run["rss_mb"])
            if not math.isfinite(build) or build < 0 or rss <= 0:
                raise ValueError(f"{directory}/{tag}: invalid construction time or peak RSS")
            builds.append(build)
            peaks.append(rss)
        samples = sorted(latency for trial in trials for latency in trial)
        rows.append({
            "dataset": case["dataset"], "scope": case["scope"], "method": method,
            "threads": case["threads"], "batches": len(case["batch_operations"]),
            "operations": sum(case["batch_operations"]), "replays": replays,
            "verified_batches": len(case["batch_operations"]),
            "mean_ms_per_batch": mean(samples),
            "replay_mean_sd_ms": stdev(mean(trial) for trial in trials) if replays > 1 else "",
            "pooled_p95_ms": samples[math.ceil(len(samples) * .95) - 1],
            "mean_maintenance_s": mean(sum(trial) for trial in trials) / 1e3,
            "mean_build_s": mean(builds), "peak_process_rss_mib": max(peaks),
        })
    for row in rows:
        row["latency_over_full"] = row["mean_ms_per_batch"] / rows[0]["mean_ms_per_batch"]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true", help="run at most three complete batches")
    mode.add_argument("--verify-only", action="store_true", help="verify every batch without collecting timings")
    mode.add_argument("--check-only", action="store_true", help="audit an existing run and regenerate its summary")
    parser.add_argument("--graph", type=Path, help="initial graph in the native replay format")
    parser.add_argument("--stream", type=Path, help="ordered batches in the native replay format")
    parser.add_argument("--dataset", help="workload label")
    parser.add_argument("--output", type=Path, required=True, help="directory for logs, build and summary")
    parser.add_argument("--threads", type=int, help="matched thread budget (default 1)")
    parser.add_argument("--cpu", type=int, help="first allowed CPU to use (default: first in affinity)")
    parser.add_argument("--runs", type=int, help="independent timing replays (default 3, smoke default 1)")
    args = parser.parse_args()

    if args.check_only:
        if any(value is not None for value in
               (args.graph, args.stream, args.dataset, args.threads, args.cpu, args.runs)):
            parser.error("--check-only reads inputs, CPUs and repetitions from the saved protocol")
    elif not args.graph or not args.stream or not args.dataset or any(c in args.dataset for c in ",\r\n"):
        parser.error("provide --graph, --stream and a nonempty --dataset without CSV separators")
    if args.runs is not None and (args.runs < 1 or args.verify_only):
        parser.error("--runs must be positive and is only used for timing")
    if args.threads is not None and args.threads < 1:
        parser.error("--threads must be positive")
    replays = 0 if args.verify_only else (args.runs or (1 if args.smoke_only else 3))
    threads = args.threads or 1

    output = args.output.resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    protocol = output / "protocol.json"
    binaries = {method: output / "build" / method for method in METHODS}
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.check_only:
            plan = load_json(protocol)
        else:
            plan, env = configure(args, output, protocol, binaries, replays, threads)
        audit_inputs(plan, binaries)
        directory = output / "replay"
        case, replays = plan["case"], plan["timing_replays"]
        if not args.check_only:
            run_paired_case(directory, case, METHODS, replays, binaries, env)
        audit_paired_case(directory, case, METHODS, replays)
        if replays:
            write_summary(output, summarize(directory, case, replays))
            print(f"REPORT {output / 'summary.csv'}", flush=True)
        print(f"AUDIT OK {case['dataset']}: {len(case['batch_operations'])} checkpoints", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
