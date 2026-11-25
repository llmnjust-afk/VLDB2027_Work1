#!/usr/bin/env python3
"""Check or reproduce the 12-checkpoint Fused-Pruned pilot."""

import argparse
import itertools
import os
from pathlib import Path
import shutil
from statistics import mean, stdev
import subprocess

from compare_replay import csv_rows, digest
from compare_star_resident import run_logged, write_summary
from revision_experiments import BUILD, ROOT, load_json, write_json
from run_review_controls import FLAGS


METHODS = ("fused", "fused-pruned", "certified")
DATASETS = ("wikivote", "amazon", "stackoverflow-b1d")
PREFIX_BATCHES = 12
REPLAYS = 3
TRACE_FIELDS = ("raw_ops", "deleted", "inserted", "region_edges", "final_m")
SCAN_FIELDS = ("region_edges", "certified_edges", "scanned_edges", "scan_degree_sum")


def prefix_text(case):
    with Path(case["stream"]).open() as source:
        parts = []
        for count in case["batch_operations"][:PREFIX_BATCHES]:
            header = next(source)
            updates = list(itertools.islice(source, count))
            if header.split() != ["B", str(count)] or len(updates) != count:
                raise ValueError(f"{case['dataset']}: invalid prefix checkpoint")
            parts.extend((header, *updates))
        return "".join(parts)


def cases():
    plan = load_json(BUILD / "vldb2027-adaptive/protocol.json")
    selected = [case for case in plan["cases"] if case["dataset"] in DATASETS]
    if [case["dataset"] for case in selected] != list(DATASETS):
        raise ValueError("archived plan has unexpected pilot cases")
    return selected


def traces(path, counts, verify):
    rows = csv_rows(path)
    if len(rows) != len(counts):
        raise ValueError(f"{path}: wrong checkpoint count")
    keys = [(int(row["run"]), int(row["batch"]), int(row["raw_ops"])) for row in rows]
    if keys != [(0, index, size) for index, size in enumerate(counts, 1)]:
        raise ValueError(f"{path}: wrong checkpoint IDs or request counts")
    if expect := [row["verify_ok"] for row in rows if row["verify_ok"] != ("1" if verify else "")]:
        raise ValueError(f"{path}: unexpected verification flags {expect}")
    return rows, [tuple(row[key] for key in TRACE_FIELDS) for row in rows]


def summarize(output, plan):
    records, trial_rows = [], []
    binary = output / "build" / "batchtruss"
    if digest(binary) != plan["binary_sha256"]:
        raise ValueError("recorded binary digest differs")
    for case in plan["cases"]:
        dataset, counts = case["dataset"], case["batch_operations"][:PREFIX_BATCHES]
        folder = output / dataset
        for key in ("graph", "stream"):
            if digest(Path(case[key])) != case[f"{key}_sha256"]:
                raise ValueError(f"{dataset}: changed original {key}")
        if (folder / "prefix12.stream").read_text() != prefix_text(case):
            raise ValueError(f"{dataset}: prefix differs from original stream")
        matched = None
        for method in METHODS:
            rows, trace = traces(folder / f"{method}.verify.csv", counts, True)
            if matched is not None and trace != matched:
                raise ValueError(f"{dataset}: methods disagree on the closure or update trace")
            matched = trace
            scans = csv_rows(folder / f"{method}.scan.csv")
            if [(row["run"], row["batch"], row["region_edges"]) for row in scans] != [
                (row["run"], row["batch"], row["region_edges"]) for row in rows
            ] or any(int(row["certified_edges"]) + int(row["scanned_edges"]) != int(row["region_edges"])
                     for row in scans):
                raise ValueError(f"{dataset}/{method}: inconsistent scan counts")
            means = []
            if method != "certified":
                for trial in range(REPLAYS):
                    rows, timed = traces(folder / f"{method}.timing.{trial}.csv", counts, False)
                    if trace != timed:
                        raise ValueError(f"{dataset}/{method}: timed trace differs from verification")
                    latency = mean(float(row["elapsed_us"]) / 1000 for row in rows)
                    means.append(latency)
                    trial_rows.append(dict(dataset=dataset, method=method, replay=trial,
                                           batches=len(rows), mean_ms_per_batch=latency))
            profile, profile_trace = traces(folder / f"{method}.profile.batches.csv", counts, False)
            phases = csv_rows(folder / f"{method}.profile.phases.csv")
            if profile_trace != trace or [(r["run"], r["batch"], r["elapsed_us"]) for r in phases] != [
                (r["run"], r["batch"], r["elapsed_us"]) for r in profile
            ]:
                raise ValueError(f"{dataset}/{method}: profile timing does not match checkpoint trace")
            records.append(dict(dataset=dataset, method=method, batches=len(scans),
                                **{key: sum(int(row[key]) for row in scans) for key in SCAN_FIELDS},
                                timing_replays=len(means), mean_ms_per_batch=mean(means) if means else "",
                                trial_mean_sd_ms=stdev(means) if means else ""))
        print(f"AUDIT OK {dataset}: {PREFIX_BATCHES} complete checkpoints", flush=True)
    return records, trial_rows


