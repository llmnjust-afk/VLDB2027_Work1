#!/usr/bin/env python3
"""Benchmark causal selection between fused regional and ordered static maintenance."""

import argparse
import fcntl
from hashlib import sha256
from math import ceil, isfinite
import os
from pathlib import Path
from statistics import mean, stdev
import subprocess
import tempfile

from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, cases, run_logged, write_summary
from experiment_replays import check_verified_traces
from revision_experiments import (
    BUILD, GENERATED, ROOT, TEMPORAL, load_json, machine, read_batches, write_json, write_stream,
)
from run_review_controls import FLAGS


DATASETS = (*GENERATED, *TEMPORAL)
METHODS = ("full", "full-fused", "static", "static-net", "adaptive")
ABLATIONS = {"full": "full", "full-fused": "fused", "adaptive": "adaptive"}
STATIC_INTERVAL = 8
POLICY = "Fused first. If its closure exceeds half the live edges, use ordered Static at the next eight checkpoints, then probe Fused."
TIMING_SCOPE = "Per-batch update application, exact labels, and route decision; input, construction, output and verification excluded."


def switch_fixture(output):
    graph = ROOT / "tests/fixtures/k4.graph"
    stream = output / "inputs/adaptive-switch.stream"
    batches = [[b"I 2 3"], [b"D 2 3", b"D 4 5"], [b"I 2 3", b"D 2 3"]]
    batches += [[] for _ in range(6)]
    batches += [[b"I 4 5"], [b"I 2 3"], [b"D 2 3"]]
    write_stream(stream, batches)
    return dict(dataset="adaptive-switch", scope="fixture", graph=str(graph), stream=str(stream),
                graph_sha256=digest(graph), stream_sha256=digest(stream),
                batch_operations=[len(batch) for batch in batches])


def operation_digest(path):
    checkpoint_free = sha256()
    with path.open("rb") as file:
        for line in file:
            if not line.startswith(b"B "):
                checkpoint_free.update(line)
    return checkpoint_free.hexdigest()


def select_prefix(case, limit, output):
    dataset = case["dataset"]
    if len(case["batch_operations"]) < limit:
        raise ValueError(f"{dataset}: fewer than the requested {limit} complete batches")
    stream = output / "inputs" / f"{dataset}.first-{limit}.stream"
    write_stream(stream, read_batches(Path(case["stream"]))[:limit])
    return case | dict(scope=f"prefix-{limit}", stream=str(stream), stream_sha256=digest(stream),
                       batch_operations=case["batch_operations"][:limit],
                       source_stream=case["stream"], source_stream_sha256=case["stream_sha256"])


def holdout_cases():
    result = []
    for days in (1, 14):
        source = BUILD / f"vldb2027-stackoverflow-holdout-112d-b{days}d/inputs"
        manifest = load_json(source / "manifest.json")
        settings = manifest["protocol"]
        if (settings["dataset_key"], settings["window_days"], settings["step_seconds"],
                settings["start_utc"], settings["end_utc"]) != (
                    "stackoverflow", [7], days * 86400, 1422489600, 1432166400
                ):
            raise ValueError(f"{source}: unexpected Stack Overflow holdout protocol")
        graph, stream, checkpoints = (source / f"w7d/{name}" for name in
                                      ("graph.txt", "updates.stream", "checkpoints.csv"))
        hashes = [digest(path) for path in (graph, stream, checkpoints)]
        if hashes != [manifest["sha256"][f"w7d/{path.name}"] for path in (graph, stream, checkpoints)]:
            raise ValueError(f"{source}: input differs from its verified manifest")
        entries = csv_rows(checkpoints)
        counts = [int(row["operations"]) for row in entries]
        if len(entries) != 112 // days or sum(counts) != 3706570:
            raise ValueError(f"{source}: incomplete holdout checkpoints")
        result.append(dict(dataset=f"stackoverflow-b{days}d", scope="holdout", graph=str(graph),
                           stream=str(stream), checkpoints=str(checkpoints), graph_sha256=hashes[0],
                           stream_sha256=hashes[1], checkpoints_sha256=hashes[2],
                           batch_operations=counts))
    daily, fortnightly = result
    if (daily["graph_sha256"] != fortnightly["graph_sha256"] or
            operation_digest(Path(daily["stream"])) != operation_digest(Path(fortnightly["stream"]))):
        raise ValueError("holdout schedules do not share an initial graph and ordered events")
    return result


