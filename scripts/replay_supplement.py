#!/usr/bin/env python3
"""Run short Star checks or compare batch sizes on a shared update prefix."""

import argparse
import csv
import itertools
from pathlib import Path
import subprocess
import sys
import tempfile

from compare_replay import csv_rows

ROOT = Path(__file__).resolve().parents[1]
INPUTS = ROOT / ".build" / "vldb2027-inputs"
DATASETS = ("amazon", "wikivote")
BATCH_SIZES = (10, 100, 1000)


def read_prefix(stream, batches):
    operations = []
    with stream.open() as source:
        for i in range(1, batches + 1):
            if source.readline().split() != ["B", "1000"]:
                raise ValueError(f"{stream}: expected a B 1000 header at batch {i}")
            rows = list(itertools.islice(source, 1000))
            if len(rows) != 1000 or any(not row.startswith(("I ", "D ")) for row in rows):
                raise ValueError(f"{stream}: invalid operations in batch {i}")
            operations.extend(rows)
    return operations


def write_stream(path, operations, batch_size):
    with path.open("w") as output:
        for start in range(0, len(operations), batch_size):
            output.write(f"B {batch_size}\n")
            output.writelines(operations[start:start + batch_size])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("experiment", choices=("star", "batch-size"))
    args = parser.parse_args()

    subprocess.run(["sha256sum", "-c", "--quiet", "inputs.sha256"], cwd=INPUTS, check=True)
    run = Path(tempfile.mkdtemp(prefix=f"vldb2027-{args.experiment}-", dir=INPUTS.parent))
    print(f"Run directory: {run}", flush=True)
    (run / "source-inputs.sha256").write_bytes((INPUTS / "inputs.sha256").read_bytes())

    summary = []
    for dataset in DATASETS:
        graph = INPUTS / f"{dataset}.graph.txt"
        original = INPUTS / f"{dataset}_new_b1000_p0.5.txt"
        if args.experiment == "star":
            stream = run / f"{dataset}-first5.stream"
            write_stream(stream, read_prefix(original, 5), 1000)
            subprocess.run(
                [sys.executable, "-B", "scripts/compare_star.py",
                 "--star-root", ROOT / ".build" / "TrussMaintenance",
                 "--graph", graph, "--stream", stream, "--runs", "1",
                 "--output", run / dataset],
                cwd=ROOT, check=True,
            )
            continue

        operations = read_prefix(original, 20)
        for batch_size in BATCH_SIZES:
            name = f"{dataset}-b{batch_size}"
            stream = run / f"{name}.stream"
            output = run / name
            write_stream(stream, operations, batch_size)
            subprocess.run(
                [sys.executable, "-B", "scripts/compare_replay.py",
                 "--graph", graph, "--stream", stream, "--dataset", dataset,
                 "--threads", "1", "--runs", "3", "--output", output,
                 "--methods", "full", "msp"],
                cwd=ROOT, check=True,
            )
            for row in csv_rows(output / "summary.csv"):
                mean_ms = float(row["mean_ms"])
                throughput = batch_size * 1000 / mean_ms
                summary.append({
                    "dataset": dataset, "batch_size": batch_size, "method": row["method"],
                    "batches": row["batches"], "mean_ms": row["mean_ms"], "p99_ms": row["p99_ms"],
                    "ops_per_s": f"{throughput:.2f}",
                })

    if summary:
        with (run / "batch-size-summary.csv").open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=list(summary[0]))
            writer.writeheader()
            writer.writerows(summary)
    print(f"Completed {args.experiment}: {run}", flush=True)


if __name__ == "__main__":
    main()
