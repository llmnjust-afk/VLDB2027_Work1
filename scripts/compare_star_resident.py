#!/usr/bin/env python3
"""Compare Full and the resident Star port on verified, paired replays."""

import argparse
import csv
import fcntl
import math
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
from time import perf_counter

from compare_replay import check_rows, csv_rows, digest
from revision_experiments import (
    GENERATED, ROOT, input_files, load_json, machine, read_batches, write_json, write_stream,
)

METHODS = ("full", "star-resident")
FIXTURES = {
    "k4-mixed": ("k4.graph", "k4-mixed.stream"),
    "triangle-reinsert": ("triangle.graph", "triangle-reinsert.stream"),
    "triangle-reverse": ("triangle.graph", "triangle-reverse.stream"),
}
SCOPE = ("steady_clock per batch, with ordered net reduction and all update work "
         "through exact labels; initial construction, input, output and oracle excluded")


def run_logged(command, stdout, stderr, *, env):
    write_json(stdout.with_suffix(".argv.json"), [str(arg) for arg in command])
    start = perf_counter()
    with stdout.open("w") as output, stderr.open("w") as errors:
        result = subprocess.run(command, cwd=ROOT, stdout=output, stderr=errors, env=env)
    if result.returncode:
        raise RuntimeError(f"exit {result.returncode}: {command[0]}; see {stdout} and {stderr}")
    return perf_counter() - start


def build(output, star_root, env):
    code = output / "build"
    code.mkdir(exist_ok=True)
    binaries = {method: code / method for method in METHODS}
    source_paths = [
        Path(__file__), ROOT / "scripts/build_star_resident.py",
        ROOT / "scripts/star-source.sha256", ROOT / "src/main.cpp", ROOT / "src/star_resident.cpp",
        *sorted((ROOT / "src").glob("*.h")),
        *sorted(path for path in (ROOT / "baselines/star-resident").rglob("*") if path.is_file()),
    ]
    # Validate the public release even when resuming with existing executables.
    subprocess.run(
        ["sha256sum", "--check", "--quiet", str(ROOT / "scripts/star-source.sha256")],
        cwd=star_root, check=True,
    )
    source_info = {
        "sources": {str(path.relative_to(ROOT)): digest(path) for path in source_paths},
        "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
        "flags": ["-O3", "-march=native", "-std=c++17", "-pthread"],
    }
    manifest = code / "manifest.json"
    if not manifest.exists():
        commands = {
            "full": ["g++", *source_info["flags"], "-o", binaries["full"], "src/main.cpp"],
            "star-resident": [
                "python3", "-B", "scripts/build_star_resident.py",
                "--star-root", star_root, "--output", code, "--cxx", "g++",
            ],
        }
        for name, command in commands.items():
            run_logged(command, code / f"{name}.build.log", code / f"{name}.build.stderr", env=env)
        write_json(manifest, source_info | {
            "executables": {method: digest(path) for method, path in binaries.items()},
        })
    else:
        checks = {method: digest(path) for method, path in binaries.items()}
        if load_json(manifest) != (source_info | {"executables": checks}):
            raise ValueError("different sources or executables in existing run; choose a new --output")
    return binaries


def audit_replay(directory, method, tag, counts, verify, *, threads=1):
    batches = csv_rows(directory / f"{method}.{tag}.batches.csv")
    check_rows(batches, counts, 1, verify)
    runs = csv_rows(directory / f"{method}.{tag}.runs.csv")
    if len(runs) != 1:
        raise ValueError(f"{directory}/{method}.{tag}: expected one independent process")
    run = runs[0]
    expected = "1" if verify else ""
    if (run["threads"], run["run"], run["nbatches"], run["verify_ok"], run["final_m"]) != (
        str(threads), "0", str(len(counts)), expected, batches[-1]["final_m"]
    ):
        raise ValueError(f"{directory}/{method}.{tag}: invalid run summary")
    latencies = [float(row["elapsed_us"]) for row in batches]
    if any(not math.isfinite(latency) or latency < 0 for latency in latencies) or sum(latencies) <= 0:
        raise ValueError(f"{directory}/{method}.{tag}: invalid latency")
    trace = [(int(row["deleted"]), int(row["inserted"]), int(row["final_m"])) for row in batches]
    return trace, statistics.mean(latencies) / 1000


