#!/usr/bin/env python3
"""Exercise the executable and cross-check its labels with a tiny independent oracle."""
import argparse
import csv
import itertools
from pathlib import Path
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"


def read_batches(path):
    batches = []
    for line in path.read_text().splitlines():
        code, *args = line.split()
        if code == "B":
            batches.append([])
        else:
            batches[-1].append((code, *map(int, args)))
    return batches


def write_batches(path, batches):
    with path.open("w") as stream:
        for batch in batches:
            stream.write(f"B {len(batch)}\n")
            for code, u, v in batch:
                stream.write(f"{code} {u} {v}\n")


def truth(edges):
    labels = dict.fromkeys(edges, 2)
    vertices = {v for edge in edges for v in edge}
    for k in range(3, len(vertices) + 1):
        survivors = set(edges)
        while True:
            pruned = {edge for edge in survivors if sum(
                tuple(sorted((edge[0], w))) in survivors and
                tuple(sorted((edge[1], w))) in survivors
                for w in vertices - set(edge)
            ) >= k - 2}
            if pruned == survivors:
                break
            survivors = pruned
        if not survivors:
            break
        labels.update(dict.fromkeys(survivors, k))
    return labels


def check(binary, graph, stream, threads, mode, temp):
    stats, labels = temp / "batches.csv", temp / "labels.txt"
    run = subprocess.run(
        [binary, "batch", "--graph", graph, "--stream", stream, "--threads", str(threads),
         "--ablate", mode, "--check-every", "1", "--batch-csv", stats, "--labels-out", labels],
        check=True, text=True, capture_output=True,
    )
    with stats.open() as file:
        counts = list(csv.DictReader(file))
    assert counts and all(row["verify_ok"] == "1" for row in counts), run.stdout
    actual = {(u, v): tau for u, v, tau in
              (map(int, line.split()) for line in labels.read_text().splitlines())}
    assert all(int(row["virtual_events"]) == 0 for row in counts)
    return actual, counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, default=ROOT / "batchtruss")
    args = parser.parse_args()
    binary = args.binary.resolve()
    with tempfile.TemporaryDirectory() as directory:
        temp = Path(directory)
        stream = temp / "prefix.stream"
        cases = [
            ("triangle", "triangle-reinsert", [(0, 0, 0, 0)]),
            ("triangle", "triangle-reverse", [(1, 0, 1, 2)]),
            ("k4", "k4-mixed", [(0, 1, 1, 6), (1, 0, 1, 5), (1, 1, 1, 5),
                                (0, 0, 0, 0), (1, 0, 0, 0), (0, 0, 0, 0)]),
        ]
        for graph_name, stream_name, expected_counts in cases:
            graph = FIXTURES / f"{graph_name}.graph"
            batches = read_batches(FIXTURES / f"{stream_name}.stream")
            edges = {tuple(map(int, line.split())) for line in graph.read_text().splitlines()}
            for i, batch in enumerate(batches):
                for code, u, v in batch:
                    if u == v:
                        continue
                    edge = tuple(sorted((u, v)))
                    if code == "I":
                        edges.add(edge)
                    else:
                        edges.discard(edge)
                write_batches(stream, batches[:i + 1])
                expected = truth(edges)
                for threads, mode in itertools.product((1, 4), ("full", "nomerge")):
                    actual, counts = check(binary, graph, stream, threads, mode, temp)
                    assert actual == expected, (stream_name, i, threads, mode, expected, actual)
                    if mode == "full":
                        assert tuple(int(counts[-1][field]) for field in
                                     ("deleted", "inserted", "peels", "region_edges")
                                     ) == expected_counts[i], counts[-1]
            print(f"{stream_name}: {len(batches)} prefixes agree with threshold oracle at T=1,4")

        graph = temp / "parallel.graph"
        groups = 11_000
        with graph.open("w") as file:
            for u in range(0, groups * 4, 4):
                for v in range(u + 1, u + 4):
                    file.write(f"{u} {v}\n")
        insert = [("I", u + i, u + j) for u in range(0, groups * 4, 4)
                  for i, j in ((1, 2), (1, 3), (2, 3))]
        write_batches(stream, [insert])
        expected = {(u + i, u + j): 4 for u in range(0, groups * 4, 4)
                    for i, j in itertools.combinations(range(4), 2)}
        serial, serial_counts = check(binary, graph, stream, 1, "full", temp)
        parallel, parallel_counts = check(binary, graph, stream, 4, "full", temp)
        assert serial == parallel == expected
        assert int(serial_counts[0]["parallel_jobs"]) == 0
        assert int(parallel_counts[0]["parallel_jobs"]) >= 2
        assert int(parallel_counts[0]["region_edges"]) == 6 * groups
        print(f"parallel K4: {len(parallel)} labels agree at T=1,4, "
              f"T=4 launched {parallel_counts[0]['parallel_jobs']} worker jobs")

        stream.write_text("B 1\nI 0 1\nD 0 1\n")
        invalid = subprocess.run(
            [binary, "batch", "--graph", FIXTURES / "triangle.graph", "--stream", stream],
            text=True, capture_output=True,
        )
        assert invalid.returncode != 0 and "batch length" in invalid.stderr
        print("malformed batch length rejected")


if __name__ == "__main__":
    main()
