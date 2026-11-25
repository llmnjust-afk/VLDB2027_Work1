#!/usr/bin/env python3
"""Compare exact batch maintainers and dynamic baselines in one pinned run."""

import argparse
import fcntl
import math
import os
from pathlib import Path
import re
from statistics import mean, stdev
import subprocess
import tempfile
from time import perf_counter

from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, build, cases, run_logged, write_summary
from experiment_replays import check_verified_traces
from revision_experiments import BUILD, ROOT, load_json, machine, write_json, write_stream
from run_adaptive_comparison import POLICY, audit_route, holdout_cases, switch_fixture
from run_review_controls import FLAGS
from run_temporal_windows import experiment_case, workload_stats


TEMPORAL = {
    "askubuntu-7d": ("vldb2027-temporal-askubuntu-w7d-ablation", 7),
    "askubuntu-365d": ("vldb2027-temporal-askubuntu-large-fused", 365),
    "stackoverflow-28d": ("vldb2027-temporal-stackoverflow-fused", 7),
}
HOLDOUT = ("stackoverflow-b1d", "stackoverflow-b14d")
DEFAULT_CASES = (*TEMPORAL, *HOLDOUT)
METHODS = ("static", "static-net", "full", "full-fused", "adaptive", "star-resident", "luo-edge")
PRUNING_PROFILES = ("full-fused", "full-fused-pruned")
ABLATIONS = {
    "full": "full", "full-fused": "fused", "adaptive": "adaptive",
    "full-fused-pruned": "fused-pruned",
}
SCOPE = ("steady_clock per batch from ordered/net update application through exact labels, "
         "including dispatch or baseline grouping; input, initialization, verification and output excluded")


def temporal_case(name, prepared, window):
    if not (prepared / "manifest.json").is_file():
        prepared = prepared / "inputs"
    manifest_path = prepared / "manifest.json"
    manifest = load_json(manifest_path)
    plan = manifest["protocol"]
    if window not in plan["window_days"] or plan.get("test_mode", False):
        raise ValueError(f"{name}: use a complete prepared input containing retention {window}")
    directory = prepared / f"w{window}d"
    files = {"graph": directory / "graph.txt", "stream": directory / "updates.stream",
             "checkpoints": directory / "checkpoints.csv", "input_manifest": manifest_path}
    hashes = {key: digest(path) for key, path in files.items()}
    for key in ("graph", "stream", "checkpoints"):
        if hashes[key] != manifest["sha256"][str(files[key].relative_to(prepared))]:
            raise ValueError(f"{name}: prepared {key} differs from its manifest")
    case, checkpoints, _ = experiment_case(prepared, window, plan["end_utc"],
                                           dataset=name, start=plan["start_utc"],
                                           step=plan["step_seconds"])
    workload_stats(case, checkpoints)
    return dict(case, dataset=name, scope="temporal", start_utc=plan["start_utc"],
                end_utc=plan["end_utc"], checkpoint_seconds=plan["step_seconds"],
                raw_sha256=plan.get("raw_sha256", ""),
                **{key: str(path) for key, path in files.items()},
                **{f"{key}_sha256": value for key, value in hashes.items()})


def first_batches(case, output):
    counts = case["batch_operations"][:3]
    source = Path(case["stream"])
    with source.open("rb") as file:
        batches = []
        for count in counts:
            if file.readline().split() != [b"B", str(count).encode()]:
                raise ValueError(f"{source}: batch headers differ from the registered input")
            batch = [file.readline() for _ in range(count)]
            if any(not row for row in batch):
                raise ValueError(f"{source}: premature end of stream")
            batches.append(batch)
    stream = output / "inputs" / f"{case['dataset']}.first-{len(counts)}.stream"
    write_stream(stream, batches)
    return dict(case, scope=f"prefix-{len(counts)}", stream=str(stream),
                stream_sha256=digest(stream), batch_operations=counts,
                source_stream=str(source), source_stream_sha256=case["stream_sha256"])


