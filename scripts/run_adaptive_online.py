#!/usr/bin/env python3
"""Calibrate online routing on an early period and measure it on later snapshots."""

import argparse
from datetime import date
import fcntl
from math import isclose, isfinite
import os
from pathlib import Path
from statistics import mean
import subprocess
from tempfile import mkdtemp
from time import perf_counter

from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, run_logged, write_summary
from prepare_large_temporal_replay import RAW_SHA256
from revision_experiments import BUILD, ROOT, load_json, machine, write_json
from run_adaptive_comparison import audit_route, operation_digest, switch_fixture
from run_review_controls import FLAGS
from run_unified_dynamic import checkpoint_rows, first_batches, temporal_case
from summarize_adaptive_online import summarize_case


FIXED_AND_LEGACY = {
    "static": ("static",),
    "full-fused": ("batch", "--ablate", "fused"),
    "full-fused-pruned": ("batch", "--ablate", "fused-pruned"),
    "adaptive": ("batch", "--ablate", "adaptive"),
}
ONLINE = ("adaptive-recent", "adaptive-cost")
PRUNED_ONLINE = ("adaptive-pruned-recent", "adaptive-pruned-cost")
METHODS = FIXED_AND_LEGACY | {method: ("batch", "--ablate", method) for method in ONLINE}
PRUNED_METHODS = FIXED_AND_LEGACY | {method: ("batch", "--ablate", method) for method in PRUNED_ONLINE}
PERIODS = (("calibration", "2014-02-01", 84), ("test", "2014-05-03", 168))
CADENCES = (1, 14)
PROBE_CANDIDATES = (4, 8)
SCOPE = ("Steady-clock maintenance including dispatch and feature collection. "
         "Loading, construction, verification, and output are separate.")
SELECTION = ("Choose one common interval by the lowest mean ratio to the better of "
             "Static and Fused on the early period, weighting both cadences and both "
             "new policies equally; ties use the shorter interval. Freeze it for the later period.")
PRUNED_SELECTION = SELECTION.replace("Static and Fused", "Static and Fused-Pruned")


def sources():
    scripts = ("run_adaptive_online", "summarize_adaptive_online", "prepare_large_temporal_replay",
               "run_temporal_windows", "run_unified_dynamic", "run_adaptive_comparison",
               "run_review_controls", "compare_replay", "compare_star_resident", "revision_experiments")
    paths = [ROOT / "src/main.cpp", *sorted((ROOT / "src").glob("*.h")),
             *(ROOT / "scripts" / f"{name}.py" for name in scripts)]
    return {str(path.relative_to(ROOT)): digest(path) for path in paths}


