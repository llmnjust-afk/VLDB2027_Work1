#!/usr/bin/env python3
"""Recompute online adaptive results from archived replay traces."""

import argparse
from datetime import date
from math import isclose
from pathlib import Path
from statistics import mean

from compare_replay import csv_rows, digest
from revision_experiments import load_json
from run_adaptive_online import (
    CADENCES, METHODS, ONLINE, PERIODS, PROBE_CANDIDATES, PRUNED_METHODS, PRUNED_ONLINE,
    PRUNED_SELECTION, RAW_SHA256, SCOPE, SELECTION, audit_tag, calibration_score,
    checkpoint_rows, summarize_case,
)


def audit_reports(results, reports):
    names = ("summary.csv", "timing_replays.csv", "route_observations.csv")
    for name, reproduced in zip(names, reports, strict=True):
        for number, (recorded, computed) in enumerate(
                zip(csv_rows(results / name), reproduced, strict=True), 1):
            if recorded.keys() != computed.keys():
                raise ValueError(f"{name}: unexpected columns at row {number}")
            for key, value in computed.items():
                previous = recorded[key]
                if isinstance(value, float):
                    matches = isclose(float(previous), value, rel_tol=1e-10, abs_tol=1e-10)
                else:
                    matches = previous == str(value)
                if not matches:
                    raise ValueError(f"{name}: {key} differs at row {number}")
        print(f"AUDIT OK {name}: {len(reproduced)} rows reproduced")


def audit_case(results, prepared, start, days, role, cadence, interval, runs, methods, online):
    workload = f"stackoverflow-2014-{role}-b{cadence}d"
    dataset = f"{workload}-p{interval}" if role == "calibration" else workload
    directory = results / dataset
    completion = load_json(directory / "complete.json")
    case = completion["case"]
    start_utc = (date.fromisoformat(start) - date(1970, 1, 1)).days * 86400
    expected = (dataset, role, interval, cadence * 86400, start_utc, start_utc + days * 86400,
                7, RAW_SHA256, days // cadence)
    observed = (case["dataset"], case["role"], case["probe_interval"], case["checkpoint_seconds"],
                case["start_utc"], case["end_utc"], case["window_days"], case["raw_sha256"],
                len(case["batch_operations"]))
    if (observed != expected or completion["timing_replays"] != runs
            or completion["methods"] != list(methods)):
        raise ValueError(f"{dataset}: completion record disagrees with the protocol")

    inputs = prepared / workload / "inputs"
    manifest_path = inputs / "manifest.json"
    checkpoint_file = inputs / "w7d/checkpoints.csv"
    manifest = load_json(manifest_path)
    if (digest(manifest_path) != case["input_manifest_sha256"]
            or digest(checkpoint_file) != case["checkpoints_sha256"]
            or manifest["sha256"]["w7d/checkpoints.csv"] != case["checkpoints_sha256"]):
        raise ValueError(f"{dataset}: checkpoint manifest or content differs")
    checkpoints = checkpoint_rows(case | {"checkpoints": str(checkpoint_file)})
    verified = audit_tag(directory, case, "verify", checkpoints, methods)
    observations = {method: [] for method in methods}
    for repetition in range(runs):
        timed = audit_tag(directory, case, f"timing.{repetition}", checkpoints, methods)
        for method, record in timed.items():
            if method not in online and any(record[key] != verified[method][key]
                                            for key in ("trace", "regions")):
                raise ValueError(f"{dataset}: {method} timing trace differs from verification")
            observations[method].append(record)
    return case, observations


def audit_archive(results, prepared):
    plan = load_json(results / "protocol.json")
    pruned = plan["version"] == 2
    methods = PRUNED_METHODS if pruned else METHODS
    online = PRUNED_ONLINE if pruned else ONLINE
    regional = "full-fused-pruned" if pruned else "full-fused"
    selection_rule = PRUNED_SELECTION if pruned else SELECTION
    regional_by_method = dict.fromkeys(online, regional) if pruned else None
    if (plan["version"] not in (1, 2) or plan["smoke_only"] or plan["threads"] != 1
            or plan["periods"] != [list(period) for period in PERIODS]
            or plan["cadences"] != list(CADENCES) or plan["probe_candidates"] != list(PROBE_CANDIDATES)
            or plan["methods"] != list(methods) or plan["selection_rule"] != selection_rule
            or plan["timing_scope"] != SCOPE):
        raise ValueError("protocol does not describe the formal online experiments")
    runs = plan["timing_replays"]
    if runs < 1:
        raise ValueError("expected at least one timed replay")
    reports = ([], [], [])

    def collect(role, start, days, cadence, interval):
        case, observations = audit_case(results, prepared, start, days, role, cadence, interval, runs,
                                        methods, online)
        for target, rows in zip(reports, summarize_case(case, observations, regional_by_method), strict=True):
            target.extend(rows)
        return observations

    role, start, days = PERIODS[0]
    scores = {str(interval): mean(
        calibration_score(collect(role, start, days, cadence, interval), online, regional) for cadence in CADENCES)
              for interval in PROBE_CANDIDATES}
    selected = min(PROBE_CANDIDATES, key=lambda interval: (scores[str(interval)], interval))
    calibration = load_json(results / "calibration.json")
    if (calibration["probe_candidates"] != list(PROBE_CANDIDATES)
            or calibration["selected_probe_interval"] != selected or calibration["selection_rule"] != selection_rule
            or calibration["scores"].keys() != scores.keys()
            or any(not isclose(score, calibration["scores"][interval], rel_tol=1e-10, abs_tol=1e-10)
                   for interval, score in scores.items())):
        raise ValueError("archived calibration differs from the replay traces")
    print(f"AUDIT OK calibration: {selected} checkpoints")
    role, start, days = PERIODS[1]
    for cadence in CADENCES:
        collect(role, start, days, cadence, selected)
    audit_reports(results, reports)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, required=True, help="directory containing the online evidence")
    parser.add_argument("--prepared-root", type=Path, help="frozen checkpoint metadata (default: results/prepared)")
    args = parser.parse_args()
    audit_archive(args.results.resolve(), (args.prepared_root or args.results / "prepared").resolve())


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SystemExit(f"ERROR: {error}") from error