def configured_cases(args, output):
    requested = {}
    for name in args.cases:
        if name in TEMPORAL:
            directory, window = TEMPORAL[name]
            requested[name] = temporal_case(name, BUILD / directory, window)
        elif name == "amazon":
            requested[name], = cases((name,), False, output)
    if any(name in HOLDOUT for name in args.cases):
        matched = {case["dataset"]: case for case in holdout_cases()}
        for days, name in zip((1, 14), HOLDOUT, strict=True):
            if name in args.cases:
                directory = BUILD / f"vldb2027-stackoverflow-holdout-112d-b{days}d"
                case = temporal_case(name, directory, 7)
                for key in ("graph_sha256", "stream_sha256", "checkpoints_sha256", "batch_operations"):
                    if case[key] != matched[name][key]:
                        raise ValueError(f"{name}: holdout data disagree with the registered pair")
                requested[name] = case
    for name, directory, window in args.prepared:
        if name in requested or name == "adaptive-switch" or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]*", name):
            raise ValueError(f"choose a unique safe workload name: {name}")
        requested[name] = temporal_case(name, Path(directory).resolve(), int(window))
    if not requested:
        raise ValueError("select at least one built-in or prepared case")
    ordered = [requested[name] for name in args.cases] + [requested[name] for name, _, _ in args.prepared]
    if args.smoke_only:
        return [switch_fixture(output), *(first_batches(case, output) for case in ordered)]
    return ordered


def binary_paths(output):
    return {method: output / "build" / method for method in ("full", "star-resident", "luo-edge")}


def fingerprint():
    files = [
        Path(__file__), ROOT / "src/main.cpp", ROOT / "src/star_resident.cpp",
        ROOT / "src/luo_edge_replay.cpp", *sorted((ROOT / "src").glob("*.h")),
        *(ROOT / "scripts" / name for name in (
            "build_star_resident.py", "star-source.sha256", "compare_replay.py",
            "compare_star_resident.py", "experiment_replays.py", "revision_experiments.py",
            "run_adaptive_comparison.py", "run_review_controls.py", "run_temporal_windows.py",
        )),
    ]
    return {str(path.relative_to(ROOT)): digest(path) for path in files}


