#!/usr/bin/env python3
"""Join closure/peel profiles with matched timings and audited label-change counts."""

import argparse
import fcntl
import math
from pathlib import Path
from statistics import mean, median
import subprocess

from analyze_label_changes import ARCHIVE_KEY, COUNTS_KEY, key, workload_map
from compare_replay import csv_rows, digest
from compare_star_resident import write_summary
from revision_experiments import BUILD, ROOT, load_json, write_json
from run_review_controls import FLAGS
from run_unified_dynamic import first_batches


METHODS = ("static", "star-resident", "luo-edge", "full-fused-pruned")
BASES = METHODS[:-1]
LABELS = {
    "askubuntu-7d": "AskU 7d", "askubuntu-365d": "AskU 365d", "amazon": "Amazon",
    "stackoverflow-28d": "SO 28d daily", "stackoverflow-b1d": "SO 112d daily",
    "stackoverflow-b14d": "SO 112d / 14d", "stackoverflow-90d-b14d": "SO 90d / 14d",
    "stackoverflow-90d-b112d": "SO 90d / 112d",
}
HELDOUT_LABELS = {
    "stackoverflow-2014-heldout-b1d": "SO 2014 / 1d",
    "stackoverflow-2014-heldout-b14d": "SO 2014 / 14d",
}
DEFAULT_ROOTS = (BUILD / "vldb2027-main3-unified", BUILD / "vldb2027-main3-large-churn")


def read_counts(path, batches):
    rows = csv_rows(path)
    if ([key(row, COUNTS_KEY) for row in rows] != [key(row, ARCHIVE_KEY) for row in batches]
            or any(row["verify_ok"] != "1" for row in rows)):
        raise ValueError(f"{path}: label diagnostics disagree with independent verification traces")
    return rows


def measure(binary, case, target, batches):
    fingerprints = {field: digest(Path(case[field])) for field in ("graph", "stream")}
    if any(fingerprints[field] != case[f"{field}_sha256"] for field in fingerprints):
        raise ValueError(f"{case['dataset']}: input digest disagrees with archived comparison")
    registration = target.with_suffix(".json")
    if registration.is_file():
        if load_json(registration) != fingerprints:
            raise ValueError(f"{target}: registered hashes differ")
        return read_counts(target, batches)
    temporary = target.with_suffix(".tmp")
    print(f"CHECK LABEL CHANGES {case['dataset']} ({len(batches)} checkpoints)", flush=True)
    with temporary.open("w") as file:
        subprocess.run([binary, case["graph"], case["stream"]], stdout=file, check=True)
    rows = read_counts(temporary, batches)
    temporary.replace(target)
    write_json(registration, fingerprints)
    return rows


