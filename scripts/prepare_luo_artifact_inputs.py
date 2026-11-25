#!/usr/bin/env python3
"""Restore the three Table 9 inputs from SNAP and verify the archived hashes."""

import argparse
from contextlib import contextmanager
import fcntl
import gzip
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, build_opener

from compare_luo_edge_reproduction import FLAGS, SOURCES
from compare_replay import digest
from run_temporal_windows import interactions, project


DATASETS = ("askubuntu-w7d", "stackoverflow-w7d", "amazon")


def snap_url(url):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or parsed.netloc != "snap.stanford.edu" or not parsed.path.startswith("/data/"):
        raise ValueError(f"expected an official SNAP input URL: {url}")
    return url


class SnapRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, snap_url(newurl))


def check_hash(path, expected):
    actual = digest(path)
    if actual != expected:
        raise ValueError(f"{path}: expected SHA-256 {expected}, found {actual}")


@contextmanager
def source_archive(cache, spec):
    filename = spec["source_filename"]
    if not filename or Path(filename).name != filename or filename in (".", ".."):
        raise ValueError(f"invalid cache filename: {filename}")
    expected = spec.get("source_sha256")
    archive = cache / filename
    if archive.exists():
        if expected:
            check_hash(archive, expected)
        print(f"Using cached {archive}", flush=True)
        yield archive
        return

    url = snap_url(spec["source_url"])
    print(f"Downloading {url}", flush=True)
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as temp:
        staged = Path(temp) / filename
        with build_opener(SnapRedirect()).open(url, timeout=60) as response, staged.open("wb") as out:
            shutil.copyfileobj(response, out)
        if expected:
            check_hash(staged, expected)
        yield staged
        # Keep the archive only after the exact replay input is recovered.
        staged.replace(archive)


def recover_amazon(root, graph, staged, batches):
    binary = staged.parent / "generator"
    subprocess.run(["g++", *FLAGS, SOURCES["full"], "-o", str(binary)], cwd=root, check=True)
    subprocess.run([str(binary), "gen", "--graph", str(graph), "--out", str(staged / "updates.stream"),
                    "--batches", str(batches), "--batch-size", "1000", "--p-insert", "0.5", "--seed", "42"],
                   check=True)


def recover_dataset(root, cache, dataset, spec):
    destination = root / "inputs" / dataset
    hashes = {"graph.txt": spec["graph_sha256"], "updates.stream": spec["stream_sha256"]}
    if dataset != "amazon":
        hashes["checkpoints.csv"] = spec["checkpoints_sha256"]

    def check_input(directory):
        for name, expected in hashes.items():
            check_hash(directory / name, expected)

    if destination.exists():
        check_input(destination)
        print(f"VERIFIED {dataset}: inputs already recovered", flush=True)
        return

    with tempfile.TemporaryDirectory(prefix=f".{dataset}-", dir=destination.parent) as temp:
        staged = Path(temp) / dataset
        staged.mkdir()
        graph = staged / "graph.txt"
        cached_graph = cache / "amazon.graph.txt"
        if dataset == "amazon" and cached_graph.is_file():
            print(f"Using cached {cached_graph}", flush=True)
            shutil.copyfile(cached_graph, graph)
            check_hash(graph, hashes["graph.txt"])
            recover_amazon(root, graph, staged, spec["batches"])
            check_input(staged)
        else:
            with source_archive(cache, spec) as archive:
                if dataset == "amazon":
                    with gzip.open(archive, "rb") as source, graph.open("wb") as out:
                        shutil.copyfileobj(source, out)
                    check_hash(graph, hashes["graph.txt"])
                    recover_amazon(root, graph, staged, spec["batches"])
                else:
                    start, end = spec["start_utc"], spec["end_utc"]
                    nodes, times, edges = interactions(archive, start=start, end=end,
                                                        windows=spec["projection_window_days"])
                    retention, = spec["window_days"]
                    project(staged, nodes, times, edges, retention, end, start=start)
                check_input(staged)
        staged.rename(destination)
    print(f"VERIFIED {dataset}: restored {spec['batches']} batches and {spec['operations']} operations", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="artifact root containing inputs/manifest.json")
    parser.add_argument("--cache", type=Path, help="directory for downloaded or prefetched SNAP files")
    parser.add_argument("--dataset", choices=DATASETS, action="append", help="recover just this dataset, repeatable")
    args = parser.parse_args()

    root = args.root.resolve()
    inputs = root / "inputs"
    specs = json.loads((inputs / "manifest.json").read_text())["datasets"]
    cache = args.cache.resolve() if args.cache else inputs / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    with (inputs / ".prepare.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for dataset in dict.fromkeys(args.dataset or DATASETS):
            recover_dataset(root, cache, dataset, specs[dataset])


if __name__ == "__main__":
    try:
        main()
    except (EOFError, KeyError, OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