def run_one(command, flag, destination, tag, extra=()):
    prefix = destination / f"{flag}.{tag}"
    batch_file = f"{prefix}.batches.csv" if tag == "profile" else f"{prefix}.csv"
    options = ["--ablate", flag, "--batch-csv", batch_file]
    options += ["--check-every", "1"] if tag == "verify" else ["--timing-only"]
    run_logged(command + options + list(extra), Path(f"{prefix}.runs.csv"),
               Path(f"{prefix}.stderr"), env=os.environ.copy())


def prepare(output, cpu, imported):
    if cpu not in os.sched_getaffinity(0):
        raise ValueError(f"CPU {cpu} is outside the allowed affinity")
    output.mkdir(parents=True)
    binary = output / "build" / "batchtruss"
    binary.parent.mkdir()
    compiler = subprocess.check_output(["g++", "--version"], text=True).splitlines()[0]
    command = ["g++", *FLAGS, "-Wall", "-Wextra", "-o", str(binary), str(ROOT / "src/main.cpp")]
    if imported:
        built = binary.with_suffix(".candidate")
        compile_command = command.copy()
        compile_command[compile_command.index(str(binary))] = str(built)
        subprocess.run(compile_command, check=True)
        if digest(built) != digest(imported / "batchtruss"):
            raise ValueError("current sources do not reproduce the original pilot binary")
        shutil.copyfile(imported / "batchtruss", binary)
        binary.chmod(built.stat().st_mode)
        built.unlink()
    else:
        subprocess.run(command, check=True)
    plan = dict(compiler=compiler, build_argv=command, binary_sha256=digest(binary),
                sources={str(path.relative_to(ROOT)): digest(path) for path in
                         [ROOT / "src/main.cpp", *sorted((ROOT / "src").glob("*.h"))]},
                cpu=cpu, threads=1, timed_replays=REPLAYS, prefix_batches=PREFIX_BATCHES,
                original_measurement_imported=bool(imported), cases=cases())
    if imported:
        shutil.copyfile(imported / "prefix12-summary.csv", output / "prefix12-summary.original.csv")
    write_json(output / "protocol.json", plan)
    for case in plan["cases"]:
        dataset = case["dataset"]
        folder = output / dataset
        folder.mkdir()
        prefix = folder / "prefix12.stream"
        prefix.write_text(prefix_text(case))
        if imported:
            original = imported / dataset
            if digest(prefix) != digest(original / prefix.name):
                raise ValueError(f"{dataset}: imported prefix does not match original stream")
            for method in METHODS:
                tags = ["verify", "scan"]
                if method != "certified":
                    tags.extend(f"timing.{i}" for i in range(REPLAYS))
                for tag in tags:
                    name = f"{method}.{tag}.csv"
                    shutil.copyfile(original / name, folder / name)
        argv = ["taskset", "-c", str(cpu), str(binary), "batch", "--graph", case["graph"],
                "--stream", str(prefix), "--threads", "1", "--dataset", dataset]
        if not imported:
            for method in METHODS:
                run_one(argv, method, folder, "verify", ("--peel-scan-csv", str(folder / f"{method}.scan.csv")))
            for index in range(REPLAYS):
                for method in METHODS[:2][::1 if index % 2 == 0 else -1]:
                    run_one(argv, method, folder, f"timing.{index}")
        for method in METHODS:
            run_one(argv, method, folder, "profile", ("--phase-csv", str(folder / f"{method}.profile.phases.csv")))
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cpu", type=int, default=15)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check-only", action="store_true")
    mode.add_argument("--import-existing", type=Path, help="recover an original exploratory prefix run")
    args = parser.parse_args()
    output = args.output.resolve()
    plan = load_json(output / "protocol.json") if args.check_only else prepare(output, args.cpu, args.import_existing)
    summary, trials = summarize(output, plan)
    if args.check_only:
        for filename, rows in (("summary.csv", summary), ("derived-replay-means.csv", trials)):
            expected = [{key: str(value) for key, value in row.items()} for row in rows]
            if csv_rows(output / filename) != expected:
                raise ValueError(f"{filename}: stored summary differs from raw CSVs")
    else:
        write_summary(output, summary)
        write_summary(output, trials, filename="derived-replay-means.csv")
        if args.import_existing and digest(output / "summary.csv") != digest(output / "prefix12-summary.original.csv"):
            raise ValueError("regenerated pilot results differ from the original summary")
    print(output / "summary.csv")


if __name__ == "__main__":
    main()