def analyze_run(root, historical, output, binary):
    plan = load_json(root / "protocol.json")
    reported = {(row["dataset"], row["method"]): row for row in csv_rows(root / "summary.csv")}
    runs = plan["timing_replays"]
    result, block_rows = [], []
    for case in plan["cases"]:
        dataset = case["dataset"]
        directory = root / dataset
        if load_json(directory / "complete.json")["case"] != case:
            raise ValueError(f"{directory}: unexpected experiment completion record")
        batches = csv_rows(directory / "full-fused.verify.batches.csv")
        fused = csv_rows(directory / "full-fused.profile.scans.csv")
        pruned = csv_rows(directory / "full-fused-pruned.profile.scans.csv")
        if (len(batches) != len(case["batch_operations"])
                or any(any(row["batch"] != str(index) or row["region_edges"] != batch["region_edges"]
                           for row in (full, reduced)) or batch["verify_ok"] != "1"
                       or int(full["scanned_edges"]) != int(batch["region_edges"])
                       or int(reduced["certified_edges"]) + int(reduced["scanned_edges"]) != int(batch["region_edges"])
                       for index, (batch, full, reduced) in enumerate(zip(batches, fused, pruned, strict=True), 1))):
            raise ValueError(f"{directory}: incomplete or inconsistent profile")
        raw = {method: [csv_rows(directory / f"{method}.timing.{trial}.batches.csv")
                        for trial in range(runs)] for method in METHODS}
        elapsed = {}
        for method, trials in raw.items():
            elapsed[method] = []
            for trial in trials:
                if [(row["batch"], row["raw_ops"], row["final_m"], row["verify_ok"]) for row in trial] != [
                     (row["batch"], row["raw_ops"], row["final_m"], "") for row in batches]:
                    raise ValueError(f"{directory}: unpaired checkpoints for {method}")
                elapsed[method].append([float(row["elapsed_us"]) / 1000 for row in trial])
            summary = reported[dataset, method]
            actual = mean(mean(trial) for trial in elapsed[method])
            if summary["status"] != "verified" or not math.isclose(float(summary["mean_ms_per_batch"]), actual):
                raise ValueError(f"{directory}: timing disagrees with {method} summary")
        count_path = historical.get((case["graph_sha256"], case["stream_sha256"]))
        recorded = output / f"{dataset}.labels.csv"
        if count_path:
            counts = read_counts(count_path, batches)
        elif recorded.with_suffix(".json").is_file():
            hashes = {field: case[f"{field}_sha256"] for field in ("graph", "stream")}
            if load_json(recorded.with_suffix(".json")) != hashes:
                raise ValueError(f"{recorded}: diagnostic belongs to another input")
            count_path = recorded
            counts = read_counts(recorded, batches)
        elif binary:
            count_path = recorded
            counts = measure(binary, case, count_path, batches)
        else:
            counts = []
        n = len(batches)
        region = [int(row["region_edges"]) for row in batches]
        coverage = mean(size / max(1, int(row["final_m"])) for size, row in zip(region, batches))
        certified = sum(int(row["certified_edges"]) for row in pruned)
        alive_closure = sum(int(row["region_surviving"]) for row in counts)
        row = {"dataset": dataset, "checkpoint_seconds": case.get("checkpoint_seconds", ""),
               "checkpoints": n, "operations": sum(case["batch_operations"]),
               "closure_share": coverage, "mean_closure_edges": mean(region),
               "mean_endpoint_degree_sum": mean(int(row["scan_degree_sum"]) for row in fused),
               "certified_share": certified / sum(region) if sum(region) else 0,
               "changed_survivor_share": sum(int(row["changed"]) for row in counts) / alive_closure
               if alive_closure else "",
               "label_counts_source": str(count_path) if count_path else "",
               "work_and_timing_source": str(directory)}
        for method in METHODS:
            row[f"{method}_mean_ms"] = mean(mean(trial) for trial in elapsed[method])
        result.append(row)
        if case.get("checkpoint_seconds") == 86400 and n >= 28:
            for start in range(0, n - 13, 14):
                for method in BASES:
                    ratios = [sum(numerator[start:start + 14]) / sum(denominator[start:start + 14])
                              for numerator, denominator in zip(elapsed[method], elapsed[METHODS[-1]], strict=True)]
                    block_rows.append({"dataset": dataset, "first_batch": start + 1,
                                       "last_batch": start + 14, "baseline": method,
                                       "median_paired_ratio": median(ratios), "replays": runs})
    return result, block_rows


def write_plot(path, workloads):
    lines = [r"% Generated by scripts/generate_main4_diagnostics.py.",
             r"\begin{tikzpicture}[x=0.65cm,y=0.30cm,font=\scriptsize]",
             r"\draw[densely dashed,gray] (0,0.4)--(0,8.5);"]
    lines += [rf"\draw[gray!50] ({i},0.70)--({i},0.46) node[below] {{{label}}};"
              for i, label in ((-4, "1/16"), (-2, "1/4"), (0, "1"), (2, "4"), (4, "16"))]
    lines.append(r"\draw[gray!50] (-4.4,0.7)--(4.5,0.7);")
    markers = (("blue!65!black", r"circle (1.7pt)"),
               ("teal!67!black", r"rectangle +(3.3pt,3.3pt)"),
               ("orange!70!black", r"-- +(1.9pt,-3.4pt) -- +(-1.9pt,-3.4pt) -- cycle"))
    offsets = (0, -.22, .22)
    for position, (dataset, label) in enumerate(LABELS.items()):
        y = 8 - position
        r = workloads[dataset]
        lines.append(rf"\node[anchor=east,inner sep=1pt] at (-4.5,{y}) {{{label}}};")
        lines.append(rf"\draw[gray!16] (-4.4,{y})--(4.5,{y});")
        for name, (color, marker), offset in zip(BASES, markers, offsets, strict=True):
            effect = math.log2(r[f"{name}_mean_ms"] / r["full-fused-pruned_mean_ms"])
            adjust = "++(-1.65pt,-1.65pt)" if name == "star-resident" else ""
            lines.append(rf"\fill[{color}] ({effect:.4f},{y + offset}) {adjust} {marker};")
    lines.append(r"\node[below] at (0,-0.45) {Baseline time / Fused-Pruned time};")
    for x, name, (color, marker) in zip((-3.3, -.2, 2.4), ("Static", "Star", "Luo-Edge"), markers, strict=True):
        adjust = "++(-1.65pt,-1.65pt)" if name == "Star" else ""
        lines.append(rf"\fill[{color}] ({x},9.0) {adjust} {marker};"
                     rf"\node[anchor=west,inner sep=2pt] at ({x},9.0) {{{name}}};")
    lines.append(r"\end{tikzpicture}")
    path.write_text("\n".join(lines) + "\n")