def setup(output, args, binary, environment):
    identity, hardware = machine()
    allowed = identity["affinity"]
    cpu = allowed[0] if args.cpu is None else args.cpu
    if cpu not in allowed:
        raise ValueError(f"CPU {cpu} is outside the allowed affinity {allowed}")
    os.sched_setaffinity(0, {cpu})
    result_cases = list(cases(args.datasets, args.smoke_only, output))
    if args.smoke_only:
        result_cases.insert(0, switch_fixture(output))
    if args.prefix_batches is not None:
        result_cases = [select_prefix(case, args.prefix_batches, output) for case in result_cases]
    if args.include_holdout:
        result_cases.extend(holdout_cases())
    program_paths = [ROOT / "src/main.cpp", *sorted((ROOT / "src").glob("*.h"))]
    tools = ("run_adaptive_comparison", "compare_replay", "compare_star_resident", "experiment_replays",
             "revision_experiments", "run_review_controls")
    program_paths.extend(ROOT / "scripts" / f"{name}.py" for name in tools)
    plan = dict(policy=POLICY, static_interval=STATIC_INTERVAL, timer_scope=TIMING_SCOPE,
                methods=list(METHODS), machine=identity, cpu=cpu, threads=1,
                timing_replays=args.runs or (1 if args.smoke_only else 3),
                smoke_only=args.smoke_only, prefix_batches=args.prefix_batches, cases=result_cases,
                flags=list(FLAGS), compiler=subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
                sources={str(path.relative_to(ROOT)): digest(path) for path in program_paths})
    manifest = output / "protocol.json"
    if manifest.exists():
        saved = load_json(manifest)
        if plan != {key: value for key, value in saved.items() if key != "binary_sha256"}:
            raise ValueError(f"{output}: existing protocol differs. Use a fresh --output or the matching arguments")
        return saved
    binary.parent.mkdir(exist_ok=True)
    temporary = binary.with_suffix(".tmp")
    run_logged(["g++", *FLAGS, "-o", temporary, ROOT / "src/main.cpp"],
               binary.parent / "build.log", binary.parent / "build.stderr", env=environment)
    temporary.replace(binary)
    (output / "hardware.txt").write_text(hardware)
    plan["binary_sha256"] = digest(binary)
    write_json(manifest, plan)
    return plan


def replay(directory, case, method, tag, binary, environment):
    prefix = directory / f"{method}.{tag}"
    mode = ["batch", "--ablate", ABLATIONS[method]] if method in ABLATIONS else [method]
    verify = tag == "verify"
    command = [binary, *mode, "--graph", case["graph"], "--stream", case["stream"],
               "--dataset", case["dataset"], "--threads", "1", "--runs", "1",
               "--batch-csv", f"{prefix}.batches.csv"]
    command += ["--check-every", "1"] if verify else ["--timing-only"]
    if method == "adaptive":
        command += ["--route-csv", f"{prefix}.routes.csv"]
    wall = run_logged(command, Path(f"{prefix}.runs.csv"), Path(f"{prefix}.stderr"), env=environment)
    _, latency = audit_replay(directory, method, tag, case["batch_operations"], verify)
    print(f"{case['dataset']} {method} {tag}: {wall:.2f} s wall, {latency:.3f} ms/batch", flush=True)


def audit_batch(directory, case, method, tag):
    prefix = directory / f"{method}.{tag}"
    trace, latency = audit_replay(directory, method, tag, case["batch_operations"], tag == "verify")
    run, = csv_rows(Path(f"{prefix}.runs.csv"))
    expected_method = ("batch", ABLATIONS[method]) if method in ABLATIONS else (method, "-")
    if (run["method"], run["ablate"]) != expected_method or run["dataset"] != case["dataset"]:
        raise ValueError(f"{prefix}: replay method or workload differs from its protocol")
    rows = csv_rows(Path(f"{prefix}.batches.csv"))
    regions = [int(row["region_edges"]) for row in rows]
    if any(not 0 <= region <= checkpoint[2] for region, checkpoint in zip(regions, trace, strict=True)):
        raise ValueError(f"{prefix}: invalid region edge count")
    return trace, regions, latency, [float(row["elapsed_us"]) / 1000 for row in rows]


