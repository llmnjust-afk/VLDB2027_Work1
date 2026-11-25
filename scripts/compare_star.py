#!/usr/bin/env python3
"""Replay the released SIGMOD Star binaries, with a separate correctness pass."""
import argparse
import csv
import heapq
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile
from time import perf_counter

from compare_replay import cpu_info, digest


def initial_graph(path):
    ids, edges = {}, set()
    with path.open() as file:
        for line in file:
            fields = line.split()
            if len(fields) < 2 or fields[0].startswith(("#", "%")):
                continue
            u, v = [ids.setdefault(int(value), len(ids)) for value in fields[:2]]
            if u != v:
                edges.add(tuple(sorted((u, v))))
    return len(ids), edges


def read_stream(path, n):
    batches = []
    with path.open() as file:
        for line in file:
            fields = line.split()
            if not fields or fields[0].startswith(("#", "%")):
                continue
            code, *values = fields
            if code.upper() == "B" and len(values) == 1:
                if batches and len(batches[-1]) != expected:
                    raise ValueError("batch length mismatch")
                expected = int(values[0])
                batches.append([])
            elif batches and code.upper() in {"I", "D"} and len(values) == 2:
                u, v = sorted(map(int, values))
                if not 0 <= u <= v < n:
                    raise ValueError("operation outside loaded vertices")
                batches[-1].append((code.upper(), u, v))
            else:
                raise ValueError(f"invalid stream line: {line}")
    if not batches or len(batches[-1]) != expected:
        raise ValueError("missing or truncated final batch")
    return batches


def write_stars(changes, star_file, centers):
    """Partition a monotone delta greedily into stars by residual degree."""
    adjacency = {}
    for u, v in changes:
        adjacency.setdefault(u, set()).add(v)
        adjacency.setdefault(v, set()).add(u)
    queue = [(-len(neighbors), u) for u, neighbors in adjacency.items()]
    heapq.heapify(queue)
    with star_file.open("w") as stars, centers.open("w") as queries:
        while queue:
            neg_degree, u = heapq.heappop(queue)
            neighbors = adjacency[u]
            if not neighbors or -neg_degree != len(neighbors):
                continue
            stars.write(f"n {u}\n")
            queries.write(f"{u} 0\n")
            for v in sorted(neighbors):
                stars.write(f"{u} {v}\n")
                adjacency[v].remove(u)
                heapq.heappush(queue, (-len(adjacency[v]), v))
            neighbors.clear()


def read_index(path):
    # The public release uses a 100-byte header followed by five native uint32s per edge.
    data = path.read_bytes()
    header = dict(row.split(",") for row in data[:100].split(b"\0", 1)[0].decode().splitlines())
    records = int(header["E"])
    if records < 0 or len(data) < 100 + 20 * records:
        raise ValueError("truncated Star index")
    labels = {}
    for u, v, label, _, _ in struct.iter_unpack("=IIIII", data[100:100 + 20 * records]):
        edge = tuple(sorted((u, v)))
        if u == v or label < 2 or edge in labels:
            raise ValueError(f"invalid Star edge/label {edge}: {label}")
        labels[edge] = label
    return labels


