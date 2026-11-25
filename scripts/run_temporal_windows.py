#!/usr/bin/env python3
"""Compare Full, Static and resident Star on registered timestamp streams."""

import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import csv
from datetime import date, datetime, time, timezone
import fcntl
import gzip
from math import ceil, isfinite
import os
from pathlib import Path
from statistics import mean, median, stdev
import subprocess
import tempfile
from time import perf_counter

from compare_replay import csv_rows, digest
from compare_star_resident import build, write_summary
from experiment_replays import audit_paired_case, run_paired_case
from revision_experiments import ROOT, load_json, machine, write_json


DAY = 86400
WINDOWS = (7, 30, 90)
METHODS = ("full", "static", "star-resident")
# Use the last complete UTC calendar year in the registered trace.
START = int(datetime(2015, 1, 1, tzinfo=timezone.utc).timestamp())
END = int(datetime(2016, 1, 1, tzinfo=timezone.utc).timestamp())
ARCHIVE = ROOT / ".build/vldb2027-askubuntu.PAgZvq"
# Four complete weeks from the start of the same calendar year, fixed before
# running the comparison. Stack Overflow keeps the primary 7-day retention.
REVIEW_END = START + 28 * DAY
PRESETS = {
    "askubuntu": ("Ask Ubuntu", WINDOWS, END),
    "askubuntu-large": ("Ask Ubuntu", (365,), REVIEW_END),
    "stackoverflow": ("Stack Overflow", (7,), REVIEW_END),
}
STACK_OVERFLOW = ROOT / "data/sx-stackoverflow.txt.gz"
STACK_OVERFLOW_URL = "https://snap.stanford.edu/data/sx-stackoverflow.txt.gz"
SCOPE = (
    "steady_clock per batch including ordered graph updates (or their net reduction) "
    "and exact label maintenance; excludes input, initialization, output, and verification"
)


def comparison_methods(fused, static_net, certified=False):
    methods = list(METHODS)
    if fused:
        methods.insert(1, "full-fused")
    if certified:
        methods.insert(1, "full-certified")
    if static_net:
        methods.insert(-1, "static-net")
    return tuple(methods)


def interactions(raw, *, start=START, end=END, windows=WINDOWS):
    """Consolidate endpoint IDs across all windows using the shared date range."""
    earliest = start - max(windows) * DAY
    retained = []
    nodes = set()
    first = last = None
    with gzip.open(raw, "rt", encoding="ascii") as source:
        for line in source:
            fields = line.partition("#")[0].split()
            if not fields:
                continue
            source_id, destination_id, raw_time = fields
            timestamp = int(raw_time)
            first = timestamp if first is None else min(first, timestamp)
            last = timestamp if last is None else max(last, timestamp)
            if earliest < timestamp <= end:
                u, v = int(source_id), int(destination_id)
                if u != v:
                    a, b = sorted((u, v))
                    retained.append((timestamp, a, b))
                    nodes.update((a, b))
    if first is None or first > earliest or last < end or not retained:
        raise ValueError("input does not cover the common evaluation interval and its warm-up")
    ids = {node: i for i, node in enumerate(sorted(nodes))}
    retained.sort()
    times = [timestamp for timestamp, _, _ in retained]
    edges = [(ids[u], ids[v]) for _, u, v in retained]
    return len(ids), times, edges


def triangle_count(edges):
    """Count a triangle once by orienting every edge in degree order."""
    degrees = Counter(vertex for edge in edges for vertex in edge)
    outgoing = defaultdict(set)
    for u, v in edges:
        if (degrees[u], u) > (degrees[v], v):
            u, v = v, u
        outgoing[u].add(v)
    return sum(len(neighbors & outgoing[v]) for neighbors in outgoing.values()
               for v in neighbors if v in outgoing)