def audit_route(directory, case, tag, adaptive, fused, static):
    path = directory / f"adaptive.{tag}.routes.csv"
    with path.open() as file:
        if file.readline().rstrip("\n") != "run,batch,route":
            raise ValueError(f"{path}: route header differs from the protocol")
    rows = csv_rows(path)
    trace, regions, _, _ = adaptive
    observed = [(row["run"], row["batch"]) for row in rows]
    if observed != [("0", str(i)) for i in range(1, len(trace) + 1)]:
        raise ValueError(f"{path}: missing or misaligned route records")
    routes = [row["route"] for row in rows]
    remaining = 0
    for index, (route, mutation, region) in enumerate(zip(routes, trace, regions, strict=True)):
        if remaining:
            expected_route, expected_mutation, expected_region = "static", static[0][index], 0
            remaining -= 1
        else:
            expected_route, expected_mutation, expected_region = "fused", fused[0][index], fused[1][index]
            if 2 * expected_region > expected_mutation[2]:
                remaining = STATIC_INTERVAL
        if (route, mutation, region) != (expected_route, expected_mutation, expected_region):
            raise ValueError(f"{path}, checkpoint {index + 1}: routing, region or mutation trace differs")
    if case["dataset"] == "adaptive-switch" and routes != (
            ["fused"] + ["static"] * STATIC_INTERVAL + ["fused", "fused", "static"]
    ):
        raise ValueError(f"{path}: the switch fixture did not exercise both transitions as intended")
    return routes


def audit_tag(directory, case, tag):
    methods = {method: audit_batch(directory, case, method, tag) for method in METHODS}
    ordinary = {method: methods[method][0] for method in METHODS if method != "adaptive"}
    expected = None
    if "checkpoints" in case:
        rows = csv_rows(Path(case["checkpoints"]))
        expected = {method: [
            (int(row[f"{scope}_deletions"]), int(row[f"{scope}_insertions"]), int(row["active_edges"]))
            for row in rows
        ] for method, scope in ((name, "ordered" if name == "static" else "net") for name in ordinary)}
    check_verified_traces(directory, ordinary, expected)
    if methods["full"][1] != methods["full-fused"][1]:
        raise ValueError(f"{directory}/{tag}: Regional and Fused closures differ")
    routes = audit_route(directory, case, tag, methods["adaptive"], methods["full-fused"], methods["static"])
    return methods, routes


def audit_case(directory, case, runs):
    verification, routes = audit_tag(directory, case, "verify")
    all_timings = {method: [] for method in METHODS}
    for trial in range(runs):
        timing, timing_routes = audit_tag(directory, case, f"timing.{trial}")
        if timing_routes != routes or any(timing[method][:2] != verification[method][:2] for method in METHODS):
            raise ValueError(f"{directory}: timing replay {trial} differs from verified paths")
        for method in METHODS:
            all_timings[method].append(timing[method][3])
    closure = verification["full"][1]
    coverage = mean(region / final if final else 0
                    for region, (_, _, final) in zip(closure, verification["full"][0], strict=True))
    rows = []
    for method, times in all_timings.items():
        pooled = sorted(value for trial in times for value in trial)
        builds, peaks = [], []
        for trial in range(runs):
            run, = csv_rows(directory / f"{method}.timing.{trial}.runs.csv")
            build_s, peak = float(run["build_s"]), int(run["rss_mb"])
            if not isfinite(build_s) or build_s < 0 or peak <= 0:
                raise ValueError(f"{directory}: invalid construction or RSS for {method} timing.{trial}")
            builds.append(build_s)
            peaks.append(peak)
        rows.append(dict(dataset=case["dataset"], scope=case["scope"], method=method, threads=1,
                         batches=len(routes), operations=sum(case["batch_operations"]), replays=runs,
                         verified_batches=len(routes), regional_mean_coverage=coverage,
                         adaptive_fused_batches=routes.count("fused") if method == "adaptive" else "",
                         adaptive_static_batches=routes.count("static") if method == "adaptive" else "",
                         mean_ms_per_batch=mean(pooled), replay_mean_sd_ms=stdev(map(mean, times)) if runs > 1 else "",
                         pooled_p95_ms=pooled[ceil(len(pooled) * .95) - 1], mean_build_s=mean(builds),
                         max_peak_process_rss_mib=max(peaks)))
    static_mean = next(row["mean_ms_per_batch"] for row in rows if row["method"] == "static")
    for row in rows:
        row["static_over_method"] = static_mean / row["mean_ms_per_batch"]
    return rows


