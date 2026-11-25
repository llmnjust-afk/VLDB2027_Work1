#!/usr/bin/env python3
"""Recompute the held-out Adaptive Online table from recorded batch latencies."""

import argparse
import csv
import json
from math import ceil, isclose, isfinite
from pathlib import Path
from statistics import mean, stdev


ROOT = Path(__file__).resolve().parents[1]
METHODS = {
    "static": "Static",
    "full-fused": "Fused",
    "full-fused-pruned": "Fused-Pruned",
    "adaptive": "Adaptive",
    "adaptive-recent": "Adaptive-Recent",
    "adaptive-cost": "Adaptive-Cost",
}
CHECKPOINTS = {1: 168, 14: 12}
REPLAYS = 3
SECONDS_PER_DAY = 86_400


def csv_rows(path):
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def recorded_latencies(results, dataset, method, operations):
    replays = []
    for phase in ("verify", *(f"timing.{i}" for i in range(REPLAYS))):
        path = results / dataset / f"{method}.{phase}.batches.csv"
        rows = csv_rows(path)
        expected = [(str(index), str(count)) for index, count in enumerate(operations, 1)]
        if [(row["batch"], row["raw_ops"]) for row in rows] != expected:
            raise ValueError(f"{path}: batch sequence differs from the completed replay")
        verification = "1" if phase == "verify" else ""
        if any(row["run"] != "0" or row["verify_ok"] != verification for row in rows):
            raise ValueError(f"{path}: unexpected run ID or verification status")
        if phase != "verify":
            samples = [float(row["elapsed_us"]) / 1000 for row in rows]
            if any(not isfinite(value) or value < 0 for value in samples):
                raise ValueError(f"{path}: invalid latency")
            replays.append(samples)

    means = [mean(samples) for samples in replays]
    pooled = sorted(sample for samples in replays for sample in samples)
    return mean(means), stdev(means), pooled[ceil(.95 * len(pooled)) - 1]


def load_table(results):
    plan = json.loads((results / "protocol.json").read_text())
    if (plan["timing_replays"] != REPLAYS or plan["threads"] != 1 or plan["smoke_only"]
            or plan["methods"] != list(METHODS) or plan["cadences"] != list(CHECKPOINTS)):
        raise ValueError("expected the formal Adaptive Online replay protocol")
    choice = json.loads((results / "calibration.json").read_text())
    candidates = choice["probe_candidates"]
    if candidates != plan["probe_candidates"] or choice["selection_rule"] != plan["selection_rule"]:
        raise ValueError("frozen probe interval differs from the experiment protocol")
    selected = choice["selected_probe_interval"]
    scores = choice["scores"]
    if selected != min(candidates, key=lambda value: (scores[str(value)], value)):
        raise ValueError("frozen probe interval did not win calibration")

    rows = [row for row in csv_rows(results / "summary.csv") if row["period"] == "test"]
    counts, values, stream = {}, {}, set()
    for cadence, count in CHECKPOINTS.items():
        dataset = f"stackoverflow-2014-test-b{cadence}d"
        completion = json.loads((results / dataset / "complete.json").read_text())
        case = completion["case"]
        operations = case["batch_operations"]
        if ((completion["methods"], completion["timing_replays"]) != (plan["methods"], REPLAYS)
                or (case["dataset"], case["role"], case["scope"]) != (dataset, "test", "temporal")
                or case["probe_interval"] != selected or case["window_days"] != 7
                or case["checkpoint_seconds"] != cadence * SECONDS_PER_DAY
                or len(operations) != count
                or case["end_utc"] - case["start_utc"] != count * cadence * SECONDS_PER_DAY):
            raise ValueError(f"{dataset}: completion marker differs from the experiment protocol")
        stream.add((case["start_utc"], case["end_utc"], case["graph_sha256"], sum(operations)))
        matching = [row for row in rows if row["dataset"] == dataset]
        summary = {row["method"]: row for row in matching}
        if len(matching) != len(METHODS) or summary.keys() != METHODS.keys():
            raise ValueError(f"{dataset}: expected one summary row for each method")

        counts[cadence] = count
        values[cadence] = {}
        for method in METHODS:
            row = summary[method]
            if ((row["batches"], row["verified_batches"], row["timing_replays"], row["status"]) !=
                    (str(count), str(count), str(REPLAYS), "verified")
                    or int(row["probe_interval"]) != selected
                    or int(row["operations"]) != sum(operations)):
                raise ValueError(f"{dataset}/{method}: summary row differs from the verified replay")
            raw = recorded_latencies(results, dataset, method, operations)
            stored = [float(row[key]) for key in
                      ("mean_ms_per_batch", "replay_mean_sd_ms", "pooled_p95_ms")]
            if not all(isclose(a, b, rel_tol=1e-12, abs_tol=1e-12) for a, b in zip(raw, stored)):
                raise ValueError(f"{dataset}/{method}: summary latencies differ from raw replays")
            values[cadence][method] = raw
    if len(stream) != 1:
        raise ValueError("test cadences should share an initial graph, interval and event count")
    return counts, values


def render(counts, values):
    def row(cells):
        return " & ".join(cells) + r" \\"

    best = {cadence: min(results, key=lambda method: results[method][0])
            for cadence, results in values.items()}
    lines = [
        "% Generated by scripts/generate_adaptive_online_paper.py",
        r"\centering",
        r"\caption{Held-out Stack Overflow latency in ms per batch. Mean",
        r"$\pm$ sample SD uses three replays. P95 pools their batch times.",
        r"Bold marks the lowest mean at each cadence.}",
        r"\label{tab:adaptive-online}",
        r"\footnotesize",
        r"\setlength{\tabcolsep}{2pt}",
        r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}lrrrr@{}}",
        r"\toprule",
        row(["", rf"\multicolumn{{2}}{{c}}{{Daily ({counts[1]} batches)}}",
             rf"\multicolumn{{2}}{{c}}{{14-day ({counts[14]} batches)}}"]),
        r"\cmidrule(lr){2-3}\cmidrule(l){4-5}",
        row(["Method", r"Mean $\pm$ SD", "P95", r"Mean $\pm$ SD", "P95"]),
        r"\midrule",
    ]
    for method, label in METHODS.items():
        cells = [label]
        for cadence in counts:
            avg, error, p95 = values[cadence][method]
            score = f"{avg:.2f}"
            if method == best[cadence]:
                score = rf"\mathbf{{{score}}}"
            cells.extend([rf"${score} \pm {error:.2f}$", f"{p95:.2f}"])
        lines.append(row(cells))
    lines.extend([r"\bottomrule", r"\end{tabular*}"])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=ROOT / ".build/vldb2027-main3-adaptive-online")
    parser.add_argument("--output", type=Path,
                        default=ROOT / "VLDB2027_Work1-main/paper/tables/tab_adaptive_online.tex")
    args = parser.parse_args()
    args.output.write_text(render(*load_table(args.results)))


if __name__ == "__main__":
    main()
