#!/usr/bin/env python3
"""Cross update placement and effective within-batch cancellation on Amazon."""

import argparse
import fcntl
import math
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess
import tempfile

from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, run_logged, write_summary
from prepare_locality import CONDITIONS, prepare
from revision_experiments import BUILD, ROOT, load_json, machine, read_batches, write_json, write_stream
from run_locality_experiments import check_inputs, check_inverses
from run_review_controls import FLAGS
from run_unified_dynamic import ABLATIONS, SCOPE, percentile, replay


METHODS = ("static", "static-net", "full-fused", "full-fused-pruned")
NET_OPS = 126
NEUTRAL_PAIRS = {0: 0, 10: 7, 30: 27, 50: 63}
SEED = 42


def reverse_prefix(target, count):
    """Balance present-to-absent and absent-to-present reversals on target keys."""
    deletes, inserts = target[:NET_OPS // 2], target[NET_OPS // 2:]
    if any(row.split()[0] != b"D" for row in deletes) or any(row.split()[0] != b"I" for row in inserts):
        raise ValueError("expected the locality generator's deletes-then-inserts order")
    target_edges = [operation for pair in zip(deletes, inserts, strict=True) for operation in pair]
    prefix = []
    for operation in target_edges[:count]:
        action, u, v = operation.split()
        inverse = b"I" if action == b"D" else b"D"
        prefix.extend((operation, b" ".join((inverse, u, v)) + b"\n"))
    return prefix + target


def injected_batches(batches, count):
    return [reverse_prefix(batch, count) if index % 2 == 0 else batch
            for index, batch in enumerate(batches)]


def prepare_cases(output, pairs):
    base = output / "inputs/base"
    placement = prepare(base, "amazon", pairs, NET_OPS, SEED)
    manifest = base / "manifest.json"
    for condition in CONDITIONS:
        source = base / f"{condition}.stream"
        net_batches = read_batches(source)
        check_inverses(source, net_batches, pairs, NET_OPS)
        for fraction, reverse_pairs in NEUTRAL_PAIRS.items():
            case_id = f"amazon-{condition}-c{fraction:02d}"
            stream = output / "inputs" / f"{case_id}.stream"
            batches = injected_batches(net_batches, reverse_pairs)
            write_stream(stream, batches)
            yield {
                "dataset": case_id, "condition": condition, "cancellation_pct": fraction,
                "reverse_pairs": reverse_pairs, "graph": placement["graph"],
                "graph_sha256": placement["graph_sha256"], "stream": str(stream),
                "stream_sha256": digest(stream), "source_stream": str(source),
                "source_stream_sha256": digest(source), "placement_manifest": str(manifest),
                "placement_manifest_sha256": digest(manifest), "initial_edges": placement["initial_edges"],
                "batch_operations": [len(batch) for batch in batches],
            }


def verify_input(case, pairs):
    check_inputs(case)
    source = Path(case["source_stream"])
    batches = read_batches(source)
    check_inverses(source, batches, pairs, NET_OPS)
    expanded = injected_batches(batches, case["reverse_pairs"])
    if (digest(source) != case["source_stream_sha256"] or expanded != read_batches(Path(case["stream"]))
            or [len(batch) for batch in expanded] != case["batch_operations"]):
        raise ValueError(f"{case['dataset']}: stream differs from the neutral-pair protocol")
    fraction = 100 * (len(expanded[0]) - NET_OPS) / len(expanded[0])
    if fraction != case["cancellation_pct"]:
        raise ValueError(f"{case['dataset']}: unexpected effective cancellation fraction")


def audit_tag(directory, case, tag):
    target_ops = NET_OPS // 2
    edge_count = case["initial_edges"]
    region, target_latencies, runs = {}, {}, {}
    for method in METHODS:
        counts = case["batch_operations"]
        trace, _ = audit_replay(directory, method, tag, counts, tag == "verify")
        expected_ops = [(count // 2 if method == "static" else target_ops) for count in counts]
        if trace != [(count, count, edge_count) for count in expected_ops]:
            raise ValueError(f"{directory}/{method}.{tag}: mismatched applied operations or graph size")
        prefix = directory / f"{method}.{tag}"
        run, = csv_rows(Path(f"{prefix}.runs.csv"))
        labels = ("batch", ABLATIONS[method]) if method in ABLATIONS else (method, "-")
        if (run["method"], run["ablate"]) != labels or run["dataset"] != case["dataset"]:
            raise ValueError(f"{prefix}: unexpected implementation or workload")
        rows = csv_rows(Path(f"{prefix}.batches.csv"))
        region[method] = [int(row["region_edges"]) for row in rows]
        if any(not 0 <= count <= edge_count for count in region[method]):
            raise ValueError(f"{prefix}: invalid region size")
        target_latencies[method] = [float(row["elapsed_us"]) / 1000 for row in rows[::2]]
        runs[method] = run
    if region["full-fused"] != region["full-fused-pruned"]:
        raise ValueError(f"{directory}/{tag}: fused implementations disagree on the closure trace")
    return region["full-fused"], target_latencies, runs


def report_case(directory, case, repeats):
    verify_regions, _, _ = audit_tag(directory, case, "verify")
    trials = [audit_tag(directory, case, f"timing.{i}") for i in range(repeats)]
    if any(regions != verify_regions for regions, _, _ in trials):
        raise ValueError(f"{directory}: timing and audited closure traces differ")
    summaries, replays = [], []
    for method in METHODS:
        latencies = [target[method] for _, target, _ in trials]
        means = [mean(values) for values in latencies]
        pooled = [value for values in latencies for value in values]
        startup = [float(run[method]["build_s"]) for _, _, run in trials]
        peak_rss = [int(run[method]["rss_mb"]) for _, _, run in trials]
        if any(not math.isfinite(value) or value < 0 for value in startup) or min(peak_rss) <= 0:
            raise ValueError(f"{directory}/{method}: invalid startup time or RSS")
        common = dict(dataset="amazon", condition=case["condition"], method=method,
                      cancellation_pct=case["cancellation_pct"], target_batches=len(latencies[0]),
                      reset_batches=len(verify_regions) // 2, initial_edges=case["initial_edges"],
                      raw_ops_per_target=case["batch_operations"][0], net_ops_per_target=NET_OPS,
                      source=str(directory))
        for i, (average, build_s, rss) in enumerate(zip(means, startup, peak_rss, strict=True)):
            replays.append(dict(common, repetition=i, mean_target_ms=average,
                                startup_s=build_s, peak_process_rss_mib=rss))
        region_mean = mean(verify_regions[::2])
        summaries.append(dict(common, timing_replays=repeats, verified_batches=len(verify_regions),
                              mean_target_ms=mean(means), replay_mean_sd_ms=stdev(means) if repeats > 1 else "",
                              pooled_p95_target_ms=percentile(pooled, .95),
                              pooled_p99_target_ms=percentile(pooled, .99),
                              mean_target_closure_edges=region_mean,
                              mean_target_closure_pct=100 * region_mean / case["initial_edges"],
                              mean_startup_s=mean(startup), peak_process_rss_mib=max(peak_rss)))
    return verify_regions, summaries, replays


def complete_case(output, case, repeats, binaries, env, audit_only, expected_regions):
    directory = output / case["dataset"]
    marker = directory / "complete.json"
    identity = {"case": case, "methods": list(METHODS), "replays": repeats}
    if marker.exists() or audit_only:
        complete = load_json(marker)
        if complete.pop("files") != {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}:
            raise ValueError(f"{directory}: archived CSV digests differ")
        if complete != identity:
            raise ValueError(f"{directory}: case manifest differs from the protocol")
    else:
        if directory.exists():
            failures = output / "failed"
            failures.mkdir(exist_ok=True)
            archive = Path(tempfile.mkdtemp(prefix=f"{case['dataset']}.", dir=failures)) / directory.name
            directory.rename(archive)
            print(f"Archived incomplete attempt {archive}", flush=True)
        directory.mkdir()
        for method in METHODS:
            replay(directory, case, method, "verify", binaries, env)
        verify_regions, _, _ = audit_tag(directory, case, "verify")
        if expected_regions is not None and verify_regions != expected_regions:
            raise ValueError(f"{directory}: neutral pairs changed the closure trace")
        for index in range(repeats):
            offset = (index + case["cancellation_pct"] // 10) % len(METHODS)
            for method in METHODS[offset:] + METHODS[:offset]:
                replay(directory, case, method, f"timing.{index}", binaries, env)
    regions, summary, replays = report_case(directory, case, repeats)
    if expected_regions is not None and regions != expected_regions:
        raise ValueError(f"{directory}: neutral pairs changed the closure trace")
    if not marker.exists():
        write_json(marker, identity | {"files": {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}})
    print(f"AUDIT OK {case['dataset']}: {len(regions)} checkpoints; {repeats} timing replays per method", flush=True)
    return regions, summary, replays


def source_fingerprint():
    dependencies = ("compare_replay.py", "compare_star_resident.py", "prepare_locality.py",
                    "prepare_locality.cpp", "revision_experiments.py", "run_locality_experiments.py",
                    "run_review_controls.py", "run_unified_dynamic.py")
    paths = [Path(__file__).resolve(), ROOT / "src/main.cpp", *sorted((ROOT / "src").glob("*.h")),
             *(ROOT / "scripts" / name for name in dependencies)]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def setup(output, binary, args, env):
    protocol = output / "protocol.json"
    if args.check_only:
        return load_json(protocol)
    identity, hardware = machine()
    cpu = identity["affinity"][0] if args.cpu is None else args.cpu
    if cpu not in identity["affinity"]:
        raise ValueError(f"CPU {cpu} is outside allowed affinity {identity['affinity']}")
    os.sched_setaffinity(0, {cpu})
    pairs = args.pairs or (2 if args.smoke_only else 20)
    repeats = args.runs or (1 if args.smoke_only else 3)
    plan = {"test_mode": args.smoke_only, "pairs": pairs, "replays": repeats, "threads": 1,
            "cpu": cpu, "machine": identity, "methods": list(METHODS), "flags": list(FLAGS),
            "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
            "timer_scope": SCOPE, "source_sha256": source_fingerprint(),
            "treatment": "Odd batches with effective inversions. Even batches reset the original topology.",
            "placement": "Concentrated within one original Amazon triangle component or dispersed across components.",
            "cases": list(prepare_cases(output, pairs))}
    if protocol.exists():
        recorded = load_json(protocol)
        if {key: value for key, value in recorded.items() if key != "binary_sha256"} != plan:
            raise ValueError("experiment protocol or build inputs changed; choose a new --output")
        return recorded
    binary.parent.mkdir(exist_ok=True)
    staged = binary.with_suffix(".tmp")
    run_logged(["g++", *FLAGS, "-o", staged, ROOT / "src/main.cpp"],
               binary.parent / "build.stdout", binary.parent / "build.stderr", env=env)
    if source_fingerprint() != plan["source_sha256"]:
        raise ValueError("sources changed during build")
    staged.replace(binary)
    (output / "hardware.txt").write_text(hardware)
    plan["binary_sha256"] = digest(binary)
    write_json(protocol, plan)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true", help="two real Amazon treatment/reset pairs")
    mode.add_argument("--check-only", action="store_true", help="reconstruct reports from completed cases")
    parser.add_argument("--pairs", type=int, help="number of treatment/reset pairs (default 20)")
    parser.add_argument("--runs", type=int, help="timing repeats per method (default 3)")
    parser.add_argument("--cpu", type=int, help="pinned logical CPU (default first available)")
    parser.add_argument("--output", type=Path, help="output directory for inputs, traces and reports")
    args = parser.parse_args()
    if any(value is not None and value < 1 for value in (args.pairs, args.runs)):
        parser.error("--pairs and --runs must be positive")
    if args.check_only and (args.output is None or any(value is not None for value in (args.pairs, args.runs, args.cpu))):
        parser.error("--check-only requires --output and takes no execution options")
    name = "vldb2027-main4-cancellation-smoke" if args.smoke_only else "vldb2027-main4-cancellation"
    output = (args.output or BUILD / name).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    binary = output / "build/batchtruss"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
    env.update(OMP_NUM_THREADS="1", OMP_DYNAMIC="FALSE", LC_ALL="C")
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = setup(output, binary, args, env)
        cases = [(case["condition"], case["cancellation_pct"], case["reverse_pairs"]) for case in plan["cases"]]
        expected = [(condition, fraction, count) for condition in CONDITIONS for fraction, count in NEUTRAL_PAIRS.items()]
        if cases != expected or plan["methods"] != list(METHODS) or plan["threads"] != 1:
            raise ValueError("unexpected saved workload or method protocol")
        if digest(binary) != plan["binary_sha256"]:
            raise ValueError("executable does not match the saved experiment protocol")
        print(f"Run directory: {output} | {plan['machine']['host']} | CPU {plan['cpu']}", flush=True)
        closures, summary, replays = {}, [], []
        for case in plan["cases"]:
            verify_input(case, plan["pairs"])
            condition = case["condition"]
            regions, metrics, repeats = complete_case(output, case, plan["replays"], {"full": binary}, env,
                                                       args.check_only, closures.get(condition))
            closures.setdefault(condition, regions)
            if condition == CONDITIONS[-1] and mean(regions[::2]) <= mean(closures[CONDITIONS[0]][::2]):
                raise ValueError("the dispersed placement did not reach a larger measured closure")
            summary.extend(metrics)
            replays.extend(repeats)
        bases = {(row["condition"], row["method"]): row["mean_target_ms"] for row in summary
                 if row["cancellation_pct"] == 0}
        static = {(row["condition"], row["cancellation_pct"]): row["mean_target_ms"] for row in summary
                  if row["method"] == "static"}
        for row in summary:
            row["latency_over_no_cancellation"] = row["mean_target_ms"] / bases[(row["condition"], row["method"])]
            row["speedup_over_static"] = static[(row["condition"], row["cancellation_pct"])] / row["mean_target_ms"]
        write_summary(output, summary)
        write_summary(output, replays, filename="timing_replays.csv")
    print(f"REPORT {output / 'summary.csv'}", flush=True)
    print(f"ALL AUDITS OK: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
