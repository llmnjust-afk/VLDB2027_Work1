#!/usr/bin/env python3
"""Measure update locality and profile Full and resident Star by stage."""

import argparse
import csv
import fcntl
import math
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess

from compare_replay import csv_rows, digest
from compare_star_resident import METHODS, SCOPE, audit_replay, build
from experiment_replays import audit_paired_case, run_paired_case, run_replay
from prepare_locality import prepare
from revision_experiments import ROOT, input_files, load_json, machine, read_batches, write_json, write_stream

DATASETS = ("email", "amazon")
CONDITIONS = ("concentrated", "dispersed")
PHASES = {
    "full": ("coalesce", "witness", "apply", "closure", "recount", "peel"),
    "star-resident": ("coalesce", "partition", "delete", "insert", "refresh"),
}


def prepare_cases(output, datasets, pairs, batch_size, seed, smoke):
    inputs = output / "inputs"
    inputs.mkdir(exist_ok=True)
    for dataset in datasets:
        _, graph, original = input_files(dataset)
        directory = inputs / dataset
        manifest = directory / "manifest.json"
        placement = prepare(directory, dataset, pairs, batch_size, seed)
        if (Path(placement["graph"]).resolve() != graph.resolve() or
                (placement["dataset"], placement["batches"], placement["batch_size"], placement["seed"])
                != (dataset, pairs, batch_size, seed)):
            raise ValueError(f"{manifest}: different graph or generator parameters")
        if [item["condition"] for item in placement["cases"]] != list(CONDITIONS):
            raise ValueError(f"{manifest}: expected concentrated and dispersed streams")

        for item in placement["cases"]:
            stream = Path(item["stream"]).resolve(strict=True)
            batches = read_batches(stream)
            check_inverses(stream, batches, pairs, batch_size)
            yield {
                "dataset": dataset, "condition": item["condition"], "graph": str(graph),
                "stream": str(stream), "graph_sha256": digest(graph), "stream_sha256": digest(stream),
                "batch_operations": [len(batch) for batch in batches],
                "selected": list(range(1, len(batches) + 1, 2)),
                "initial_edges": placement["initial_edges"], "placement_manifest": str(manifest),
                "placement_manifest_sha256": digest(manifest),
            }

        batches = read_batches(original)
        if smoke:
            batches = batches[:3]
            original = directory / "observed.prefix.stream"
            write_stream(original, batches)
        yield {
            "dataset": dataset, "condition": "observed", "graph": str(graph), "stream": str(original),
            "graph_sha256": digest(graph), "stream_sha256": digest(original),
            "batch_operations": [len(batch) for batch in batches],
            "selected": list(range(1, len(batches) + 1)), "initial_edges": placement["initial_edges"],
        }


def check_inverses(path, batches, pairs, batch_size):
    if len(batches) != 2 * pairs or any(len(batch) != batch_size for batch in batches):
        raise ValueError(f"{path}: unexpected complete target and reset batches")
    for target, reset in zip(batches[::2], batches[1::2]):
        rows = [row.split() for row in target]
        edges = {tuple(sorted((int(u), int(v)))) for _, u, v in rows if u != v}
        inverse = [(b"D" if op == b"I" else b"I", u, v) for op, u, v in reversed(rows)]
        reset_rows = [tuple(row.split()) for row in reset]
        # Some adapters preserve order in the reset; all touched edges are disjoint as keys.
        if (len(edges) != batch_size or sum(op == b"D" for op, _, _ in rows) != batch_size // 2
                or sorted(reset_rows) != sorted(inverse)):
            raise ValueError(f"{path}: unmatched net update budget or reset")


def check_inputs(case):
    for kind in ("graph", "stream", "placement_manifest"):
        if kind in case and digest(Path(case[kind])) != case[f"{kind}_sha256"]:
            raise ValueError(f"{case[kind]}: checksum differs from experiment protocol")
    if "placement_manifest" in case:
        manifest = Path(case["placement_manifest"])
        for name, value in load_json(manifest)["artifacts"].items():
            if digest(manifest.parent / name) != value:
                raise ValueError(f"{manifest.parent / name}: checksum differs from input manifest")


def profile_rows(directory, case, method, verify_trace):
    path = directory / f"{method}.profile.phases.csv"
    trace, _ = audit_replay(directory, method, "profile", case["batch_operations"], False)
    if trace != verify_trace:
        raise ValueError(f"{directory}: {method} profiled replay diverged from verification")
    raw = csv_rows(directory / f"{method}.profile.batches.csv")
    rows = csv_rows(path)
    names = [f"{name}_us" for name in PHASES[method]]
    wanted = {"run", "batch", "elapsed_us", *names}
    if len(rows) != len(raw) or set(rows[0]) != wanted:
        raise ValueError(f"{path}: unexpected phase schema or batch count")
    selected = set(case["selected"])
    totals = {key: [] for key in ("elapsed", *PHASES[method], "unattributed")}
    for row, timing in zip(rows, raw):
        values = [float(row[key]) for key in ("elapsed_us", *names)]
        elapsed, *parts = values
        remainder = elapsed - sum(parts)
        if ((row["run"], row["batch"]) != (timing["run"], timing["batch"]) or
                not math.isclose(elapsed, float(timing["elapsed_us"]), abs_tol=0.01) or
                any(not math.isfinite(x) or x < 0 for x in values) or remainder < -0.01):
            raise ValueError(f"{path}: invalid phase measurements in batch {row['batch']}")
        if int(row["batch"]) in selected:
            for samples, value in zip(totals.values(), [*values, max(0.0, remainder)]):
                samples.append(value)
    total = sum(totals["elapsed"])
    return [dict(dataset=case["dataset"], condition=case["condition"], method=method,
                 batches=len(selected), phase=name, mean_us_per_batch=mean(values),
                 share_of_profiled_batch_us=sum(values) / total, source=str(path))
            for name, values in totals.items()]


