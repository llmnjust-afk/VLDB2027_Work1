#!/usr/bin/env python3
"""Stage and run the independently verified VLDB revision experiments."""

import argparse
import csv
import fcntl
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from compare_replay import csv_rows, digest
from summarize_replay import verify

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / ".build"
INPUTS = BUILD / "vldb2027-inputs"
GENERATED = ("email", "wikivote", "amazon")
TEMPORAL = {
    "email-temporal": ("vldb2027-temporal.hLdLNt", "email-temporal-w7d"),
    "askubuntu": ("vldb2027-askubuntu.PAgZvq", "askubuntu-temporal-w7d"),
}
CORE = BUILD / "vldb2027-core.ISeXI1"
ABLATION_ARCHIVE = {
    "email": BUILD / "vldb2027-nomerge-email-20261120-145626",
    "amazon": BUILD / "vldb2027-nomerge-amazon-20261120-230119",
}
MAIN_METHODS = ("full", "static", "msp")
ABLATION_METHODS = ("full", "nomerge")
LATENCY_CLOCK = "steady_clock; update application and label maintenance included"
LATENCY_SCOPE = "separate; graph/stream loading, checks and output excluded from update latency"


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2) + "\n")
    temporary.replace(path)


def load_json(path):
    return json.loads(path.read_text())


def cgroup_value(controller, attribute):
    groups = {}
    for line in Path("/proc/self/cgroup").read_text().splitlines():
        _, names, name = line.split(":", 2)
        for key in names.split(","):
            groups[key] = name.lstrip("/")
    unified = "" in groups
    mount = Path("/sys/fs/cgroup")
    if unified:
        leaf = mount / groups[""]
    else:
        mounts = [entry for entry in mount.glob("*") if controller in entry.name.split(",")]
        if not mounts or controller not in groups:
            return []
        mount = mounts[0]
        leaf = mount / groups[controller]
    values = []
    for directory in (leaf, *leaf.parents):
        path = directory / attribute[0 if unified else 1]
        if path.is_file():
            values.append((directory, path.read_text().strip()))
        if directory == mount:
            break
    return values


def machine():
    affinity = sorted(os.sched_getaffinity(0))
    quota = []
    for directory, value in cgroup_value("cpu", ("cpu.max", "cpu.cfs_quota_us")):
        fields = value.split()
        if len(fields) == 1 and int(fields[0]) > 0:
            fields.append((directory / "cpu.cfs_period_us").read_text().strip())
        if len(fields) == 2 and fields[0] != "max":
            quota.append(int(fields[0]) / int(fields[1]))
    memory = []
    for _, value in cgroup_value("memory", ("memory.max", "memory.limit_in_bytes")):
        if value != "max" and int(value) < 1 << 60:
            memory.append(int(value))
    hardware = subprocess.check_output(["lscpu"], text=True, env={**os.environ, "LC_ALL": "C"})
    model = next((line.split(":", 1)[1].strip() for line in hardware.splitlines()
                  if line.startswith("Model name:")), platform.machine())
    return {
        "host": socket.gethostname(), "cpu_model": model, "platform": platform.platform(),
        "affinity": affinity, "cpu_quota": min(quota, default=None),
        "thread_budget": max(1, min(len(affinity), int(min(quota, default=len(affinity))))),
        "memory_limit_bytes": min(memory, default=None),
        "runtime_environment": {key: value for key, value in sorted(os.environ.items())
                                if key.startswith(("OMP_", "GOMP_", "KMP_")) or
                                key in ("GLIBC_TUNABLES", "LD_PRELOAD", "LD_LIBRARY_PATH")},
    }, hardware


def read_batches(path):
    batches = []
    with path.open("rb") as stream:
        for line in stream:
            header = line.split()
            if len(header) != 2 or header[0] != b"B" or not header[1].isdigit():
                raise ValueError(f"{path}: invalid batch header after batch {len(batches)}")
            batch = [stream.readline() for _ in range(int(header[1]))]
            for operation in batch:
                fields = operation.split()
                if (len(fields) != 3 or fields[0] not in (b"I", b"D") or
                        not all(value.isdigit() for value in fields[1:])):
                    raise ValueError(f"{path}: invalid operation in batch {len(batches) + 1}")
            batches.append(batch)
    if not batches:
        raise ValueError(f"{path}: no batches")
    return batches