def temporal_inputs(output, env, check_only, prepared_root=None):
    for role, start, length in PERIODS:
        cases = []
        start_date = date.fromisoformat(start)
        for cadence in CADENCES:
            name = f"stackoverflow-2014-{role}-b{cadence}d"
            directory = (prepared_root or output / "prepared") / name
            if not check_only and prepared_root is None and not (directory / "protocol.json").is_file():
                print(f"PREPARE {name} from SNAP", flush=True)
                subprocess.run(
                    ["python3", "-B", "scripts/prepare_large_temporal_replay.py",
                     "--start-date", start, "--replay-days", str(length), "--retention-days", "7",
                     "--checkpoint-days", str(cadence), "--output", directory],
                    cwd=ROOT, env=env, check=True)
            case = temporal_case(name, directory, 7)
            day = 86400
            expected_timestamp = (start_date - date(1970, 1, 1)).days * day
            if (case["raw_sha256"], case["start_utc"], case["end_utc"],
                    case["checkpoint_seconds"], len(case["batch_operations"])) != (
                    RAW_SHA256, expected_timestamp, expected_timestamp + length * day,
                    cadence * day, length // cadence):
                raise ValueError(f"{directory}: prepared workload has different source, dates or cadence")
            cases.append(case | {"role": role})
        daily, coarse = cases
        if (daily["graph_sha256"] != coarse["graph_sha256"]
                or operation_digest(Path(daily["stream"])) != operation_digest(Path(coarse["stream"]))):
            raise ValueError(f"{role}: cadence variants must have the same initial graph and ordered stream")
        yield role, cases


def run_replay(directory, case, method, tag, binary, env, methods):
    prefix = directory / f"{method}.{tag}"
    command = [
        binary, *methods[method], "--graph", case["graph"], "--stream", case["stream"],
        "--dataset", case["dataset"], "--threads", "1", "--runs", "1",
        "--batch-csv", f"{prefix}.batches.csv",
    ]
    command += ["--check-every", "1"] if tag == "verify" else ["--timing-only"]
    if method == "adaptive":
        command += ["--route-csv", f"{prefix}.routes.csv"]
    elif method in (*ONLINE, *PRUNED_ONLINE):
        command += ["--online-route-csv", f"{prefix}.routes.csv", "--online-probe-interval",
                    str(case["probe_interval"])]
    wall = run_logged(command, Path(f"{prefix}.runs.csv"), Path(f"{prefix}.stderr"), env=env)
    _, latency = audit_replay(directory, method, tag, case["batch_operations"], tag == "verify")
    print(f"{case['dataset']} {method} {tag}: {len(case['batch_operations'])} batches, "
          f"{wall:.2f} s wall, {latency:.3f} ms/batch", flush=True)


def audit_online(prefix, case, rows, batches, traces, fixed, regional_route="fused", uses_degree=False):
    if len(rows) != len(batches):
        raise ValueError(f"{prefix}: missing online route records")
    previous = None
    previous_region, previous_degree = 0, 0
    for i, (row, batch, trace) in enumerate(zip(rows, batches, traces, strict=True)):
        route = row["route"]
        if (row["run"], row["batch"], int(row["raw_ops"])) != ("0", str(i + 1), case["batch_operations"][i]):
            raise ValueError(f"{prefix}: route record differs from the registered batch")
        if route not in fixed:
            raise ValueError(f"{prefix}: unknown route {route}")
        switched = previous is not None and previous != route
        if int(row["switched"]) != switched:
            raise ValueError(f"{prefix}: incorrect switch marker at batch {i + 1}")
        if (i < 2 and (row["reason"], route) != ("warmup", (regional_route, "static")[i])):
            raise ValueError(f"{prefix}: warmup did not measure both paths")
        if i >= 2:
            reason = "probe" if i % case["probe_interval"] == 0 else "predicted"
            if row["reason"] != reason:
                raise ValueError(f"{prefix}: decision reason disagrees with the probe schedule")
        pre_edges = int(row["pre_edges"])
        expected_pre_edges = (traces[i - 1][2] if i else trace[2] - trace[1] + trace[0])
        if (pre_edges, int(row["prior_fused_region_edges"]), int(row["prior_fused_degree_sum"])) != (
                expected_pre_edges, previous_region, previous_degree):
            raise ValueError(f"{prefix}: lagged features disagree at batch {i + 1}")
        chosen = fixed[route]
        region = int(row["region_edges"])
        degree = int(row["region_degree_sum"])
        if trace != chosen[0][i] or region != chosen[1][i] or region != int(batch["region_edges"]):
            raise ValueError(f"{prefix}: executed path or region disagrees with its fixed counterpart")
        if degree < 0 or (route == "static" and degree) or (region == 0 and degree):
            raise ValueError(f"{prefix}: invalid measured region degree")
        if uses_degree and degree < 2 * region:
            raise ValueError(f"{prefix}: missing or incomplete region degree measurement")
        decision, path = (float(row[field]) for field in ("decision_us", "path_us"))
        if (not all(isfinite(value) and value >= 0 for value in (decision, path))
                or decision + path > float(batch["elapsed_us"]) + 0.1):
            raise ValueError(f"{prefix}: invalid phase timings")
        if route == regional_route:
            previous_region, previous_degree = region, degree
        previous = route


def audit_tag(directory, case, tag, checkpoints, methods=METHODS):
    data = {}
    online = tuple(method for method in (*ONLINE, *PRUNED_ONLINE) if method in methods)
    for method, command in methods.items():
        prefix = directory / f"{method}.{tag}"
        trace, _ = audit_replay(directory, method, tag, case["batch_operations"], tag == "verify")
        run, = csv_rows(Path(f"{prefix}.runs.csv"))
        expected_ablation = command[2] if command[0] == "batch" else "-"
        if (run["dataset"], run["method"], run["ablate"]) != (case["dataset"], command[0], expected_ablation):
            raise ValueError(f"{prefix}: method or workload differs from the registered case")
        batches = csv_rows(Path(f"{prefix}.batches.csv"))
        regions = [int(row["region_edges"]) for row in batches]
        if any(not 0 <= region <= row[2] for region, row in zip(regions, trace, strict=True)):
            raise ValueError(f"{prefix}: invalid region edge count")
        routes = csv_rows(Path(f"{prefix}.routes.csv")) if method == "adaptive" or method in online else []
        data[method] = dict(trace=trace, regions=regions, batches=batches, routes=routes, run=run)

    def pair(method):
        return data[method]["trace"], data[method]["regions"]

    if pair("full-fused") != pair("full-fused-pruned"):
        raise ValueError(f"{directory}/{tag}: pruning changed net changes or closure sizes")
    fixed = {"static": pair("static"), "fused": pair("full-fused")}
    if [row[2] for row in fixed["static"][0]] != [row[2] for row in fixed["fused"][0]]:
        raise ValueError(f"{directory}/{tag}: baseline edge counts disagree")
    if checkpoints is not None:
        for route, trace in fixed.items():
            regime = "ordered" if route == "static" else "net"
            expected = [(int(row[f"{regime}_deletions"]), int(row[f"{regime}_insertions"]),
                         int(row["active_edges"])) for row in checkpoints]
            if trace[0] != expected:
                raise ValueError(f"{directory}/{tag}: {route} differs from the prepared snapshots")
    adaptive = data["adaptive"]
    audit_route(directory, case, tag,
                (adaptive["trace"], adaptive["regions"], None, None), fixed["fused"], fixed["static"])
    for method in online:
        item = data[method]
        regional = "fused-pruned" if method in PRUNED_ONLINE else "fused"
        paths = {"static": fixed["static"], regional: pair(f"full-{regional}")}
        audit_online(directory / f"{method}.{tag}", case, item["routes"], item["batches"], item["trace"],
                     paths, regional, method.endswith("-cost"))
    return data


def run_case(output, case, runs, binary, env, check_only, methods):
    directory = output / case["dataset"]
    complete = directory / "complete.json"
    expected = dict(case=case, methods=list(methods), timing_replays=runs)
    ready = complete.is_file()
    if ready and load_json(complete) != expected:
        raise ValueError(f"{directory}: completion record disagrees with the current protocol")
    if check_only and not ready:
        raise ValueError(f"{directory}: incomplete case")
    if not ready:
        if directory.exists():
            failed = output / "failed"
            failed.mkdir(exist_ok=True)
            directory.rename(Path(mkdtemp(prefix=f"{directory.name}.", dir=failed)) / directory.name)
        directory.mkdir()
    if any(digest(Path(case[key])) != case[f"{key}_sha256"]
           for key in ("graph", "stream", "checkpoints", "input_manifest") if key in case):
        raise ValueError(f"{directory}: reproducible input checksum differs")
    checkpoints = checkpoint_rows(case)
    tags = ("verify", *(f"timing.{i}" for i in range(runs)))
    observations = {method: [] for method in methods}
    started = perf_counter()
    order = tuple(methods)
    for index, tag in enumerate(tags):
        if not ready:
            offset = 0 if index == 0 else (index - 1) * 2 % len(order)
            for method in order[offset:] + order[:offset]:
                run_replay(directory, case, method, tag, binary, env, methods)
        data = audit_tag(directory, case, tag, checkpoints, methods)
        for method in methods:
            if index == 0:
                continue
            if method not in (*ONLINE, *PRUNED_ONLINE) and (data[method]["trace"], data[method]["regions"]) != (
                    verified[method]["trace"], verified[method]["regions"]):
                raise ValueError(f"{directory}/{tag}: {method} differs from the verification replay")
            observations[method].append(data[method])
        if index == 0:
            verified = data
    if not ready:
        write_json(complete, expected)
    print(f"AUDIT OK {case['dataset']}: {len(case['batch_operations'])} exact checkpoints, "
          f"{runs} timed replays per method ({perf_counter() - started:.1f} s)", flush=True)
    return observations


def calibration_score(observations, online=ONLINE, regional="full-fused"):
    def total(method):
        return mean(sum(float(row["elapsed_us"]) for row in replay["batches"])
                    for replay in observations[method])
    baseline = min(total("static"), total(regional))
    return mean(total(method) / baseline for method in online)


def execute(output, runs, smoke, binary, env, check_only, pruned_routes=False, prepared_root=None):
    reports = ([], [], [])
    methods = PRUNED_METHODS if pruned_routes else METHODS
    online = PRUNED_ONLINE if pruned_routes else ONLINE
    regional = "full-fused-pruned" if pruned_routes else "full-fused"
    selection_rule = PRUNED_SELECTION if pruned_routes else SELECTION
    regional_by_method = dict.fromkeys(online, regional) if pruned_routes else None

    def record(case):
        observations = run_case(output, case, runs, binary, env, check_only, methods)
        for target, source in zip(reports, summarize_case(case, observations, regional_by_method), strict=True):
            target.extend(source)
        return observations

    if smoke:
        fixture = switch_fixture(output) if not check_only else load_json(
            output / "adaptive-switch/complete.json")["case"]
        record(fixture | {"role": "smoke", "probe_interval": 4})
        prefixes = {"stackoverflow-prefix": BUILD / "vldb2027-stackoverflow-holdout-112d-b1d"}
        if prepared_root is not None:
            prefixes = {f"stackoverflow-prefix-b{days}d": prepared_root / f"stackoverflow-2014-test-b{days}d"
                        for days in CADENCES}
        for name, source in prefixes.items():
            if check_only:
                prefix = load_json(output / name / "complete.json")["case"]
            else:
                prepared_name = "stackoverflow" if prepared_root is None else name
                prefix = first_batches(temporal_case(prepared_name, source, 7), output)
                prefix |= {"dataset": name, "role": "smoke", "probe_interval": 8}
            record(prefix)
    else:
        selection = output / "calibration.json"
        for role, cases in temporal_inputs(output, env, check_only, prepared_root):
            if role == "calibration":
                scores = {}
                for interval in PROBE_CANDIDATES:
                    measured = [
                        record(case | {"dataset": f"{case['dataset']}-p{interval}", "probe_interval": interval})
                        for case in cases
                    ]
                    scores[str(interval)] = mean(calibration_score(group, online, regional) for group in measured)
                selected = min(PROBE_CANDIDATES, key=lambda interval: (scores[str(interval)], interval))
                decision = {"probe_candidates": list(PROBE_CANDIDATES), "scores": scores,
                            "selected_probe_interval": selected, "selection_rule": selection_rule}
                if selection.is_file():
                    recorded = load_json(selection)
                    recorded_scores = recorded.pop("scores")
                    if (recorded != {key: value for key, value in decision.items() if key != "scores"}
                            or recorded_scores.keys() != scores.keys()
                            or any(not isclose(recorded_scores[key], value, rel_tol=1e-10, abs_tol=1e-10)
                                   for key, value in scores.items())):
                        raise ValueError("recorded calibration selection differs from the audited trials")
                elif check_only:
                    raise ValueError("missing pre-test calibration selection")
                else:
                    write_json(selection, decision)
                print(f"CALIBRATED probe interval {selected}; frozen before test replay", flush=True)
            else:
                for case in cases:
                    record(case | {"probe_interval": selected})
    for filename, rows in zip(("summary.csv", "timing_replays.csv", "route_observations.csv"),
                              reports, strict=True):
        write_summary(output, rows, filename=filename)
    print(f"REPORT {output / 'summary.csv'}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--smoke-only", action="store_true", help="run a switch fixture and three real batches")
    mode.add_argument("--check-only", action="store_true", help="audit and summarize existing results")
    parser.add_argument("--pruned-routes", action="store_true", help="route between Static and Fused-Pruned")
    parser.add_argument("--prepared-root", type=Path, help="reuse four frozen calibration/test projections")
    parser.add_argument("--runs", type=int, help="timed replays per method (default: 3; smoke: 1)")
    parser.add_argument("--cpu", type=int, help="pin to this CPU (default: first allowed)")
    parser.add_argument("--output", type=Path, help="output directory holding protocol, logs and reports")
    args = parser.parse_args()
    if (args.runs is not None and args.runs < 1
            or args.check_only and (args.output is None or args.runs is not None or args.cpu is not None
                                    or args.pruned_routes or args.prepared_root is not None)):
        parser.error("use positive --runs; --check-only takes only --output")
    base_name = "vldb2027-main4-adaptive-pruned" if args.pruned_routes else "vldb2027-main3-adaptive-online"
    default = BUILD / (f"{base_name}-smoke" if args.smoke_only else base_name)
    output = (args.output or default).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    protocol = output / "protocol.json"
    binary = output / "build/batchtruss"
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
        env |= {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
        if args.check_only:
            plan = load_json(protocol)
        else:
            prepared_root = args.prepared_root.resolve(strict=True) if args.prepared_root else None
            identity, hardware = machine()
            cpu = identity["affinity"][0] if args.cpu is None else args.cpu
            if cpu not in identity["affinity"]:
                raise ValueError(f"CPU {cpu} is outside allowed affinity {identity['affinity']}")
            os.sched_setaffinity(0, {cpu})
            plan = {
                "version": 2 if args.pruned_routes else 1, "machine": identity, "cpu": cpu, "threads": 1,
                "smoke_only": args.smoke_only, "timing_replays": args.runs or (1 if args.smoke_only else 3),
                "sources": sources(), "flags": list(FLAGS), "periods": [list(period) for period in PERIODS],
                "cadences": list(CADENCES), "probe_candidates": list(PROBE_CANDIDATES),
                "selection_rule": PRUNED_SELECTION if args.pruned_routes else SELECTION,
                "timing_scope": SCOPE, "methods": list(PRUNED_METHODS if args.pruned_routes else METHODS),
                "compiler": subprocess.check_output(["g++", "--version"], env=env, text=True).splitlines()[0],
            }
            if prepared_root is not None:
                plan["prepared_root"] = str(prepared_root)
            if protocol.is_file():
                recorded = load_json(protocol)
                if {key: val for key, val in recorded.items() if key != "binary_sha256"} != plan:
                    raise ValueError(f"{output}: host, inputs, tools or settings changed; use a fresh output")
                plan = recorded
            else:
                binary.parent.mkdir(exist_ok=True)
                run_logged(["g++", *FLAGS, "src/main.cpp", "-o", binary],
                           binary.parent / "build.log", binary.parent / "build.stderr", env=env)
                if sources() != plan["sources"]:
                    raise ValueError("sources changed while building")
                plan["binary_sha256"] = digest(binary)
                (output / "hardware.txt").write_text(hardware)
                write_json(protocol, plan)
        pruned_routes = plan["version"] == 2
        methods = PRUNED_METHODS if pruned_routes else METHODS
        selection_rule = PRUNED_SELECTION if pruned_routes else SELECTION
        if (plan["version"] not in (1, 2)
                or (plan["methods"], plan["periods"], plan["cadences"], plan["probe_candidates"],
                    plan["selection_rule"], plan["timing_scope"]) != (
                    list(methods), [list(period) for period in PERIODS], list(CADENCES),
                    list(PROBE_CANDIDATES), selection_rule, SCOPE)):
            raise ValueError("recorded run protocol differs from the online comparison")
        if digest(binary) != plan["binary_sha256"]:
            raise ValueError(f"{binary}: archived executable checksum differs")
        print(f"Run directory: {output} | {plan['machine']['host']} | CPU {plan['cpu']}", flush=True)
        prepared_root = Path(plan["prepared_root"]) if "prepared_root" in plan else None
        execute(output, plan["timing_replays"], plan["smoke_only"], binary, env, args.check_only,
                pruned_routes, prepared_root)
        print(f"ALL AUDITS OK: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}") from error