def read_case(directory, case, runs):
    counts = case["batch_operations"]
    trace = None
    means = {}
    for method in METHODS:
        verified, _ = audit_replay(directory, method, "verify", counts, True)
        if trace is not None and verified != trace:
            raise ValueError(f"{directory}: methods disagree on net changes or batch-end edge counts")
        trace = verified
        means[method] = []
        for trial in range(runs):
            timed, mean = audit_replay(directory, method, f"timing.{trial}", counts, False)
            if timed != trace:
                raise ValueError(f"{directory}: {method} timing trial {trial} diverged from verification")
            means[method].append(mean)
    return [
        {
            "dataset": case["dataset"], "scope": case["scope"], "method": method,
            "threads": 1, "batches": len(counts), "operations": sum(counts),
            "replays": runs, "verified_batches": len(counts),
            "mean_ms_per_batch": round(statistics.mean(means[method]), 6),
            "replay_mean_sd_ms": round(statistics.stdev(means[method]), 6) if runs > 1 else "",
            "latency_over_full": round(statistics.mean(means[method]) / statistics.mean(means["full"]), 6),
        }
        for method in METHODS
    ]


def run_case(directory, case, counts, runs, binaries, env):
    success = directory / "complete.json"
    if success.exists():
        if load_json(success)["case"] != case:
            raise ValueError(f"{directory}: existing results use a different input")
        rows = read_case(directory, case, runs)
        print(f"REUSED {case['dataset']} ({len(counts)} verified batches)", flush=True)
        return rows
    if directory.exists():
        failed = directory.parent / "failed"
        failed.mkdir(exist_ok=True)
        archived = Path(tempfile.mkdtemp(prefix=f"{directory.name}.", dir=failed)) / directory.name
        directory.rename(archived)
        print(f"Archived incomplete attempt in {archived}", flush=True)
    directory.mkdir()

    def run(method, tag, verify):
        prefix = directory / f"{method}.{tag}"
        command = [binaries[method]]
        if method == "full":
            command += ["batch", "--ablate", "full"]
        command += [
            "--graph", case["graph"], "--stream", case["stream"], "--dataset", case["dataset"],
            "--threads", "1", "--runs", "1", "--batch-csv", f"{prefix}.batches.csv",
        ]
        command += ["--check-every", "1"] if verify else ["--timing-only"]
        wall_s = run_logged(command, Path(f"{prefix}.runs.csv"), Path(f"{prefix}.stderr"), env=env)
        trace, mean = audit_replay(directory, method, tag, counts, verify)
        print(f"{case['dataset']} {method} {tag}: {len(counts)} batches, "
              f"{wall_s:.2f} s wall; {mean:.3f} ms/batch inside timer", flush=True)
        return trace, wall_s

    started = perf_counter()
    verified = {}
    for method in METHODS:
        verified[method], _ = run(method, "verify", True)
    if verified["full"] != verified["star-resident"]:
        raise ValueError(f"{directory}: verification traces disagree")
    for trial in range(runs):
        order = METHODS if trial % 2 == 0 else METHODS[::-1]
        for method in order:
            trace, _ = run(method, f"timing.{trial}", False)
            if trace != verified[method]:
                raise ValueError(f"{directory}: {method} timing trial {trial} diverged from verification")
    rows = read_case(directory, case, runs)
    wall_s = perf_counter() - started
    write_json(success, {"case": case, "replays": runs, "wall_s": wall_s})
    print(f"VERIFIED {case['dataset']}: {len(counts)} batches, {sum(counts)} operations, "
          f"{runs} timed replays per method; {wall_s:.1f} s wall", flush=True)
    return rows