def write_stream(path, batches):
    data = b"".join(f"B {len(batch)}\n".encode() + b"".join(
        row if row.endswith(b"\n") else row + b"\n" for row in batch) for batch in batches)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_bytes() != data:
            raise ValueError(f"saved stream differs from planned input: {path}")
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as output:
            output.write(data)
        Path(output.name).replace(path)


def input_files(dataset):
    if dataset in TEMPORAL:
        directory, label = TEMPORAL[dataset]
        parent = BUILD / directory
        manifest = parent / "SHA256SUMS"
        graph, stream = parent / "input/graph.txt", parent / "input/updates.stream"
    else:
        label = dataset
        parent = INPUTS
        manifest = parent / "inputs.sha256"
        graph = parent / f"{dataset}.graph.txt"
        stream = parent / f"{dataset}_new_b1000_p0.5.txt"
    entries = dict((filename.strip(), sha) for sha, filename in
                   (line.split(maxsplit=1) for line in manifest.read_text().splitlines()))
    for path in (graph, stream):
        if digest(path) != entries.get(str(path.relative_to(parent))):
            raise ValueError(f"missing or mismatched registered hash: {path}")
    return label, graph, stream


def source_snapshot():
    paths = [ROOT / "Makefile", *(ROOT / "src").glob("*")]
    for subdir in ("GKlib", "src"):
        paths += list((ROOT / "baselines/AccTrussDecomposition/msp" / subdir).rglob("*"))
    return {str(path.relative_to(ROOT)): digest(path) for path in sorted(paths)
            if path.is_file() and (path.name == "Makefile" or path.suffix in (".c", ".cpp", ".h", ".hpp"))}


def preflight(run, jobs):
    for command in ("gcc", "g++", "make", "lscpu", "sha256sum", "pdfinfo", "latexmk"):
        print(f"{command}: {shutil.which(command) or 'unavailable'}")
    identity, hardware = machine()
    print(f"{identity['host']} | {identity['cpu_model']} | affinity={len(identity['affinity'])} "
          f"quota={identity['cpu_quota']} effective={identity['thread_budget']} "
          f"memory limit={identity['memory_limit_bytes']}", flush=True)
    run.mkdir(parents=True, exist_ok=True)
    (run / "hardware.txt").write_text(hardware)
    write_json(run / "environment.json", identity)
    provenance = run / "input-provenance"
    provenance.mkdir(exist_ok=True)
    input_status = {}
    for dataset in (*GENERATED, *TEMPORAL):
        try:
            _, graph, stream = input_files(dataset)
            input_status[dataset] = {str(path): digest(path) for path in (graph, stream)}
            print(f"INPUT OK {dataset}: {graph} + {stream}", flush=True)
        except (OSError, ValueError) as error:
            input_status[dataset] = {"missing": str(error)}
            print(f"INPUT MISSING {dataset}: {error}", file=sys.stderr, flush=True)
    write_json(provenance / "inputs.json", input_status)
    manifests = [INPUTS / "input-manifest.tsv", INPUTS / "inputs.sha256"]
    for name, _ in TEMPORAL.values():
        folder = BUILD / name
        manifests.extend(folder / relative for relative in
                         ("SHA256SUMS", "input/manifest.json", "input/checkpoints.csv", "prepare_temporal.py"))
    for path in manifests:
        if path.is_file():
            target = provenance / path.relative_to(BUILD)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
    for relative in ("index/index.out", "ballDel/ballDel.out", "ballsIns/ballsIns.out"):
        path = BUILD / "TrussMaintenance/code" / relative
        print(f"Star CLI {relative}: {'executable' if os.access(path, os.X_OK) else 'unavailable'}")
    baseline = ROOT / "VLDB2027_Work1-main/paper/main-current.pdf"
    if shutil.which("pdfinfo") and baseline.is_file():
        (run / "baseline-pdfinfo.txt").write_bytes(subprocess.check_output(["pdfinfo", baseline]))
        (run / "baseline.sha256").write_text(f"{digest(baseline)}  {baseline}\n")
    binary = build_binary(run, jobs, identity)
    print(f"Built Full, Static and MSP: {binary}")
    print(f"ENV READY {run}", flush=True)