def run_case(args, executables, n, initial, batches, tag, repetitions):
    check = tag == "verify"
    out = args.output
    fields = ("run", "batch", "raw_ops", "deleted", "inserted", "adapter_ms",
              "kernel_ms", "elapsed_ms", "verify_ok", "final_m")
    with (out / f"star.{tag}.batches.csv").open("w") as file, \
         (out / f"star.{tag}.runs.csv").open("w") as run_file, \
         (out / f"star.{tag}.upstream.log").open("w") as log, \
         tempfile.TemporaryDirectory(prefix="truss-star-") as directory:
        raw = csv.writer(file)
        raw.writerow(fields)
        builds = csv.writer(run_file)
        builds.writerow(("run", "build_ms", "threads"))
        temp = Path(directory)
        graph_file, index = temp / "graph.txt", temp / "state.bin"
        star_file, centers = (temp / f"{name}.txt" for name in ("stars", "centers"))
        new_index = temp / "next.bin"
        reference_file, labels_file = temp / "reference.txt", temp / "reference.labels"

        def execute(name, *paths):
            result = subprocess.run([executables[name], *paths], cwd=temp, text=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            log.write(result.stdout)
            log.flush()
            result.check_returncode()
            return result.stdout

        for run in range(repetitions):
            shadow = set(initial)
            start = perf_counter()
            graph_file.write_text("".join(f"{u} {v}\n" for u, v in sorted(initial)))
            execute("index", graph_file, index)
            builds.writerow((run, (perf_counter() - start) * 1000, 1))

            for batch_id, ops in enumerate(batches, 1):
                start = perf_counter()
                before = {}
                for code, u, v in ops:
                    if u == v:
                        continue
                    edge = (u, v)
                    before.setdefault(edge, edge in shadow)
                    if code == "I":
                        shadow.add(edge)
                    else:
                        shadow.discard(edge)
                dels = [e for e, present in before.items() if present and e not in shadow]
                ins = [e for e, present in before.items() if not present and e in shadow]
                kernel = 0.0
                for action, changes in (("delete", dels), ("insert", ins)):
                    if not changes:
                        continue
                    write_stars(changes, star_file, centers)
                    output = execute(action, index, star_file, centers, new_index)
                    matches = re.findall(r"total query time: ([0-9.]+) ms", output)
                    if len(matches) != 1:
                        raise ValueError("missing upstream timing")
                    kernel += float(matches[0])
                    new_index.replace(index)
                elapsed = (perf_counter() - start) * 1000
                if check:
                    actual = read_index(index)
                    if actual.keys() != shadow:
                        raise ValueError(f"batch {batch_id}: missing {next(iter(shadow - actual.keys()), None)}, "
                                         f"extra {next(iter(actual.keys() - shadow), None)}; operations={ops}")
                    with reference_file.open("w") as reference:
                        for vertex in range(n):
                            reference.write(f"{vertex} {vertex}\n")
                        for u, v in shadow:
                            reference.write(f"{u} {v}\n")
                    subprocess.run([args.binary, "static", "--graph", reference_file,
                                    "--labels-out", labels_file], check=True, stdout=subprocess.DEVNULL)
                    with labels_file.open() as labels:
                        for line in labels:
                            u, v, label = map(int, line.split())
                            if actual[(u, v)] != label:
                                raise ValueError(f"batch {batch_id}: ({u},{v}) expected {label}, "
                                                 f"Star={actual[(u,v)]}; operations={ops}")
                raw.writerow((run, batch_id, len(ops), len(dels), len(ins), elapsed - kernel,
                              kernel, elapsed, 1 if check else "", len(shadow)))
                file.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--star-root", type=Path, required=True)
    parser.add_argument("--graph", type=Path, required=True)
    parser.add_argument("--stream", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--binary", type=Path, default=Path("batchtruss"))
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("runs must be positive")
    args.binary = args.binary.resolve(strict=True)
    args.output = args.output.resolve()
    args.output.mkdir(parents=True)
    code = args.star_root / "code"
    binaries = {label: (code / relative).resolve(strict=True)
                for label, relative in {"index": "index/index.out",
                                        "insert": "ballsIns/ballsIns.out", "delete": "ballDel/ballDel.out"}.items()}
    metadata = {"inputs": {str(p): digest(p) for p in (args.graph, args.stream, args.binary)},
                "binaries": {str(p): digest(p) for p in binaries.values()},
                "threads": 1, "runs": args.runs, "affinity": sorted(os.sched_getaffinity(0)),
                "timing": "wall including translation and index I/O; kernel from upstream timers",
                "batch_adapter": "ordered replay to net changes, greedy residual-degree stars, Star delete then Star insert"}
    (args.output / "manifest.json").write_text(json.dumps(metadata, indent=2) + "\n")
    (args.output / "hardware.txt").write_text(cpu_info())
    n, edges = initial_graph(args.graph)
    batches = read_stream(args.stream, n)
    run_case(args, binaries, n, edges, batches, "verify", 1)
    print(f"verified Star: {len(batches)} entire edge states and trussness arrays", flush=True)
    run_case(args, binaries, n, edges, batches, "timing", args.runs)
    (args.output / "complete").write_text("Star edge sets and labels verified before timing.\n")


if __name__ == "__main__":
    main()