def latency_rows(directory, case, runs):
    selected = set(case["selected"])
    full = csv_rows(directory / "full.verify.batches.csv")
    region = mean(int(row["region_edges"]) for row in full if int(row["batch"]) in selected)
    raw_ops = sum(case["batch_operations"][i - 1] for i in case["selected"])
    results = []
    for method in METHODS:
        samples = [[float(row["elapsed_us"]) / 1000
                    for row in csv_rows(directory / f"{method}.timing.{trial}.batches.csv")
                    if int(row["batch"]) in selected] for trial in range(runs)]
        means = [mean(trial) for trial in samples]
        pooled = sorted(value for trial in samples for value in trial)
        results.append(dict(
            dataset=case["dataset"], condition=case["condition"], method=method, threads=1,
            target_batches=len(selected), reset_batches=len(full) - len(selected), raw_ops=raw_ops,
            replays=runs, mean_ms_per_batch=mean(means), replay_mean_sd_ms=stdev(means) if runs > 1 else "",
            p95_ms_per_batch=pooled[math.ceil(.95 * len(pooled)) - 1],
            mean_full_closure_edges=region, source=str(directory), placement=case["placement_manifest"],
        ))
    return results


def write_csv(path, rows):
    scratch = path.with_suffix(".tmp")
    with scratch.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0])
        writer.writeheader()
        writer.writerows(rows)
    scratch.replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--smoke-only", action="store_true", help="two target/reset pairs and three observed batches")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--datasets", choices=DATASETS, nargs="+", default=DATASETS)
    parser.add_argument("--batches", type=int, help="number of target batches (default 20; smoke 2)")
    parser.add_argument("--batch-size", type=int, default=100, help="raw ops per controlled batch (default 100)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--runs", type=int, help="uninstrumented timing repeats (default 3; smoke 1)")
    parser.add_argument("--cpu", type=int, help="one allowed logical CPU shared by both methods")
    parser.add_argument("--star-root", type=Path, default=ROOT / ".build/TrussMaintenance")
    parser.add_argument("--check-only", action="store_true", help="audit a saved output directory")
    args = parser.parse_args()
    pairs = args.batches if args.batches is not None else (2 if args.smoke_only else 20)
    runs = args.runs if args.runs is not None else (1 if args.smoke_only else 3)
    if runs < 1 or pairs < 1 or args.batch_size < 2 or args.batch_size % 2 or len(set(args.datasets)) != len(args.datasets):
        parser.error("use positive runs/batches, an even batch size, and distinct datasets")
    default = "vldb2027-locality-smoke" if args.smoke_only else "vldb2027-locality"
    output = (args.output or ROOT / ".build" / default).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    protocol = output / "protocol.json"
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.check_only:
            plan = load_json(protocol)
        else:
            identity, hardware = machine()
            allowed = identity["affinity"]
            cpu = allowed[0] if args.cpu is None else args.cpu
            if cpu not in allowed:
                parser.error(f"CPU {cpu} is outside allowed affinity {allowed}")
            os.sched_setaffinity(0, {cpu})
            env = os.environ | {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
            print(f"Run directory: {output} | {identity['host']} | CPU {cpu}", flush=True)
            plan = {
                "test_mode": args.smoke_only, "threads": 1, "replays": runs, "cpu": cpu,
                "machine": {key: value for key, value in identity.items() if key != "affinity"},
                "timer_scope": SCOPE, "profiles": "one extra instrumented replay per method and case",
                "sources": {name: digest(ROOT / name) for name in (
                    "scripts/run_locality_experiments.py", "scripts/experiment_replays.py",
                    "scripts/prepare_locality.py", "scripts/prepare_locality.cpp",
                )},
                "cases": list(prepare_cases(output, args.datasets, pairs, args.batch_size, args.seed,
                                            args.smoke_only)),
            }
            if protocol.exists() and load_json(protocol) != plan:
                raise ValueError("inputs, parameters, host, or CPU changed; use a new --output")
            write_json(protocol, plan)
            (output / "hardware.txt").write_text(hardware)
            binaries = build(output, args.star_root.resolve(strict=True), env)

        latencies, phases = [], []
        for case in plan["cases"]:
            check_inputs(case)
            label = case["condition"]
            directory = output / f"{case['dataset']}-{label}"
            repeats = 0 if label == "observed" else plan["replays"]
            net_trace = [(count // 2, count // 2, case["initial_edges"])
                         for count in case["batch_operations"]]
            expected = {method: net_trace for method in METHODS} if repeats else None
            if args.check_only:
                trace = audit_paired_case(directory, case, METHODS, repeats, expected_traces=expected)
            else:
                trace = run_paired_case(directory, case, METHODS, repeats, binaries, env, expected_traces=expected)
            for method in METHODS:
                if not args.check_only and not (directory / f"{method}.profile.complete").exists():
                    run_replay(directory, case, method, "profile", binaries, env)
                phases.extend(profile_rows(directory, case, method, trace[method]))
                if not args.check_only:
                    (directory / f"{method}.profile.complete").touch()
            if repeats:
                latencies.extend(latency_rows(directory, case, repeats))
        write_csv(output / "locality_summary.csv", latencies)
        write_csv(output / "phase_summary.csv", phases)
    print(f"REPORT {output / 'locality_summary.csv'} and {output / 'phase_summary.csv'}", flush=True)
    print(f"Locality and stage profiling complete: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