def build_binary(run, jobs, identity):
    binary = run / "build/batchtruss-msp"
    stamp = run / "build-manifest.json"
    sources = source_snapshot()
    compilers = {name: subprocess.check_output([name, "--version"], text=True).splitlines()[0]
                 for name in ("g++", "gcc")}
    signature = dict(sources=sources, compilers=compilers, host=identity["host"],
                     cpu_model=identity["cpu_model"], platform=identity["platform"])
    if stamp.is_file():
        if load_json(stamp) != dict(signature, binary_sha256=digest(binary)):
            raise ValueError("run directory has a different build or host; use a new --run directory")
    else:
        for relative in sources:
            target = run / "build" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / relative, target)
        with (run / "build.log").open("w") as log:
            subprocess.run(["make", "-B", "-C", str(run / "build"), "-j",
                            str(min(jobs, identity["thread_budget"])), "all", "batchtruss-msp"],
                           stdout=log, stderr=subprocess.STDOUT, check=True)
        write_json(stamp, dict(signature, binary_sha256=digest(binary)))
    return binary


def check_config(directory, spec):
    summaries = verify(directory)
    meta = load_json(directory / "manifest.json")
    inputs = list(meta["inputs"].values())
    if (inputs[:2] != spec["inputs"] or len(inputs) != 3 or
            meta["dataset"] != spec["label"] or meta["threads_requested"] != spec["threads"] or
            meta["runs"] != spec["runs"] or meta["clock"] != LATENCY_CLOCK or
            meta["build"] != LATENCY_SCOPE or
            not set(spec["methods"]).issubset(meta["methods"])):
        raise ValueError(f"incompatible replay protocol in {directory}")
    full = csv_rows(directory / "full.verify.batches.csv")
    if [int(row["raw_ops"]) for row in full] != spec["counts"]:
        raise ValueError(f"unexpected batch boundaries in {directory}")
    for summary in summaries:
        method = summary["method"]
        threads = 1 if method == "peredge" else spec["threads"]
        if int(summary["threads"]) != threads:
            raise ValueError(f"unexpected thread count for {method} in {directory}")
        for kind in ("verify", "timing"):
            rows = csv_rows(directory / f"{method}.{kind}.runs.csv")
            expected = 1 if kind == "verify" else spec["runs"]
            status = "1" if kind == "verify" else ""
            if len(rows) != expected or any(
                    (int(row["run"]), int(row["threads"]), row["verify_ok"]) !=
                    (index, threads, status) for index, row in enumerate(rows)):
                raise ValueError(f"invalid {kind} run summary for {method} in {directory}")
    if "binary_sha256" in spec and inputs[2] != spec["binary_sha256"]:
        raise ValueError(f"binary differs from plan in {directory}")
    if spec.get("separate_timing_processes") and not meta.get("separate_timing_processes"):
        raise ValueError(f"timing process protocol differs from plan in {directory}")
    return meta