def project(directory, nodes, times, interactions_by_time, days, end, *, start=START, step=DAY):
    length = days * DAY
    expiry_pos = bisect_right(times, start - length)
    arrival_pos = bisect_right(times, start)
    active = Counter(interactions_by_time[expiry_pos:arrival_pos])
    previous = set(active)
    graph = directory / "graph.txt"
    stream = directory / "updates.stream"
    checkpoints = directory / "checkpoints.csv"
    with graph.open("w") as file:
        # DynGraph uses first encounter to map IDs and ignores these self-loops.
        file.writelines(f"{node} {node}\n" for node in range(nodes))
        file.writelines(f"{u} {v}\n" for u, v in sorted(previous))

    with stream.open("w") as output, checkpoints.open("w", newline="") as check_file:
        writer = csv.writer(check_file)
        writer.writerow(("batch", "start_utc_exclusive", "end_utc_inclusive", "operations",
                         "ordered_deletions", "ordered_insertions", "net_deletions",
                         "net_insertions", "active_edges", "active_triangles"))
        for batch, endpoint in enumerate(range(start + step, end + 1, step), 1):
            ops = []
            ordered_deletions = 0
            # On equal timestamps, apply every expiration/arrival before exposing
            # set membership. The window is open left and closed right.
            arrival_end = bisect_right(times, endpoint)
            expiry_end = bisect_right(times, endpoint - length)
            while arrival_pos < arrival_end or expiry_pos < expiry_end:
                t_arrival = times[arrival_pos] if arrival_pos < arrival_end else endpoint + 1
                t_expiry = times[expiry_pos] + length if expiry_pos < expiry_end else endpoint + 1
                timestamp = min(t_arrival, t_expiry)
                deltas = Counter()
                while arrival_pos < arrival_end and times[arrival_pos] == timestamp:
                    deltas[interactions_by_time[arrival_pos]] += 1
                    arrival_pos += 1
                while expiry_pos < expiry_end and times[expiry_pos] + length == timestamp:
                    deltas[interactions_by_time[expiry_pos]] -= 1
                    expiry_pos += 1
                for edge, delta in sorted(deltas.items()):
                    old = active[edge]
                    new = old + delta
                    if new < 0:
                        raise ValueError(f"negative interaction multiplicity at {timestamp}: {edge}")
                    if new:
                        active[edge] = new
                    else:
                        active.pop(edge, None)
                    if bool(old) != bool(new):
                        code = "I" if new else "D"
                        ops.append((code, *edge))
                        ordered_deletions += code == "D"

            # Independent raw-snapshot check: count neither incremental activities
            # nor emitted operations, just deduplicate the timestamp slice.
            direct = set(interactions_by_time[expiry_end:arrival_end])
            if active.keys() != direct:
                raise ValueError(f"{days}-day window projection mismatch at {endpoint}")
            output.write(f"B {len(ops)}\n")
            output.writelines(f"{op} {u} {v}\n" for op, u, v in ops)
            writer.writerow((batch, endpoint - step, endpoint, len(ops), ordered_deletions,
                             len(ops) - ordered_deletions, len(previous - direct),
                             len(direct - previous), len(direct), triangle_count(direct)))
            previous = direct


def prepare(output, raw, end, signature):
    prepared = output / "inputs"
    manifest = prepared / "manifest.json"
    if prepared.exists():
        recorded = load_json(manifest)
        if recorded["protocol"] != signature:
            raise ValueError("prepared inputs use a different experiment protocol")
        if any(digest(prepared / name) != value for name, value in recorded["sha256"].items()):
            raise ValueError("prepared inputs have changed")
        return prepared
    if raw is None:
        raise ValueError(f"missing prepared inputs in {output}")
    started = perf_counter()
    print(f"Preparing temporal inputs under {output}", flush=True)
    start = signature["start_utc"]
    windows = signature["window_days"]
    # Smoke runs retain the same vertex mapping as their full registered
    # protocol, even though they emit only the first three checkpoints.
    input_end = signature.get("projection_end_utc", END)
    # Selecting a single window keeps the vertex mapping of the full sweep.
    projection_windows = signature.get("projection_window_days", windows)
    nodes, times, edges = interactions(raw, start=start, end=input_end, windows=projection_windows)
    with tempfile.TemporaryDirectory(prefix=".inputs.", dir=output) as temp:
        staged = Path(temp) / "inputs"
        staged.mkdir()
        for days in windows:
            directory = staged / f"w{days}d"
            directory.mkdir()
            project(directory, nodes, times, edges, days, end, start=start,
                    step=signature["step_seconds"])
        write_json(staged / "manifest.json", {
            "protocol": signature, "vertices": nodes,
            "wall_s": perf_counter() - started,
            "sha256": {str(path.relative_to(staged)): digest(path)
                       for path in sorted(staged.glob("*/*"))},
        })
        staged.rename(prepared)
    print(f"Prepared and verified direct raw snapshots in {perf_counter() - started:.1f} s", flush=True)
    return prepared


