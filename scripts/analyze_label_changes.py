#!/usr/bin/env python3
"""Verify surviving label changes and pair counts with existing timing archives."""

import argparse
from collections import defaultdict
from pathlib import Path
import subprocess
from statistics import correlation, mean, median

from compare_replay import csv_rows, digest
from experiment_replays import audit_paired_case
from revision_experiments import ROOT, load_json
from revision_report import LABELS, audit, write_csv
from run_locality_experiments import METHODS, check_inputs, profile_rows


DATASETS = ("email", "wikivote", "amazon", "email-temporal", "askubuntu")
COUNTS = (
    "batch", "raw_ops", "previous_m", "final_m", "net_deleted", "net_inserted",
    "region_edges", "region_surviving", "changed", "promoted", "demoted",
    "changed_two_or_more", "degree_mass", "region_triangles", "verify_ok",
)
ARCHIVE_KEY = ("batch", "raw_ops", "final_m", "deleted", "inserted", "region_edges")
COUNTS_KEY = ("batch", "raw_ops", "final_m", "net_deleted", "net_inserted", "region_edges")


def workload_map(archive):
    status, valid = audit(archive, ("main",))
    failures = [row["configuration"] for row in status if row["status"] != "VERIFIED"]
    if failures:
        raise ValueError(f"main replay audit failed for {', '.join(failures)}")
    return {dataset: (directory, spec) for _, dataset, directory, spec in valid}


def collect(destination, workloads):
    executable = destination / "measure-label-changes"
    subprocess.run(["g++", "-std=c++17", "-O3", "-march=native", "-pthread",
                    f"-I{ROOT / 'src'}", str(ROOT / "scripts/measure_label_changes.cpp"),
                    "-o", str(executable)], check=True)
    for dataset in DATASETS:
        _, spec = workloads[dataset]
        check_inputs_unchanged(spec)
        output = destination / f"{dataset}.csv"
        temporary = output.with_suffix(".tmp")
        print(f"Verifying label changes on {dataset}", flush=True)
        with temporary.open("w") as file:
            subprocess.run([executable, spec["graph"], spec["stream"]],
                           stdout=file, check=True)
        temporary.replace(output)


def check_inputs_unchanged(spec):
    if [digest(Path(spec[field])) for field in ("graph", "stream")] != spec["inputs"]:
        raise ValueError("graph or stream differs from the audited main replay")


def key(row, fields):
    return tuple(int(row[field]) for field in fields)


def archived_profiles(profile_root, dataset, spec, restored):
    if dataset not in ("email", "amazon"):
        return None
    directory = profile_root / f"{dataset}-observed"
    case = load_json(directory / "complete.json")["case"]
    check_inputs(case)
    if [case[field] for field in ("graph_sha256", "stream_sha256")] != spec["inputs"]:
        raise ValueError(f"{directory}: profile input differs from the main replay")
    verified = audit_paired_case(directory, case, METHODS, 0)
    profile_rows(directory, case, "full", verified["full"])
    batch_rows = csv_rows(directory / "full.profile.batches.csv")
    phase_path = directory / "full.profile.phases.csv"
    phases = csv_rows(phase_path)
    if [key(row, ARCHIVE_KEY) for row in batch_rows] != [key(row, COUNTS_KEY) for row in restored]:
        raise ValueError(f"{directory}: profile batches differ from the retrospective replay")
    return phase_path, phases