def record_config(run, group, key, spec, archive=None, identity=None, binary=None):
    plan_dir = run / "plan" / group
    plan_dir.mkdir(parents=True, exist_ok=True)
    output_dir = run / group
    output_dir.mkdir(exist_ok=True)
    plan_path = plan_dir / f"{key}.json"
    output = output_dir / key
    if archive is None:
        spec.update(binary_sha256=digest(binary), environment=identity, separate_timing_processes=True)
    else:
        spec.update(archive=str(archive))
    with (plan_dir / f"{key}.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if plan_path.is_file() and load_json(plan_path) != spec:
            raise ValueError(f"run plan changed for {group}/{key}; use a new --run directory")
        write_json(plan_path, spec)
        if (output / "complete").is_file():
            try:
                check_config(output, spec)
            except (OSError, ValueError, KeyError) as error:
                print(f"RETRY {group}/{key} (invalid existing output: {error})", flush=True)
            else:
                print(f"SKIP {group}/{key} (already verified)", flush=True)
                return
        if output.exists():
            attempts = output_dir / "_attempts"
            attempts.mkdir(exist_ok=True)
            output.rename(attempts / f"{key}-{time.time_ns()}")
        if archive is not None:
            check_config(archive, spec)
            shutil.copytree(archive, output, ignore=shutil.ignore_patterns("complete"))
            shutil.copy2(archive / "complete", output / "complete")
            print(f"REUSED {group}/{key} <- {archive}", flush=True)
        else:
            print(f"RUN {group}/{key}", flush=True)
            start = time.monotonic()
            command = [sys.executable, "-B", str(ROOT / "scripts/compare_replay.py"),
                       "--graph", spec["graph"], "--stream", spec["stream"], "--binary", str(binary),
                       "--dataset", spec["label"], "--threads", str(spec["threads"]),
                       "--runs", str(spec["runs"]), "--output", str(output),
                       "--separate-timing-processes", "--methods", *spec["methods"]]
            with (output_dir / f"{key}.log").open("a") as log:
                log.write(f"\nStarted {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n")
                log.flush()
                result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT)
                log.write(f"Exit {result.returncode}; wall seconds {time.monotonic() - start:.3f}\n")
            if result.returncode:
                raise ValueError(f"replay failed; see {output_dir / (key + '.log')}")
            check_config(output, spec)
            print(f"DONE {group}/{key} ({time.monotonic() - start:.1f} s wall)", flush=True)


def spec_for(dataset, label, graph, stream, batches, threads, runs, methods, batch_size=None):
    return dict(dataset=dataset, label=label, graph=str(graph), stream=str(stream),
                inputs=[digest(graph), digest(stream)], counts=[len(batch) for batch in batches],
                threads=threads, runs=runs, methods=list(methods), batch_size=batch_size)


def prepared_binary(run, identity):
    binary = run / "build/batchtruss-msp"
    stamp = load_json(run / "build-manifest.json")
    if (stamp["host"], stamp["platform"], stamp["cpu_model"], stamp["binary_sha256"]) != (
            identity["host"], identity["platform"], identity["cpu_model"], digest(binary)):
        raise ValueError("binary or host differs from preflight; run env in a fresh --run directory")
    return binary