def experiment_case(prepared, days, end, *, dataset="askubuntu", start=START, step=DAY,
                    methods=METHODS):
    directory = prepared / f"w{days}d"
    checkpoints = csv_rows(directory / "checkpoints.csv")
    expected_endpoints = range(start + step, end + 1, step)
    if [(int(row["batch"]), int(row["start_utc_exclusive"]), int(row["end_utc_inclusive"]))
            for row in checkpoints] != [(i, t - step, t) for i, t in enumerate(expected_endpoints, 1)]:
        raise ValueError(f"checkpoints differ from the shared calendar: {directory}")
    case = {
        "dataset": f"{dataset}-w{days}d",
        "graph": str(directory / "graph.txt"),
        "stream": str(directory / "updates.stream"),
        "window_days": days,
        "batch_operations": [int(row["operations"]) for row in checkpoints],
    }
    traces_by_scope = {
        scope: [(int(row[f"{scope}_deletions"]), int(row[f"{scope}_insertions"]), int(row["active_edges"]))
                for row in checkpoints]
        for scope in ("ordered", "net")
    }
    traces = {method: traces_by_scope["ordered" if method == "static" else "net"] for method in methods}
    return case, checkpoints, traces


def workload_stats(case, checkpoints):
    active_edges = [int(row["active_edges"]) for row in checkpoints]
    active_triangles = [int(row["active_triangles"]) for row in checkpoints]
    signed_counts = [
        tuple(int(row[key]) for key in (
            "operations", "ordered_deletions", "ordered_insertions", "net_deletions", "net_insertions"
        ))
        for row in checkpoints
    ]
    if any(raw != del_ordered + ins_ordered or del_ordered - net_del != ins_ordered - net_ins
           or not 0 <= net_del <= del_ordered
           for raw, del_ordered, ins_ordered, net_del, net_ins in signed_counts):
        raise ValueError(f"{case['dataset']}: inconsistent mutation counts")
    cancellations = [raw - net_del - net_ins for raw, _, _, net_del, net_ins in signed_counts]
    raw_events = sum(case["batch_operations"])
    if raw_events <= 0:
        raise ValueError(f"{case['dataset']}: interval has no active membership changes")
    return dict(
        dataset=case["dataset"], window_days=case["window_days"],
        batches=len(checkpoints), operations=raw_events,
        ordered_deletions=sum(row[1] for row in signed_counts),
        ordered_insertions=sum(row[2] for row in signed_counts),
        net_deletions=sum(row[3] for row in signed_counts),
        net_insertions=sum(row[4] for row in signed_counts),
        cancelled_operations=sum(cancellations),
        cancelled_operation_fraction=sum(cancellations) / raw_events,
        cancellation_checkpoints=sum(count > 0 for count in cancellations),
        min_active_edges=min(active_edges), mean_active_edges=mean(active_edges),
        max_active_edges=max(active_edges), min_active_triangles=min(active_triangles),
        mean_active_triangles=mean(active_triangles), max_active_triangles=max(active_triangles),
    )