def pair(dataset, location, archive, spec, profile_root):
    check_inputs_unchanged(spec)
    raw_path = location / f"{dataset}.csv"
    restored = [{field: int(row[field]) for field in COUNTS} for row in csv_rows(raw_path)]
    archived = csv_rows(archive / "full.verify.batches.csv")
    if [key(row, COUNTS_KEY) for row in restored] != [key(row, ARCHIVE_KEY) for row in archived]:
        raise ValueError(f"{raw_path}: batches differ from the audited main replay")
    if not restored or any(row["verify_ok"] != 1 for row in restored):
        raise ValueError(f"{raw_path}: expected a complete independently verified replay")

    profiles = archived_profiles(profile_root, dataset, spec, restored)
    indices = {row["batch"]: row for row in restored}
    samples = defaultdict(list)
    for row in csv_rows(archive / "full.timing.batches.csv"):
        batch = int(row["batch"])
        if key(row, ARCHIVE_KEY) != key(indices[batch], COUNTS_KEY):
            raise ValueError(f"{archive}: recorded timing checkpoint differs at batch {batch}")
        samples[batch].append(float(row["elapsed_us"]))

    result = []
    previous_m = restored[0]["previous_m"]
    for index, row in enumerate(restored):
        if (previous_m != row["previous_m"] or
                previous_m - row["net_deleted"] + row["net_inserted"] != row["final_m"] or
                row["region_edges"] - row["net_inserted"] != row["region_surviving"] or
                row["changed"] != row["promoted"] + row["demoted"] or
                not 0 <= row["changed_two_or_more"] <= row["changed"] <= row["region_surviving"] or
                len(samples[row["batch"]]) != spec["runs"]):
            raise ValueError(f"{raw_path}: inconsistent counts or timing replay count at batch {row['batch']}")
        previous_m = row["final_m"]
        phase_path, phase = (profiles[0], profiles[1][index]) if profiles else ("", {})
        profiled_us = (sum(float(phase[field]) for field in ("closure_us", "recount_us", "peel_us"))
                       if phase else "")
        result.append(dict(dataset=dataset, **row,
                           full_timing_median_us=median(samples[row["batch"]]),
                           profiled_region_us=profiled_us, label_counts_source=str(raw_path),
                           timing_source=str(archive / "full.timing.batches.csv"),
                           profile_source=str(phase_path)))
    return result


def summarize(dataset, rows):
    total_changed = sum(row["changed"] for row in rows)
    total_region = sum(row["region_surviving"] for row in rows)
    profiled = [row for row in rows if row["profile_source"]]
    return dict(dataset=dataset, batches=len(rows),
                mean_region_surviving=mean(row["region_surviving"] for row in rows),
                mean_promoted=mean(row["promoted"] for row in rows),
                mean_demoted=mean(row["demoted"] for row in rows),
                pooled_changed_percent=100 * total_changed / total_region if total_region else None,
                changed_two_or_more=sum(row["changed_two_or_more"] for row in rows),
                region_size_profile_corr=correlation(
                    [row["region_edges"] for row in profiled],
                    [row["profiled_region_us"] for row in profiled]) if profiled else "",
                degree_mass_profile_corr=correlation(
                    [row["degree_mass"] for row in profiled],
                    [row["profiled_region_us"] for row in profiled]) if profiled else "")


def table(rows, target):
    lines = [
        r"% Generated by scripts/analyze_label_changes.py",
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Surviving closure edges and label changes on the generated and",
        r"edge-count streams. Counts, promotions, and demotions are means per",
        r"batch. The final column divides changed labels by surviving closure",
        r"edges, pooled across batches.}",
        r"\label{tab:surviving-label-changes}",
        r"\small",
        r"\setlength{\tabcolsep}{2pt}",
        r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}lrrrr@{}}",
        r"\toprule",
        r"Workload & Closure & Promotions & Demotions & Changed \\",
        r"\midrule",
    ]
    for row in rows:
        workload = LABELS[row["dataset"]]
        if row["dataset"] == "email-temporal":
            lines.append(r"\midrule")
        lines.append(
            f"{workload} & {row['mean_region_surviving']:,.0f} & {row['mean_promoted']:,.1f} & "
            f"{row['mean_demoted']:,.1f} & {row['pooled_changed_percent']:.2f}\\% " + r"\\"
        )
    lines.extend((r"\bottomrule", r"\end{tabular*}", r"\end{table}"))
    target.write_text("\n".join(lines) + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / ".build/vldb2027-revision-phase1")
    parser.add_argument("--profiles", type=Path, default=ROOT / ".build/vldb2027-locality")
    parser.add_argument("--counts", type=Path, default=ROOT / ".build/vldb2027-label-changes")
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    parser.add_argument("--collect", action="store_true", help="rerun all deterministic verifications")
    args = parser.parse_args()
    try:
        workloads = workload_map(args.run.resolve(strict=True))
        if args.collect:
            args.counts.mkdir(parents=True, exist_ok=True)
            collect(args.counts.resolve(), workloads)
        all_rows, all_summaries = [], []
        for dataset in DATASETS:
            directory, spec = workloads[dataset]
            pairs = pair(dataset, args.counts.resolve(), directory, spec, args.profiles.resolve())
            all_rows.extend(pairs)
            all_summaries.append(summarize(dataset, pairs))
        destination = args.paper / "tables"
        destination.mkdir(parents=True, exist_ok=True)
        write_csv(destination / "label_changes_batches.csv", all_rows)
        write_csv(destination / "label_changes_summary.csv", all_summaries)
        table(all_summaries, destination / "tab_label_changes.tex")
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