def run_group(args):
    run = args.run
    group = args.action
    identity, _ = machine()
    if group == "ablation":
        group += f"-prefix-{args.prefix_batches}" if args.prefix_batches else "-full"
    if getattr(args, "fresh", False):
        group += "-fresh"
    if args.action == "smoke":
        binary = prepared_binary(run, identity)
        with (run / "regression.log").open("w") as log:
            subprocess.run([sys.executable, "-B", str(ROOT / "tests/test_replay.py"),
                            "--binary", str(binary)], stdout=log, stderr=subprocess.STDOUT, check=True)
        graph = ROOT / "tests/fixtures/k4.graph"
        stream = ROOT / "tests/fixtures/k4-mixed.stream"
        spec = spec_for("k4", "k4", graph, stream, read_batches(stream), 1, 3,
                        ("full", "nomerge", "peredge", "static", "msp"))
        record_config(run, group, "k4", spec, identity=identity, binary=binary)
        return

    if args.action == "main":
        datasets = (*GENERATED, *TEMPORAL)
    elif args.action in ("ablation", "pilot"):
        datasets = ("email", "amazon")
    else:
        datasets = ("amazon", "wikivote")
    if args.action == "threads":
        if args.max_threads not in (1, 2, 4, 8, 16):
            raise ValueError("--max-threads must be a power of two from 1 to 16")
        grid = [threads for threads in (1, 2, 4, 8, 16)
                if threads <= min(args.max_threads, identity["thread_budget"])]
        grid_path = run / "threads-grid.json"
        if grid_path.exists():
            previous = load_json(grid_path)
            if previous["environment"] != identity:
                raise ValueError("thread sweep environment changed; use a fresh --run directory")
            grid = sorted(set(grid) | set(previous["threads"]))
        write_json(grid_path, {"environment": identity, "threads": grid})
        print(f"THREAD GRID {grid} (effective budget {identity['thread_budget']})", flush=True)
    failures = 0
    for dataset in datasets:
        if args.datasets and dataset not in args.datasets:
            continue
        try:
            label, graph, original = input_files(dataset)
            batches = read_batches(original)
            if dataset in GENERATED and (len(batches) != 200 or any(len(batch) != 1000 for batch in batches)):
                raise ValueError(f"{dataset}: expected the registered 200 x 1000 stream")
            jobs = []
            if args.action == "main":
                archive = CORE / f"{dataset}-t1" if dataset in GENERATED else BUILD / TEMPORAL[dataset][0] / "native"
                jobs.append((dataset, 1, 3 if args.fresh or dataset in TEMPORAL else 5, MAIN_METHODS,
                             original, batches, None, None if args.fresh else archive))
            elif args.action == "batch-size":
                ops = [operation for batch in batches[:20] for operation in batch]
                for size in (50, 100, 250, 500, 1000):
                    stream = run / "streams" / f"{dataset}-first20000-b{size}.stream"
                    regrouped = [ops[i:i + size] for i in range(0, len(ops), size)]
                    write_stream(stream, regrouped)
                    jobs.append((f"{dataset}-b{size}", 1, 3, ("full", "msp"), stream, regrouped, size, None))
            elif args.action == "threads":
                for threads in grid:
                    jobs.append((f"{dataset}-t{threads}", threads, 3, ("full",),
                                 original, batches, 1000, None))
            else:
                prefix = args.prefix_batches
                if prefix:
                    stream = run / "streams" / f"{dataset}-first{prefix * 1000}-b1000.stream"
                    selected = batches[:prefix]
                    write_stream(stream, selected)
                else:
                    stream, selected = original, batches
                archive = None
                if args.action == "ablation" and not prefix and not args.fresh:
                    archive = ABLATION_ARCHIVE[dataset]
                runs = 1 if args.action == "pilot" or (archive and dataset == "email") else 3
                jobs.append((dataset, 1, runs, ABLATION_METHODS, stream, selected, 1000, archive))
            for key, threads, runs, methods, stream, selected, size, archive in jobs:
                try:
                    spec = spec_for(dataset, label, graph, stream, selected, threads, runs, methods, size)
                    binary = None if archive else prepared_binary(run, identity)
                    record_config(run, group, key, spec, archive, identity, binary)
                except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
                    failures += 1
                    print(f"FAILED {group}/{key}: {error}", file=sys.stderr, flush=True)
        except (OSError, ValueError) as error:
            failures += 1
            print(f"FAILED {group}/{dataset}: {error}", file=sys.stderr, flush=True)
    if failures:
        raise ValueError(f"{failures} configuration or input failure(s) in {group}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", required=True, type=Path)
    commands = parser.add_subparsers(dest="action", required=True)
    env = commands.add_parser("env")
    env.add_argument("--jobs", type=int, default=4)
    commands.add_parser("smoke")
    for name in ("main", "batch-size", "threads", "pilot", "ablation"):
        sub = commands.add_parser(name)
        allowed = ((*GENERATED, *TEMPORAL) if name == "main" else
                   ("amazon", "wikivote") if name in ("batch-size", "threads") else ("email", "amazon"))
        sub.add_argument("--datasets", nargs="+", choices=allowed)
        if name in ("main", "ablation"):
            sub.add_argument("--fresh", action="store_true")
        if name in ("pilot", "ablation"):
            sub.add_argument("--prefix-batches", type=int, default=3 if name == "pilot" else 0)
        if name == "threads":
            sub.add_argument("--max-threads", type=int, default=8)
    args = parser.parse_args()
    args.run = args.run.resolve()
    if args.action == "env" and args.jobs < 1:
        parser.error("--jobs must be positive")
    prefix = getattr(args, "prefix_batches", None)
    if prefix is not None and not ((1 if args.action == "pilot" else 0) <= prefix < 200):
        parser.error("--prefix-batches must be within the registered 200-batch stream")
    try:
        if args.action == "env":
            preflight(args.run, args.jobs)
        else:
            run_group(args)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