def cases(datasets, smoke, output):
    paths = {}
    if smoke:
        for dataset, pair in FIXTURES.items():
            paths[dataset] = tuple(ROOT / "tests/fixtures" / filename for filename in pair)
    for dataset in datasets:
        _, graph, stream = input_files(dataset)
        paths[dataset] = graph, stream
    for dataset, (graph, stream) in paths.items():
        batches = read_batches(stream)
        scope = "full"
        if dataset in FIXTURES:
            scope = "fixture"
        elif smoke:
            batches = batches[:3]
            stream = output / "inputs" / f"{dataset}.first-3.stream"
            write_stream(stream, batches)
            scope = "prefix-3"
        yield {
            "dataset": dataset, "scope": scope, "graph": str(graph), "stream": str(stream),
            "graph_sha256": digest(graph), "stream_sha256": digest(stream),
            "batch_operations": [len(batch) for batch in batches],
        }


def write_summary(output, rows, *, filename="summary.csv"):
    temporary = output / f"{filename}.tmp"
    with temporary.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(output / filename)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--datasets", choices=GENERATED, nargs="+", default=GENERATED)
    parser.add_argument("--runs", type=int, help="independent timed replays per method (default: 3; smoke: 1)")
    parser.add_argument("--smoke-only", action="store_true", help="use fixtures and three complete batches per graph")
    parser.add_argument("--output", type=Path, help="experiment directory; reused only with identical protocol")
    parser.add_argument("--star-root", type=Path, default=ROOT / ".build/TrussMaintenance")
    parser.add_argument("--cpu", type=int, help="pin both methods to this allowed CPU (default: first allowed)")
    parser.add_argument("--check-only", action="store_true", help="audit and summarize a saved experiment directory")
    args = parser.parse_args()
    runs = args.runs if args.runs is not None else (1 if args.smoke_only else 3)
    if runs < 1 or len(set(args.datasets)) != len(args.datasets):
        parser.error("use positive --runs and distinct --datasets")
    if args.check_only and args.output is None:
        parser.error("--check-only requires --output")
    default = "vldb2027-star-resident-smoke" if args.smoke_only else "vldb2027-star-resident"
    output = (args.output or ROOT / ".build" / default).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    protocol = output / "protocol.json"
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.check_only:
            plan = load_json(protocol)
            rows = []
            for case in plan["cases"]:
                directory = output / case["dataset"]
                complete = load_json(directory / "complete.json")
                if complete["case"] != case or complete["replays"] != plan["replays"]:
                    raise ValueError(f"{directory}: completion record disagrees with experiment protocol")
                rows.extend(read_case(directory, case, plan["replays"]))
                print(f"VERIFIED {case['dataset']}: {len(case['batch_operations'])} batches", flush=True)
        else:
            identity, hardware = machine()
            allowed = identity["affinity"]
            cpu = allowed[0] if args.cpu is None else args.cpu
            if cpu not in allowed:
                parser.error(f"CPU {cpu} is outside the allowed affinity: {allowed}")
            os.sched_setaffinity(0, {cpu})
            env = os.environ | {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
            print(f"Run directory: {output} | {identity['host']} | {identity['cpu_model']} | CPU {cpu}", flush=True)
            plan = {
                "machine": {key: value for key, value in identity.items() if key != "affinity"},
                "cpu": cpu, "threads": 1, "replays": runs, "test_mode": args.smoke_only,
                "timer_scope": SCOPE, "star_basis": "hash-pinned released Star insert/delete kernels",
                "cases": list(cases(args.datasets, args.smoke_only, output)),
            }
            if protocol.exists() and load_json(protocol) != plan:
                raise ValueError("different inputs, runs, host or CPU in existing output; choose a new --output")
            write_json(protocol, plan)
            (output / "hardware.txt").write_text(hardware)
            binaries = build(output, args.star_root.resolve(strict=True), env)
            rows = []
            for case in plan["cases"]:
                rows.extend(run_case(output / case["dataset"], case, case["batch_operations"],
                                     runs, binaries, env))
        write_summary(output, rows)
    print(f"REPORT {output / 'summary.csv'}", flush=True)
    print(f"Star resident comparison complete: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
