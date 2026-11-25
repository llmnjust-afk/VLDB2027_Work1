#!/usr/bin/env python3
"""Replay causal routing policies on audited, chronologically separated latency traces."""

import argparse
from collections import deque
import csv
from datetime import datetime, timezone
from math import ceil, isclose, isfinite
from pathlib import Path
from statistics import mean

from compare_replay import csv_rows, digest
from experiment_replays import audit_paired_case
from revision_experiments import BUILD, load_json, write_json
from run_adaptive_comparison import holdout_cases, run_case
from run_temporal_windows import experiment_case, prepare


EARLY = BUILD / "vldb2027-temporal-stackoverflow-fused"
LATE = BUILD / "vldb2027-adaptive"
DEFAULT_OUTPUT = BUILD / "vldb2027-main3-adaptive-history"
FUSED = "full-fused"
STATIC = "static"
ADAPTIVE = "adaptive"
METHODS = (FUSED, STATIC)
THRESHOLDS = (20, 35, 50, 65, 80)
INTERVALS = (1, 2, 4, 8, 16)
REGISTERED = (50, 8)
PROBE_INTERVAL = 8
RECENT_SAMPLES = 3


def traces(directory, runs, methods):
    verification = csv_rows(directory / f"{FUSED}.verify.batches.csv")
    coverage = []
    for row in verification:
        region, edges = int(row["region_edges"]), int(row["final_m"])
        if not 0 <= region <= edges:
            raise ValueError(f"{directory}: invalid closure size")
        coverage.append((region, edges or 1))
    observations = []
    for trial in range(runs):
        observed = {}
        for method in methods:
            path = directory / f"{method}.timing.{trial}.batches.csv"
            observed[method] = [float(row["elapsed_us"]) / 1000 for row in csv_rows(path)]
            if len(observed[method]) != len(coverage) or not all(
                    isfinite(value) and value > 0 for value in observed[method]):
                raise ValueError(f"{path}: incomplete or invalid latencies")
        observations.append(observed)
    return coverage, observations


def inputs():
    early = load_json(EARLY / "protocol.json")
    later = load_json(LATE / "protocol.json")
    start, end, step = (early[key] for key in ("start_utc", "end_utc", "step_seconds"))
    if early["dataset_key"] != "stackoverflow" or early["window_days"] != [7]:
        raise ValueError("the calibration trace must be the early Stack Overflow replay")
    prepared = prepare(EARLY, None, end, early)
    case, _, expected = experiment_case(
        prepared, 7, end, dataset="stackoverflow", start=start, step=step, methods=early["methods"]
    )
    directory = EARLY / case["dataset"]
    audit_paired_case(directory, case, tuple(early["methods"]), early["timing_replays"],
                      expected_traces=expected)
    periods = {"calibration_daily": (directory, start, end, step, early["timing_replays"], METHODS)}
    binary = LATE / "build/batchtruss"
    if digest(binary) != later["binary_sha256"]:
        raise ValueError("the later replay executable differs from its manifest")
    for cadence, case in zip((1, 14), holdout_cases(), strict=True):
        recorded = next(item for item in later["cases"] if item["dataset"] == case["dataset"])
        if recorded != case:
            raise ValueError(f"{case['dataset']}: inconsistent workload registration")
        manifest = load_json(Path(case["checkpoints"]).parent.parent / "manifest.json")
        plan = manifest["protocol"]
        if (plan["raw_sha256"] != early["raw_sha256"] or plan["window_days"] != early["window_days"]
                or plan["start_utc"] != end or plan["step_seconds"] < step):
            raise ValueError("calibration must end before the later updates begin")
        run_case(LATE, case, later["timing_replays"], binary, {}, audit_only=True)
        periods[f"later_{cadence}d"] = (
            LATE / case["dataset"], plan["start_utc"], plan["end_utc"], plan["step_seconds"],
            later["timing_replays"], (*METHODS, ADAPTIVE),
        )
    return periods


def threshold_routes(coverage, threshold, interval):
    remaining = 0
    routes = []
    for region, edges in coverage:
        if remaining:
            routes.append(STATIC)
            remaining -= 1
        else:
            routes.append(FUSED)
            # Coverage is revealed by a completed Fused execution.
            if 100 * region > threshold * edges:
                remaining = interval
    return routes


def latency_routes(observed, n):
    history = {method: deque(maxlen=RECENT_SAMPLES) for method in METHODS}
    routes = []
    for batch in range(n):
        if batch < len(METHODS):
            selected = METHODS[batch]
        else:
            preferred = min(METHODS, key=lambda method: mean(history[method]))
            selected = next(method for method in METHODS if method != preferred) if (
                batch % PROBE_INTERVAL == 0
            ) else preferred
        routes.append(selected)
        history[selected].append(observed[selected][batch])
    return routes