def write_table(path, workloads):
    header = [r"% Generated by scripts/generate_main4_diagnostics.py.",
              r"\begin{table}[!tb]", r"\centering", r"\footnotesize",
              r"\caption{Closure and label work. Coverage, edges, and degree sum",
              r"are batch means. Certified is the pooled share of closure visits",
              r"assigned label 2 or 3 before the bucket sweep. Changed is the",
              r"pooled share of surviving closure edges with a different label.}",
              r"\label{tab:closure-work}", r"\setlength{\tabcolsep}{2pt}",
              r"\begin{tabular*}{\columnwidth}{@{}l@{\extracolsep{\fill}}rrrrr@{}}",
              r"\toprule",
              r"Workload & Cover & Edges & $\sum\deg$ (M) & Certified & Changed \\", r"\midrule"]
    rows = []
    for key, name in (LABELS | HELDOUT_LABELS).items():
        if key not in workloads:
            continue
        row = workloads[key]
        coverage = f"{100 * row['closure_share']:.2f}"
        certified = f"{100 * row['certified_share']:.2f}"
        changed = row["changed_survivor_share"]
        changed = rf"{100 * changed:.2f}\%" if changed != "" else "--"
        rows.append(rf"{name} & {coverage}\% & {row['mean_closure_edges']:,.0f} & "
                    rf"{row['mean_endpoint_degree_sum'] / 1e6:.2f} & {certified}\% & {changed} \\")
    tail = [r"\bottomrule", r"\end{tabular*}", r"\end{table}"]
    path.write_text("\n".join(header + rows + tail) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=BUILD / "vldb2027-main4-diagnostics")
    parser.add_argument("--extra-root", type=Path, action="append", default=[])
    parser.add_argument("--figure", type=Path, help="write vector TikZ for the original eight workloads")
    parser.add_argument("--table", type=Path, help="write the discovery-and-certification table")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--measure-missing", action="store_true", help="run label-only verification where missing")
    mode.add_argument("--smoke-only", action="store_true", help="test label-only verification on three real batches")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        generate(args, output)


def generate(args, output):
    binary = None
    if args.measure_missing or args.smoke_only:
        binary = output / "measure-label-changes"
        subprocess.run(["g++", *FLAGS, "-I", ROOT / "src", ROOT / "scripts/measure_label_changes.cpp",
                        "-o", binary], check=True)
    if args.smoke_only:
        source = load_json(DEFAULT_ROOTS[0] / "protocol.json")["cases"][0]
        case = first_batches(source, output)
        n = len(case["batch_operations"])
        batches = csv_rows(DEFAULT_ROOTS[0] / case["dataset"] / "full-fused.verify.batches.csv")[:n]
        measure(binary, case, output / "smoke.labels.csv", batches)
        print(f"AUDIT OK: {n} label-change checkpoints")
        return
    archive = workload_map(BUILD / "vldb2027-revision-phase1")
    historical = {tuple(spec["inputs"]): BUILD / f"vldb2027-label-changes/{dataset}.csv"
                  for dataset, (_, spec) in archive.items()}
    all_rows, all_blocks = [], []
    for source in (*DEFAULT_ROOTS, *args.extra_root):
        rows, blocks = analyze_run(source, historical, output, binary)
        all_rows.extend(rows)
        all_blocks.extend(blocks)
    write_summary(output, all_rows, filename="workload_diagnostics.csv")
    write_summary(output, all_blocks, filename="paired_blocks.csv")
    if args.figure:
        write_plot(args.figure, {row["dataset"]: row for row in all_rows})
    if args.table:
        write_table(args.table, {row["dataset"]: row for row in all_rows})
    print(f"AUDIT OK: {len(all_rows)} workloads and {len(all_blocks)} paired block comparisons in {output}")


if __name__ == "__main__":
    main()