def setup(args, output, binaries):
    identity, hardware = machine()
    cpu = identity["affinity"][0] if args.cpu is None else args.cpu
    if cpu not in identity["affinity"]:
        raise ValueError(f"CPU {cpu} is outside allowed affinity {identity['affinity']}")
    os.sched_setaffinity(0, {cpu})
    env = {key: val for key, val in os.environ.items() if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
    env |= {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
    methods = (*METHODS, "full-fused-pruned") if args.include_pruned else METHODS
    plan = dict(
        machine=identity, cpu=cpu, threads=1, methods=list(methods),
        timer_scope=SCOPE, adaptive_policy=POLICY, star_root=str(args.star_root),
        timing_replays=args.runs or (1 if args.smoke_only else 3), smoke_only=args.smoke_only,
        profile_pruned=args.profile_pruned,
        cases=configured_cases(args, output), sources=fingerprint(), flags=list(FLAGS),
        compiler=subprocess.check_output(["g++", "--version"], text=True, env=env).splitlines()[0],
    )
    protocol = output / "protocol.json"
    if protocol.is_file():
        recorded = load_json(protocol)
        if {key: value for key, value in recorded.items() if key != "binary_sha256"} != plan:
            raise ValueError("inputs, host, code, or run protocol changed; choose a new --output")
        return recorded, env
    build(output, args.star_root, env)
    run_logged(["g++", *FLAGS, "src/luo_edge_replay.cpp", "-o", binaries["luo-edge"]],
               output / "build/luo-edge.build.log", output / "build/luo-edge.build.stderr", env=env)
    if fingerprint() != plan["sources"]:
        raise ValueError("sources changed during compilation; retry with a fresh output")
    if args.include_pruned:
        usage = subprocess.run([binaries["full"]], capture_output=True, text=True, env=env)
        if "fused-pruned" not in usage.stderr:
            raise ValueError("the current build does not expose fused-pruned")
    (output / "hardware.txt").write_text(hardware)
    plan["binary_sha256"] = {method: digest(path) for method, path in binaries.items()}
    write_json(protocol, plan)
    return plan, env


def checkpoint_rows(case):
    if "checkpoints" not in case:
        return None
    rows = csv_rows(Path(case["checkpoints"]))[:len(case["batch_operations"])]
    if [int(row["operations"]) for row in rows] != case["batch_operations"]:
        raise ValueError(f"{case['dataset']}: checkpoint and stream lengths disagree")
    return rows


def replay(directory, case, method, tag, binaries, env):
    prefix = directory / f"{method}.{tag}"
    command = [binaries.get(method, binaries["full"])]
    if method in ABLATIONS:
        command += ["batch", "--ablate", ABLATIONS[method]]
    elif method not in binaries:
        command += [method]
    command += ["--graph", case["graph"], "--stream", case["stream"], "--dataset", case["dataset"],
                "--threads", "1", "--runs", "1", "--batch-csv", f"{prefix}.batches.csv"]
    command += ["--check-every", "1"] if tag == "verify" else ["--timing-only"]
    if method == "adaptive":
        command += ["--route-csv", f"{prefix}.routes.csv"]
    if tag == "profile":
        command += ["--phase-csv", f"{prefix}.phases.csv", "--peel-scan-csv", f"{prefix}.scans.csv"]
    wall = run_logged(command, Path(f"{prefix}.runs.csv"), Path(f"{prefix}.stderr"), env=env)
    print(f"{case['dataset']} {method} {tag}: {wall:.2f} s wall", flush=True)


def audit_tag(directory, case, methods, checkpoints, tag):
    data = {}
    for method in methods:
        prefix = directory / f"{method}.{tag}"
        trace, latency = audit_replay(directory, method, tag, case["batch_operations"], tag == "verify")
        run, = csv_rows(Path(f"{prefix}.runs.csv"))
        expected = ("batch", ABLATIONS[method]) if method in ABLATIONS else (method, "-")
        if (run["method"], run.get("ablate", "-")) != expected or run["dataset"] != case["dataset"]:
            raise ValueError(f"{prefix}: replay method or workload differs from its protocol")
        rows = csv_rows(Path(f"{prefix}.batches.csv"))
        regions = [int(row["region_edges"]) for row in rows] if method in ABLATIONS else []
        if regions and any(not 0 <= value <= entry[2] for value, entry in zip(regions, trace, strict=True)):
            raise ValueError(f"{prefix}: invalid region size")
        data[method] = trace, regions, latency, rows, run
    ordinary = {method: data[method][0] for method in methods if method != "adaptive"}
    expected_traces = None
    if checkpoints is not None:
        expected_traces = {
            method: [(int(row[f"{scope}_deletions"]), int(row[f"{scope}_insertions"]),
                      int(row["active_edges"])) for row in checkpoints]
            for method, scope in ((name, "ordered" if name == "static" else "net") for name in ordinary)
        }
    check_verified_traces(directory, ordinary, expected_traces)
    for method in ("full-fused", "full-fused-pruned"):
        if method in methods and data[method][:2] != data["full"][:2]:
            raise ValueError(f"{directory}/{tag}: {method} changed the net mutation/region trace")
    routes = audit_route(directory, case, tag, data["adaptive"][:4], data["full-fused"][:4], data["static"][:4])
    return data, routes


def percentile(values, q):
    return sorted(values)[math.ceil(q * len(values)) - 1]


def pruning_profiles(directory, case, verified):
    records = []
    for method in PRUNING_PROFILES:
        prefix = directory / f"{method}.profile"
        trace, latency = audit_replay(directory, method, "profile", case["batch_operations"], False)
        if trace != verified[method][0]:
            raise ValueError(f"{prefix}: profile replay disagrees with verification")
        batches = csv_rows(Path(f"{prefix}.batches.csv"))
        runs = csv_rows(Path(f"{prefix}.runs.csv"))
        phases = csv_rows(Path(f"{prefix}.phases.csv"))
        scans = csv_rows(Path(f"{prefix}.scans.csv"))
        expected = [("0", str(batch)) for batch in range(1, len(trace) + 1)]
        if len(runs) != 1 or (runs[0]["dataset"], runs[0]["method"], runs[0]["ablate"]) != (
                case["dataset"], "batch", ABLATIONS[method]):
            raise ValueError(f"{prefix}: profile method or workload differs from its protocol")
        if any([(row["run"], row["batch"]) for row in rows] != expected for rows in (phases, scans)):
            raise ValueError(f"{prefix}: incomplete profile traces")
        fields = ("coalesce_us", "witness_us", "apply_us", "closure_us", "recount_us", "peel_us")
        values = [[float(row[key]) for row in phases] for key in fields]
        if any(not math.isfinite(v) or v < 0 for phase in values for v in phase):
            raise ValueError(f"{prefix}: invalid phase time")
        certified, scanned, degree_sum = 0, 0, 0
        for region, batch, row in zip(verified[method][1], batches, scans, strict=True):
            observed = int(row["region_edges"])
            removed = int(row["certified_edges"])
            visited = int(row["scanned_edges"])
            degrees = int(row["scan_degree_sum"])
            if observed != region or int(batch["region_edges"]) != region or removed + visited != region \
                    or min(removed, visited, degrees) < 0:
                raise ValueError(f"{prefix}: inconsistent peel scan counts")
            certified += removed
            scanned += visited
            degree_sum += degrees
        records.append(dict(
            dataset=case["dataset"], scope=case["scope"], method=method, batches=len(trace),
            mean_instrumented_ms_per_batch=latency,
            **{f"mean_{key.removesuffix('_us')}_ms": mean(phase) / 1000
               for key, phase in zip(fields, values, strict=True)},
            certified_edges=certified, scanned_edges=scanned, scan_degree_sum=degree_sum,
        ))
    return records


def summarize(directory, case, methods, runs, checkpoints, profile_pruned):
    verified, routes = audit_tag(directory, case, methods, checkpoints, "verify")
    details, samples = [], {method: [] for method in methods}
    for trial in range(runs):
        observed, trial_routes = audit_tag(directory, case, methods, checkpoints, f"timing.{trial}")
        if trial_routes != routes or any(observed[method][:2] != verified[method][:2] for method in methods):
            raise ValueError(f"{directory}: timing paths disagree with the verified replay")
        for method, (_, _, latency, rows, run) in observed.items():
            build_s, rss = float(run["build_s"]), int(run["rss_mb"])
            if not math.isfinite(build_s) or build_s < 0 or rss <= 0:
                raise ValueError(f"{directory}: invalid initialization time or peak process RSS")
            samples[method].extend(float(row["elapsed_us"]) / 1000 for row in rows)
            total_s = latency * len(rows) / 1000
            details.append(dict(
                dataset=case["dataset"], scope=case["scope"], method=method, repetition=trial,
                mean_ms_per_batch=latency, mean_maintenance_s=total_s,
                build_s=build_s, build_plus_maintenance_s=build_s + total_s,
                peak_process_rss_mib=rss,
            ))
    graph_stats = workload_stats(case, checkpoints) if checkpoints is not None else {}
    regions = [size / entry[2] if entry[2] else 0
               for size, entry in zip(verified["full"][1], verified["full"][0], strict=True)]
    common = dict(
        dataset=case["dataset"], scope=case["scope"], threads=1,
        batches=len(regions), operations=sum(case["batch_operations"]), verified_batches=len(regions),
        timing_replays=runs, status="verified", window_days=case.get("window_days", ""),
        checkpoint_seconds=case.get("checkpoint_seconds", ""), start_utc=case.get("start_utc", ""),
        end_utc=int(checkpoints[-1]["end_utc_inclusive"]) if checkpoints is not None else "",
        min_active_edges=min(entry[2] for entry in verified["full"][0]),
        mean_active_edges=mean(entry[2] for entry in verified["full"][0]),
        max_active_edges=max(entry[2] for entry in verified["full"][0]),
        min_active_triangles=graph_stats.get("min_active_triangles", ""),
        mean_active_triangles=graph_stats.get("mean_active_triangles", ""),
        max_active_triangles=graph_stats.get("max_active_triangles", ""),
        cancelled_operation_fraction=graph_stats.get("cancelled_operation_fraction", ""),
        mean_net_changes=mean(deleted + inserted for deleted, inserted, _ in verified["full"][0]),
        regional_mean_coverage=mean(regions), regional_p95_coverage=percentile(regions, .95),
    )
    summary = []
    for method in methods:
        trials = [row for row in details if row["method"] == method]
        means = [row["mean_ms_per_batch"] for row in trials]
        summary.append(dict(
            common, method=method, mean_ms_per_batch=mean(means),
            replay_mean_sd_ms=stdev(means) if runs > 1 else "",
            pooled_p95_ms=percentile(samples[method], .95),
            pooled_p99_ms=percentile(samples[method], .99),
            mean_maintenance_s=mean(row["mean_maintenance_s"] for row in trials),
            mean_build_s=mean(row["build_s"] for row in trials),
            mean_build_plus_maintenance_s=mean(row["build_plus_maintenance_s"] for row in trials),
            peak_process_rss_mib=max(row["peak_process_rss_mib"] for row in trials),
            adaptive_static_batches=routes.count("static") if method == "adaptive" else "",
        ))
    dimensions = ("mean_ms_per_batch", "mean_build_s", "peak_process_rss_mib")
    for row in summary:
        row["pareto_mean_build_rss"] = not any(
            all(other[key] <= row[key] for key in dimensions) and
            any(other[key] < row[key] for key in dimensions) for other in summary
        )
    diagnostic = pruning_profiles(directory, case, verified) if profile_pruned else []
    return summary, details, diagnostic


def validate_case(output, case, methods, runs, binaries, env, check_only, profile_pruned):
    for key in ("graph", "stream", "checkpoints", "source_stream", "input_manifest"):
        if key in case and digest(Path(case[key])) != case[f"{key}_sha256"]:
            raise ValueError(f"{case['dataset']}: recorded input {key} changed")
    checkpoints = checkpoint_rows(case)
    directory = output / case["dataset"]
    complete = directory / "complete.json"
    expected = dict(case=case, methods=methods, replays=runs)
    if complete.is_file() or check_only:
        recorded = load_json(complete)
        if recorded.pop("files") != {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}:
            raise ValueError(f"{directory}: measured CSVs differ from the completion record")
        if recorded != expected:
            raise ValueError(f"{directory}: completion record differs from the experiment protocol")
        result = summarize(directory, case, methods, runs, checkpoints, profile_pruned)
    else:
        if directory.exists():
            failed = output / "failed"
            failed.mkdir(exist_ok=True)
            retry = Path(tempfile.mkdtemp(prefix=f"{case['dataset']}.", dir=failed)) / case["dataset"]
            directory.rename(retry)
            print(f"Archived incomplete attempt in {retry}", flush=True)
        directory.mkdir()
        started = perf_counter()
        for method in methods:
            replay(directory, case, method, "verify", binaries, env)
        audit_tag(directory, case, methods, checkpoints, "verify")
        for trial in range(runs):
            offset = trial % len(methods)
            for method in methods[offset:] + methods[:offset]:
                replay(directory, case, method, f"timing.{trial}", binaries, env)
        if profile_pruned:
            for method in PRUNING_PROFILES:
                replay(directory, case, method, "profile", binaries, env)
        result = summarize(directory, case, methods, runs, checkpoints, profile_pruned)
        files = {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}
        write_json(complete, expected | {"files": files})
        print(f"Finished {case['dataset']} in {perf_counter() - started:.1f} s wall", flush=True)
    print(f"AUDIT OK {case['dataset']}: {len(case['batch_operations'])} exact checkpoints", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true", help="use a switch fixture and up to three batches per case")
    mode.add_argument("--check-only", action="store_true", help="audit an existing output and regenerate CSV reports")
    parser.add_argument("--cases", nargs="*", choices=(*DEFAULT_CASES, "amazon"), default=DEFAULT_CASES)
    parser.add_argument("--prepared", nargs=3, action="append", default=[], metavar=("NAME", "INPUT_ROOT", "WINDOW_DAYS"),
                        help="add a frozen temporal projection with a verified manifest")
    parser.add_argument("--include-pruned", action="store_true", help="include the fused-pruned experimental variant")
    parser.add_argument("--profile-pruned", action="store_true",
                        help="separate Fused/Fused-Pruned phase and scan profiles (requires --include-pruned)")
    parser.add_argument("--runs", type=int, help="fresh timing processes per method (default: 3; smoke: 1)")
    parser.add_argument("--cpu", type=int, help="single logical CPU (default: first allowed)")
    parser.add_argument("--star-root", type=Path, default=BUILD / "TrussMaintenance")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if (args.runs is not None and args.runs < 1) or len(set(args.cases)) != len(args.cases):
        parser.error("use positive --runs and distinct --cases")
    if args.check_only and args.output is None:
        parser.error("--check-only requires --output")
    if args.profile_pruned and not args.include_pruned:
        parser.error("--profile-pruned requires --include-pruned")
    name = "vldb2027-unified-dynamic-smoke" if args.smoke_only else "vldb2027-unified-dynamic"
    output = (args.output or BUILD / name).resolve()
    args.star_root = args.star_root.resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    binaries = binary_paths(output)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        plan, env = (load_json(output / "protocol.json"), None) if args.check_only else setup(args, output, binaries)
        methods, runs = plan["methods"], plan["timing_replays"]
        profile_pruned = plan.get("profile_pruned", False)
        if (tuple(methods) not in (METHODS, (*METHODS, "full-fused-pruned"))
                or runs < 1 or plan["threads"] != 1 or plan["timer_scope"] != SCOPE
                or plan["adaptive_policy"] != POLICY or not plan["cases"]
                or (profile_pruned and "full-fused-pruned" not in methods)
                or {method: digest(path) for method, path in binaries.items()} != plan["binary_sha256"]):
            raise ValueError("saved build or comparison protocol differs")
        print(f"Run directory: {output} | {plan['machine']['host']} | CPU {plan['cpu']}", flush=True)
        summary, details, diagnostics = [], [], []
        for case in plan["cases"]:
            rows, repetitions, profile = validate_case(
                output, case, methods, runs, binaries, env, args.check_only, profile_pruned)
            summary.extend(rows)
            details.extend(repetitions)
            diagnostics.extend(profile)
        write_summary(output, summary)
        write_summary(output, details, filename="timing_replays.csv")
        if diagnostics:
            write_summary(output, diagnostics, filename="pruning_profile.csv")
    print(f"REPORT {output / 'summary.csv'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