def summarize(period, regime, trials, schedule, *, threshold="", interval="", recorded=False):
    costs = []
    probe_increases = []
    counts = []
    for trial, routes in zip(trials, schedule, strict=True):
        chosen = [trial[ADAPTIVE if recorded else route][index] for index, route in enumerate(routes)]
        costs.extend(chosen)
        changes = sum(left != right for left, right in zip(routes, routes[1:]))
        probes = [index for index, route in enumerate(routes) if route == FUSED
                  and (not index or routes[index - 1] == STATIC)]
        probe_increases.append(sum(max(0, chosen[index] - trial[STATIC][index]) for index in probes))
        counts.append((routes.count(STATIC), changes, len(probes)))
    costs.sort()
    n = len(schedule[0])
    elapsed = sum(costs) / len(trials)
    best_fixed = min(mean(sum(trial[method]) for trial in trials) for method in METHODS)
    oracle = mean(sum(min(static, fused) for static, fused in zip(trial[STATIC], trial[FUSED]))
                  for trial in trials)
    static_count, switches, probes = [mean(column) for column in zip(*counts)]
    return dict(
        period=period, policy=regime, threshold_percent=threshold, static_interval=interval,
        measurements="recorded_replay" if recorded else "offline_trace",
        timing_replays=len(trials), checkpoints=n, mean_ms=mean(costs),
        pooled_p95_ms=costs[ceil(.95 * len(costs)) - 1],
        pooled_p99_ms=costs[ceil(.99 * len(costs)) - 1], mean_total_ms=elapsed,
        best_fixed_mean_total_ms=best_fixed, hindsight_mean_total_ms=oracle,
        regret_best_fixed_percent=100 * (elapsed / best_fixed - 1),
        regret_hindsight_percent=100 * (elapsed / oracle - 1),
        mean_static_checkpoints=static_count, mean_switches=switches, mean_fused_probes=probes,
        mean_positive_probe_excess_vs_static_ms=mean(probe_increases),
    )


def period_rows(period, spec):
    directory, _, _, _, runs, methods = spec
    coverage, trials = traces(directory, runs, methods)
    n = len(coverage)
    rows = []
    for threshold in THRESHOLDS:
        for interval in INTERVALS:
            routes = threshold_routes(coverage, threshold, interval)
            rows.append(summarize(period, "coverage_threshold", trials, [routes] * runs,
                                  threshold=threshold, interval=interval))
    for method in METHODS:
        rows.append(summarize(period, method + "_fixed", trials, [[method] * n] * runs))
    rows.append(summarize(period, "past_latency_probe", trials,
                          [latency_routes(trial, n) for trial in trials], interval=PROBE_INTERVAL))
    if ADAPTIVE in methods:
        route_names = {"static": STATIC, "fused": FUSED}
        schedule = [[route_names[row["route"]] for row in
                     csv_rows(directory / f"{ADAPTIVE}.timing.{index}.routes.csv")]
                    for index in range(runs)]
        rows.append(summarize(period, "adaptive_measured", trials, schedule,
                              threshold=REGISTERED[0], interval=REGISTERED[1], recorded=True))
    return rows


def date_of(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    periods = inputs()
    rows = [row for period, spec in periods.items() for row in period_rows(period, spec)]
    grid = [row for row in rows
            if row["period"] == "calibration_daily" and row["policy"] == "coverage_threshold"]
    best = min(row["mean_total_ms"] for row in grid)
    ties = [row for row in grid if isclose(row["mean_total_ms"], best, abs_tol=1e-12, rel_tol=0)]
    chosen = min(ties, key=lambda row: (
        abs(row["threshold_percent"] - REGISTERED[0]), abs(row["static_interval"] - REGISTERED[1]),
    ))
    selected = chosen["threshold_percent"], chosen["static_interval"]
    for row in rows:
        matches = (row["threshold_percent"], row["static_interval"])
        row["selected_on_calibration"] = row["policy"] == "coverage_threshold" and matches == selected
    args.output.mkdir(parents=True, exist_ok=True)
    path = args.output / "sensitivity.csv"
    with path.open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    write_json(args.output / "protocol.json", dict(
        periods={name: dict(path=str(spec[0]), start_utc_exclusive=date_of(spec[1]),
                            end_utc_inclusive=date_of(spec[2]), checkpoint_days=spec[3] // 86400,
                            timing_replays=spec[4]) for name, spec in periods.items()},
        evaluation="Retrospective trace-driven routing. The later block was previously reported.",
        timing="Simulated switches select costs from separate fixed-method runs. "
               "Dispatch, cache/layout effects of switching, and other state-dependent costs are unmeasured. "
               "The recorded Adaptive row includes actual per-batch decision and update costs.",
        calibration="Minimize mean total latency on the earlier daily block only. "
                    "Ties favor threshold then interval closest to the recorded (50%, 8) rule, "
                    "then ascending grid order.",
        calibration_ties=len(ties), selected_threshold_percent=selected[0], selected_interval=selected[1],
        past_latency=f"Start with Fused then Static. Prefer the lower mean of the last "
                     f"{RECENT_SAMPLES} observed costs per method, and probe the alternative "
                     f"every {PROBE_INTERVAL} checkpoints. Restart at each period.",
        regret="Best fixed chooses a method per period. Hindsight selects the per-trial "
               "per-checkpoint minimum between fixed Static and Fused. Separate timing runs "
               "can put measured Adaptive below either trace reference.",
        probe_excess="Positive excess over fixed Static at the first Fused checkpoint or "
                     "when switching from Static to Fused. This is opportunity cost, excluding dispatch.",
        tails="Nearest-rank P95/P99 over pooled per-checkpoint costs within each period. "
              "Three timing repetitions are measurements of the same workload.",
    ))
    print(f"Selected {selected[0]}%/{selected[1]} on the earlier daily block from {len(ties)} tied settings")
    print(path)


if __name__ == "__main__":
    try:
        main()
    except (KeyError, OSError, StopIteration, ValueError) as error:
        raise SystemExit(f"ERROR: {error}") from error
