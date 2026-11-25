#!/usr/bin/env python3
"""Audit the archived Regional/Luo-Edge replays and rebuild paper Table 9."""

import argparse
import csv
import hashlib
import json
import math
import re
from functools import lru_cache
from pathlib import Path
from statistics import mean, stdev


WORKLOADS = (
    ("askubuntu-w7d", "Ask Ubuntu", 365, 221275, 3),
    ("stackoverflow-w7d", "Stack Overflow", 28, 777973, 2),
    ("amazon", "Amazon", 200, 200000, 2),
)
METHODS = ("full", "luo-edge")
SOURCE_FILES = {"full": "src/main.cpp", "luo-edge": "src/luo_edge_replay.cpp"}
DOI = "10.1109/TC.2022.3174594"
THREADS = (1, 4)
REPLAYS = 3
FLOAT_FIELDS = {
    "mean_ms_per_batch", "replay_mean_sd_ms", "pooled_p95_ms",
    "mean_maintenance_s", "mean_build_s", "latency_over_full",
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


@lru_cache(maxsize=None)
def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def close(actual, expected):
    return math.isclose(float(actual), float(expected), rel_tol=1e-12, abs_tol=1e-14)


def csv_rows(path):
    with path.open(newline="") as file:
        return list(csv.DictReader(file))


def load_json(path):
    return json.loads(path.read_text())


def edge_key(u, v):
    return (min(u, v) << 32) | max(u, v)


def stream_trace(graph, stream):
    """Rebuild net mutations and batch-end edge counts from ordered input."""
    edges = set()
    vertex_ids = {}
    with graph.open("rb") as file:
        for line in file:
            fields = line.split()
            if fields and not fields[0].startswith((b"#", b"%")):
                # The loader assigns IDs in first-appearance order, including
                # the self-loop sentinels used to register isolated vertices.
                u, v = map(int, fields[:2])
                u = vertex_ids.setdefault(u, len(vertex_ids))
                v = vertex_ids.setdefault(v, len(vertex_ids))
                if u != v:
                    edges.add(edge_key(u, v))
    counts, trace = [], []
    with stream.open("rb") as file:
        for line in file:
            header = line.split()
            require(len(header) == 2 and header[0] == b"B" and header[1].isdigit(),
                    f"{stream}: invalid header after batch {len(counts)}")
            size = int(header[1])
            original_presence = {}
            for _ in range(size):
                fields = file.readline().split()
                require(len(fields) == 3 and fields[0] in (b"I", b"D"),
                        f"{stream}: invalid event in batch {len(counts) + 1}")
                u, v = map(int, fields[1:])
                require(0 <= u < len(vertex_ids) and 0 <= v < len(vertex_ids),
                        f"{stream}: invalid vertex in batch {len(counts) + 1}")
                if u == v:
                    continue
                edge = edge_key(u, v)
                original_presence.setdefault(edge, edge in edges)
                if fields[0] == b"I":
                    edges.add(edge)
                else:
                    edges.discard(edge)
            deletes = sum(present and edge not in edges
                          for edge, present in original_presence.items())
            inserts = sum(not present and edge in edges
                          for edge, present in original_presence.items())
            counts.append(size)
            trace.append((deletes, inserts, len(edges)))
    return counts, trace


def check_argv(path, method, case, tag, threads, verification):
    argv = load_json(path)
    mode = ["batch", "--ablate", "full"] if method == "full" else []
    suffix = ["--check-every", "1"] if verification else ["--timing-only"]
    expected = mode + [
        "--graph", case["graph"], "--stream", case["stream"],
        "--dataset", case["dataset"], "--threads", str(threads), "--runs", "1",
        "--batch-csv",
    ]
    require(len(argv) == 2 + len(expected) + len(suffix) and
            argv[1:1 + len(expected)] == expected and argv[-len(suffix):] == suffix and
            Path(argv[0]).name == method and Path(argv[-1 - len(suffix)]).name == f"{tag}.batches.csv",
            f"{path}: replay arguments disagree with the recorded protocol")


def check_build(root, directory, plan):
    build = plan["build"]
    for name, sha in build["sources"].items():
        path = Path(name)
        require(not path.is_absolute() and ".." not in path.parts and digest(root / path) == sha,
                f"{directory.name}: source hash disagrees for {name}")
    for method, source in SOURCE_FILES.items():
        sha = plan["executable_sha256"][method]
        require(re.fullmatch(r"[0-9a-f]{64}", sha), f"{directory.name}: invalid executable hash")
        command = directory / "build" / f"{method}.argv.json"
        if command.is_file():
            argv = load_json(command)
            expected = ["g++", *build["flags"], source, "-o"]
            require(len(argv) == 1 + len(expected) and argv[:-1] == expected and
                    Path(argv[-1]).name == method,
                    f"{command}: build arguments disagree with the recorded protocol")


def audit_pass(directory, method, tag, case, counts, trace):
    base = directory / f"{method}.{tag}"
    verification = tag == "verify"
    verify_ok = "1" if verification else ""
    check_argv(Path(f"{base}.runs.argv.json"), method, case, f"{method}.{tag}",
               case["threads"], verification)
    rows = csv_rows(Path(f"{base}.batches.csv"))
    require(len(rows) == len(counts), f"{base}: incomplete batch trace")
    times, groups = [], []
    for i, (row, count, (deletes, inserts, final)) in enumerate(zip(rows, counts, trace), 1):
        expected = {
            "run": 0, "batch": i, "raw_ops": count, "deleted": deletes,
            "inserted": inserts, "final_m": final, "verify_ok": verify_ok,
        }
        require(all(row[key] == str(value) for key, value in expected.items()),
                f"{base}: verification or ordered mutation trace disagrees at batch {i}")
        elapsed = float(row["elapsed_us"])
        require(math.isfinite(elapsed) and elapsed >= 0, f"{base}: invalid batch time")
        times.append(elapsed / 1000)
        if method == "luo-edge":
            group = int(row["deletion_groups"]), int(row["insertion_groups"])
            require(all((0 < part <= events) if events else part == 0
                        for part, events in zip(group, (deletes, inserts))),
                    f"{base}: invalid group counts at batch {i}")
            groups.append(group)

    run, = csv_rows(Path(f"{base}.runs.csv"))
    expected = {
        "dataset": case["dataset"], "method": "batch" if method == "full" else method,
        "threads": case["threads"], "run": 0, "nbatches": len(counts),
        "verify_ok": verify_ok, "final_m": trace[-1][2],
    }
    require(all(run[key] == str(value) for key, value in expected.items()),
            f"{base}: invalid process summary")
    build, rss = float(run["build_s"]), int(run["rss_mb"])
    require(math.isfinite(build) and build >= 0 and rss > 0 and math.fsum(times) > 0,
            f"{base}: invalid construction time, maintenance time or RSS")
    total = float(run["total_maint_s"])
    require(math.isfinite(total) and abs(total - math.fsum(times) / 1000) <= 1e-6,
            f"{base}: process and batch maintenance times disagree")
    return times, build, rss, groups


def audit_case(root, workload, threads, hashes, counts, trace):
    case_name = f"{workload}-t{threads}"
    directory = root / "results" / case_name
    plan = load_json(directory / "protocol.json")
    case = plan["case"]
    require(plan["citation_doi"] == DOI and plan["methods"] == list(METHODS) and
            plan["timing_replays"] == REPLAYS and case["dataset"] == workload and
            case["scope"] == "full" and case["threads"] == threads and
            case["batch_operations"] == counts,
            f"{case_name}: incompatible workload, methods or protocol")
    require(case["graph_sha256"] == hashes["graph_sha256"] and
            case["stream_sha256"] == hashes["stream_sha256"] == case["source_stream_sha256"],
            f"{case_name}: archived input hashes disagree")
    affinity = set(plan["affinity"])
    require(len(affinity) == threads and affinity <= set(plan["machine"]["affinity"]),
            f"{case_name}: invalid thread affinity")
    check_build(root, directory, plan)

    replay = directory / "replay"
    complete = load_json(replay / "complete.json")
    require((complete["case"], complete["methods"], complete["replays"]) ==
            (case, list(METHODS), REPLAYS), f"{case_name}: incomplete or altered completion record")
    reconstructed = []
    for method in METHODS:
        verified_groups = audit_pass(replay, method, "verify", case, counts, trace)[3]
        samples, builds, peaks, trial_means, totals = [], [], [], [], []
        for trial in range(REPLAYS):
            times, build, rss, groups = audit_pass(
                replay, method, f"timing.{trial}", case, counts, trace)
            require(groups == verified_groups, f"{case_name}: {method} grouping trace changed")
            samples.extend(times)
            builds.append(build)
            peaks.append(rss)
            trial_means.append(mean(times))
            totals.append(math.fsum(times) / 1000)
        samples.sort()
        reconstructed.append({
            "dataset": workload, "scope": "full", "method": method, "threads": threads,
            "batches": len(counts), "operations": sum(counts), "replays": REPLAYS,
            "verified_batches": len(counts), "mean_ms_per_batch": mean(samples),
            "replay_mean_sd_ms": stdev(trial_means),
            "pooled_p95_ms": samples[math.ceil(len(samples) * .95) - 1],
            "mean_maintenance_s": mean(totals), "mean_build_s": mean(builds),
            "peak_process_rss_mib": max(peaks),
        })

    archived = csv_rows(directory / "summary.csv")
    require(len(archived) == len(METHODS), f"{case_name}: incomplete archived summary")
    for row, original in zip(reconstructed, archived):
        row["latency_over_full"] = row["mean_ms_per_batch"] / reconstructed[0]["mean_ms_per_batch"]
        for field, value in row.items():
            matches = close(original[field], value) if field in FLOAT_FIELDS else original[field] == str(value)
            require(matches, f"{case_name}: archived {row['method']} {field} disagrees with replay")
    print(f"AUDIT OK {case_name}: {len(counts)} checkpoints, {sum(counts)} ordered events")
    return reconstructed


TABLE_HEAD = r"""\begin{table}[!tb]
\centering
\caption{Matched comparison with the Luo-Edge reproduction.
Latency is in ms per batch. P95 pools three replays per method.
Bold marks the lowest mean.}
\label{tab:luo-comparison}
\small
\setlength{\tabcolsep}{2pt}
\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}lrrrrr@{}}
\toprule
 & & \multicolumn{2}{c}{Regional} & \multicolumn{2}{c}{Luo-Edge} \\
\cmidrule(lr){3-4}\cmidrule(l){5-6}
Workload & Threads & Mean & P95 & Mean & P95 \\
\midrule"""

TABLE_TAIL = r"""\bottomrule
\end{tabular*}
\end{table}"""


def table(rows):
    keyed = {(row["dataset"], row["threads"], row["method"]): row for row in rows}
    lines = [TABLE_HEAD]
    for workload, label, _, _, digits in WORKLOADS:
        for threads in THREADS:
            pair = [keyed[workload, threads, method] for method in METHODS]
            best = min(row["mean_ms_per_batch"] for row in pair)
            fields = [f"{label if threads == THREADS[0] else '':<15}", str(threads)]
            for row in pair:
                value = f"{row['mean_ms_per_batch']:.{digits}f}"
                if row["mean_ms_per_batch"] == best:
                    value = rf"\textbf{{{value}}}"
                fields.extend([value, f"{row['pooled_p95_ms']:.{digits}f}"])
            lines.append(" & ".join(fields) + r" \\")
    lines.append(TABLE_TAIL)
    return "\n".join(lines) + "\n"


def archived_trace(root, workload):
    path = root / "results" / f"{workload}-t1" / "replay" / "full.verify.batches.csv"
    counts, trace = [], []
    before = None
    for row in csv_rows(path):
        count, deletes, inserts, final = (int(row[key])
                                         for key in ("raw_ops", "deleted", "inserted", "final_m"))
        if before is None:
            before = final + deletes - inserts
        require(0 <= deletes <= before and 0 <= inserts and deletes + inserts <= count and
                final == before - deletes + inserts,
                f"{path}: invalid net counts at batch {len(counts) + 1}")
        counts.append(count)
        trace.append((deletes, inserts, final))
        before = final
    return counts, trace


def audit(root, output, require_inputs):
    archive = (root / "results").resolve()
    require(all(not (output / name).resolve().is_relative_to(archive)
                for name in ("summary.csv", "table9.tex")),
            "choose an output directory outside the archived results")
    manifest = load_json(root / "inputs" / "manifest.json")["datasets"]
    rows = []
    for workload, _, batches, operations, _ in WORKLOADS:
        hashes = manifest[workload]
        require((hashes["batches"], hashes["operations"]) == (batches, operations),
                f"{workload}: unexpected workload size in the manifest")
        graph, stream = (root / "inputs" / workload / name for name in ("graph.txt", "updates.stream"))
        for path, field in ((graph, "graph_sha256"), (stream, "stream_sha256")):
            expected_sha = hashes[field]
            require(re.fullmatch(r"[0-9a-f]{64}", expected_sha), f"{workload}: invalid {field}")
            if path.is_file():
                require(digest(path) == expected_sha, f"{path}: input hash disagrees with the manifest")
            elif require_inputs:
                raise ValueError(f"missing input: {path}")
        if graph.is_file() and stream.is_file():
            counts, trace = stream_trace(graph, stream)
            print(f"{workload}: verified input hashes and recomputed the ordered mutation trace")
        else:
            counts, trace = archived_trace(root, workload)
            print(f"{workload}: auditing archived traces (input materialization is optional)")
        require((len(counts), sum(counts)) == (batches, operations),
                f"{workload}: wrong checkpoint or event count")
        for threads in THREADS:
            rows.extend(audit_case(root, workload, threads, hashes, counts, trace))
    generated = table(rows)
    reference = (root / "table9-reference.tex").read_text()
    match = re.search(r"(?ms)^\\begin\{table\}.*?^\\end\{table\}", reference)
    require(match and " ".join(match.group().split()) == " ".join(generated.split()),
            "rebuilt LaTeX disagrees with the Table 9 reference")
    output.mkdir(parents=True, exist_ok=True)
    with (output / "summary.csv").open("w", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    (output / "table9.tex").write_text(generated)
    print(f"Table 9 reproduced from all six archived configurations in {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="extracted artifact root")
    parser.add_argument("--output", type=Path, required=True,
                        help="directory for rebuilt summary.csv and table9.tex")
    parser.add_argument("--require-inputs", action="store_true",
                        help="require all materialized inputs and independently replay ordered events")
    args = parser.parse_args()
    try:
        audit(args.root, args.output, args.require_inputs)
    except (OSError, ValueError, KeyError) as error:
        raise SystemExit(f"ERROR: {error}") from error


if __name__ == "__main__":
    main()