def run_case(output, case, runs, binary, environment, audit_only):
    dataset = case["dataset"]
    for name in ("graph", "stream", "source_stream", "checkpoints"):
        if name in case and digest(Path(case[name])) != case[f"{name}_sha256"]:
            raise ValueError(f"{dataset}: changed {name}")
    directory = output / dataset
    success = directory / "complete.json"
    protocol = dict(case=case, methods=list(METHODS), replays=runs)
    if success.exists() or audit_only:
        record = load_json(success)
        if record.pop("files") != {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}:
            raise ValueError(f"{directory}: saved replay CSVs differ from the completion record")
        if record != protocol:
            raise ValueError(f"{directory}: completion record disagrees with the protocol")
        rows = audit_case(directory, case, runs)
    else:
        if directory.exists():
            failed = output / "failed"
            failed.mkdir(exist_ok=True)
            archived = Path(tempfile.mkdtemp(prefix=f"{dataset}.", dir=failed)) / dataset
            directory.rename(archived)
            print(f"Archived incomplete attempt in {archived}", flush=True)
        directory.mkdir()
        for method in METHODS:
            replay(directory, case, method, "verify", binary, environment)
        audit_tag(directory, case, "verify")
        for trial in range(runs):
            offset = trial % len(METHODS)
            for method in METHODS[offset:] + METHODS[:offset]:
                replay(directory, case, method, f"timing.{trial}", binary, environment)
        rows = audit_case(directory, case, runs)
        files = {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}
        write_json(success, protocol | {"files": files})
    print(f"AUDIT OK {dataset}: {rows[0]['verified_batches']} checkpoints, {runs} timing replays per method", flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--smoke-only", action="store_true", help="verify policy switches on a fixture and benchmark first three batches")
    modes.add_argument("--check-only", action="store_true", help="audit completed traces and regenerate summary from the saved protocol")
    modes.add_argument("--prefix-batches", type=int, help="use exactly this many initial complete batches for a pilot")
    parser.add_argument("--datasets", nargs="+", choices=DATASETS, default=DATASETS)
    parser.add_argument("--runs", type=int, help="fresh timings per method (default 3, smoke 1)")
    parser.add_argument("--cpu", type=int, help="logical CPU (default first allowed)")
    parser.add_argument("--include-holdout", action="store_true", help="also compare daily and 14-day Stack Overflow holdout schedules")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.runs is not None and args.runs < 1) or len(set(args.datasets)) != len(args.datasets):
        parser.error("require positive --runs and distinct datasets")
    if args.prefix_batches is not None and args.prefix_batches < 1:
        parser.error("--prefix-batches must be positive")
    if args.include_holdout and (args.smoke_only or args.check_only or args.prefix_batches is not None):
        parser.error("--include-holdout is only used when setting up a formal run")
    if args.check_only and args.output is None:
        parser.error("--check-only requires --output")
    name = ("vldb2027-adaptive-smoke" if args.smoke_only else
            f"vldb2027-adaptive-prefix-{args.prefix_batches}" if args.prefix_batches is not None else "vldb2027-adaptive")
    output = (args.output or BUILD / name).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    binary = output / "build/batchtruss"
    environment = dict(os.environ)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan = load_json(output / "protocol.json") if args.check_only else setup(output, args, binary, environment)
        runs = plan["timing_replays"]
        if digest(binary) != plan["binary_sha256"] or (plan["policy"], plan["static_interval"], plan["methods"],
                plan["threads"]) != (POLICY, STATIC_INTERVAL, list(METHODS), 1) or runs < 1:
            raise ValueError(f"{output}: executable or audit protocol mismatch")
        print(f"Run directory: {output} | {plan['machine']['host']} | CPU {plan['cpu']}", flush=True)
        rows = []
        for case in plan["cases"]:
            rows.extend(run_case(output, case, runs, binary, environment, args.check_only))
        write_summary(output, rows)
    print(f"Adaptive comparison complete: {output / 'summary.csv'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