def summarize(directory, case, checkpoints, runs, *, methods=METHODS):
    workload = workload_stats(case, checkpoints)
    result = []
    checkpoint_medians = {}
    for method in methods:
        trials, build_times, process_peaks = [], [], []
        for trial in range(runs):
            prefix = directory / f"{method}.timing.{trial}"
            batches = csv_rows(Path(f"{prefix}.batches.csv"))
            trials.append([float(row["elapsed_us"]) / 1000 for row in batches])
            run, = csv_rows(Path(f"{prefix}.runs.csv"))
            build_s = float(run["build_s"])
            rss_mib = int(run["rss_mb"])
            if not isfinite(build_s) or build_s < 0 or rss_mib <= 0:
                raise ValueError(f"{prefix}: invalid construction time or process RSS")
            build_times.append(build_s)
            process_peaks.append(rss_mib)
        checkpoint_medians[method] = [median(times) for times in zip(*trials, strict=True)]
        latency = sorted(sample for replay in trials for sample in replay)
        result.append(dict(
            workload, method=method, replays=runs, threads=1, mean_ms_per_batch=mean(latency),
            replay_mean_sd_ms=stdev(mean(replay) for replay in trials) if runs > 1 else "",
            pooled_p95_ms=latency[ceil(len(latency) * .95) - 1],
            mean_build_s=mean(build_times),
            build_sd_s=stdev(build_times) if runs > 1 else "",
            min_peak_process_rss_mib=min(process_peaks),
            max_peak_process_rss_mib=max(process_peaks),
            status="verified",
        ))
    for row in result:
        method = row["method"]
        row["fastest_checkpoints"] = sum(
            all(value < checkpoint_medians[other][index] for other in methods if method != other)
            for index, value in enumerate(checkpoint_medians[method])
        )
        row["latency_over_full"] = row["mean_ms_per_batch"] / result[0]["mean_ms_per_batch"]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", choices=PRESETS, default="askubuntu",
                        help="registered replay (Stack Overflow: 7-day retention; "
                             "Ask Ubuntu large: 365-day retention; both use the first 28 days of 2015 UTC)")
    parser.add_argument("--raw", type=Path,
                        help=f"Stack Overflow SNAP gzip file (default: {STACK_OVERFLOW})")
    parser.add_argument("--fused", action="store_true", help="include Full-Fused alongside the three default methods")
    parser.add_argument("--certified", action="store_true",
                        help="include Full-Certified with low-support edges prelabelled before peeling")
    parser.add_argument("--static-net", action="store_true", help="also time full-graph peeling after net updates")
    parser.add_argument("--window-days", type=int, help="run only this retention window from the selected preset")
    parser.add_argument("--start-date", type=date.fromisoformat,
                        help="first UTC date (YYYY-MM-DD; default: 2015-01-01)")
    parser.add_argument("--replay-days", type=int,
                        help="number of UTC days to replay, excluding window warm-up")
    parser.add_argument("--checkpoint-days", type=int,
                        help="UTC days per batch (default: 1); retention still follows the dataset preset")
    parser.add_argument("--smoke-only", action="store_true", help="verify and time up to three complete checkpoints")
    parser.add_argument("--runs", type=int, help="independent timing replays (default: 3; smoke: 1)")
    parser.add_argument("--cpu", type=int, help="pin to this allowed logical CPU (default: first allowed)")
    parser.add_argument("--output", type=Path, help="experiment directory for compatible resumes and auditing")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--prepare-only", action="store_true",
                      help="prepare and verify the input projection, report workload size without benchmarking")
    mode.add_argument("--check-only", action="store_true", help="audit an existing --output and regenerate summary")
    parser.add_argument("--star-root", type=Path, default=ROOT / ".build/TrussMaintenance")
    args = parser.parse_args()
    if args.check_only and args.output is None:
        parser.error("--check-only requires --output")
    if args.raw is not None and (args.dataset != "stackoverflow" or args.check_only):
        parser.error("--raw requires a new or resumable Stack Overflow replay")
    custom_interval = (args.start_date, args.replay_days, args.checkpoint_days)
    if args.check_only and (args.window_days is not None or any(value is not None for value in custom_interval)):
        parser.error("--check-only takes its interval and window from the saved protocol")
    if any(value is not None and value <= 0 for value in custom_interval[1:]):
        parser.error("use positive replay and checkpoint lengths")
    runs = args.runs if args.runs is not None else (1 if args.smoke_only else 3)
    if runs < 1:
        parser.error("--runs must be positive")
    dataset, projection_windows, projection_end = PRESETS[args.dataset]
    windows = projection_windows
    if args.window_days is not None:
        if args.window_days not in projection_windows:
            parser.error(f"--window-days for {args.dataset} must be one of {projection_windows}")
        windows = (args.window_days,)
    start = START if args.start_date is None else int(
        datetime.combine(args.start_date, time.min, timezone.utc).timestamp())
    step_days = 1 if args.checkpoint_days is None else args.checkpoint_days
    step = step_days * DAY
    if args.replay_days is not None:
        projection_end = start + args.replay_days * DAY
    if projection_end <= start or (projection_end - start) % step:
        parser.error("the replay interval must contain a positive whole number of checkpoints")
    end = min(start + 3 * step, projection_end) if args.smoke_only else projection_end
    default = "vldb2027-temporal-windows" if args.dataset == "askubuntu" else f"vldb2027-temporal-{args.dataset}"
    if any(value is not None for value in custom_interval):
        first_date = datetime.fromtimestamp(start, timezone.utc).strftime("%Y%m%d")
        default += f"-{first_date}-{(projection_end - start) // DAY}d-b{step_days}d"
    if args.window_days is not None:
        default += f"-w{args.window_days}d"
    if args.fused:
        default += "-fused"
    if args.static_net:
        default += "-static-net"
    if args.certified:
        default += "-certified"
    if args.smoke_only:
        default += "-smoke"
    output = (args.output or ROOT / ".build" / default).resolve()
    if not args.check_only:
        output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        protocol = output / "protocol.json"
        if args.check_only:
            plan = load_json(protocol)
            end, runs = plan["end_utc"], plan["timing_replays"]
            methods = tuple(plan["methods"])
            expected_methods = comparison_methods("full-fused" in methods, "static-net" in methods,
                                                  "full-certified" in methods)
            if methods != expected_methods:
                raise ValueError("unrecognized temporal method list in the recorded protocol")
            prepared = prepare(output, None, end, plan)
        else:
            methods = comparison_methods(args.fused, args.static_net, args.certified)
            if args.dataset == "stackoverflow":
                raw = (args.raw or STACK_OVERFLOW).resolve()
                if not raw.is_file():
                    raise ValueError(f"Stack Overflow data required at {raw}. SNAP: {STACK_OVERFLOW_URL}")
                raw_hash = digest(raw)
            else:
                raw = ARCHIVE / "events.txt.gz"
                registered = (ARCHIVE / "SHA256SUMS").read_text().splitlines()[0].split()
                raw_hash = digest(raw)
                if registered != [raw_hash, raw.name]:
                    raise ValueError(f"raw Ask Ubuntu checksum mismatch: {raw}")
            identity, hardware = machine()
            allowed = identity.pop("affinity")
            cpu = allowed[0] if args.cpu is None else args.cpu
            if cpu not in allowed:
                parser.error(f"CPU {cpu} is outside allowed affinity {allowed}")
            os.sched_setaffinity(0, {cpu})
            env = os.environ | {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
            plan = {
                "dataset": dataset, "dataset_key": args.dataset, "raw": str(raw), "raw_sha256": raw_hash,
                "start_utc": start, "end_utc": end, "step_seconds": step,
                "projection_end_utc": projection_end,
                "window_days": list(windows), "checkpoint_interval": f"(t-{step_days}d,t]",
                "edge_window_interval": "(t-w,t]", "timing_replays": runs,
                "machine": identity, "cpu": cpu, "threads": 1, "methods": list(methods),
                "test_mode": args.smoke_only, "timer_scope": SCOPE,
                "runner_sha256": digest(Path(__file__)),
            }
            if args.dataset == "stackoverflow":
                plan["dataset_source"] = STACK_OVERFLOW_URL
            if windows != projection_windows:
                plan["projection_window_days"] = list(projection_windows)
            if protocol.exists() and load_json(protocol) != plan:
                raise ValueError("different interval, inputs, host, CPU or runner; choose a new --output")
            write_json(protocol, plan)
            (output / "hardware.txt").write_text(hardware)
            print(f"Run directory: {output} | {identity['host']} | {identity['cpu_model']} | CPU {cpu}", flush=True)
            prepared = prepare(output, raw, end, plan)
            if not args.prepare_only:
                binaries = build(output, args.star_root.resolve(strict=True), env)
        rows = []
        for days in plan["window_days"]:
            case, checkpoints, traces = experiment_case(
                prepared, days, end,
                dataset=plan.get("dataset_key", "askubuntu"), start=plan["start_utc"],
                step=plan["step_seconds"], methods=methods)
            if args.prepare_only:
                workload = workload_stats(case, checkpoints)
                rows.append(workload)
                print(f"INPUTS {case['dataset']}: {workload['batches']} batches, "
                      f"{workload['mean_active_edges']:,.0f} mean active edges, "
                      f"{workload['cancelled_operation_fraction']:.2%} cancelled operations", flush=True)
                continue
            directory = output / case["dataset"]
            if args.check_only:
                audit_paired_case(directory, case, methods, runs, expected_traces=traces)
                print(f"VERIFIED {case['dataset']}: {len(checkpoints)} batches", flush=True)
            else:
                run_paired_case(directory, case, methods, runs, binaries, env, expected_traces=traces)
            rows.extend(summarize(directory, case, checkpoints, runs, methods=methods))
        if args.prepare_only:
            write_json(output / "workloads.json", rows)
            print(f"REPORT {output / 'workloads.json'}", flush=True)
            return
        write_summary(output, rows)
    print(f"REPORT {output / 'summary.csv'}", flush=True)
    print(f"Temporal windows complete: {output}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
