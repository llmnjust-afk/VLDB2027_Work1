#!/usr/bin/env python3
"""Prepare matched concentrated and dispersed update batches on an unchanged input graph."""

import argparse
import fcntl
from pathlib import Path
import subprocess
import tempfile

from compare_replay import digest
from revision_experiments import ROOT, input_files, load_json, write_json


CONDITIONS = ("concentrated", "dispersed")
ARTIFACTS = (*[f"{condition}.stream" for condition in CONDITIONS], "placement.csv")
FLAGS = ("-O3", "-march=native", "-std=c++17", "-pthread")


def prepare(output, dataset, batches, batch_size, seed):
    graph = input_files(dataset)[1].resolve(strict=True)
    source = ROOT / "scripts/prepare_locality.cpp"
    sources = (Path(__file__).resolve(), source, *(ROOT / "src" / filename for filename in
               ("common.h", "graph.h", "truss_static.h", "stream.h")))
    output.mkdir(parents=True, exist_ok=True)
    output = output.resolve(strict=True)
    manifest = output / "manifest.json"
    plan = {
        "dataset": dataset, "graph": str(graph), "graph_sha256": digest(graph),
        "batch_size": batch_size, "batches": batches, "seed": seed,
        "cases": [{"condition": condition, "stream": str(output / f"{condition}.stream")}
                  for condition in CONDITIONS],
        "placement_csv": str(output / "placement.csv"),
        "placement_unit": "triangle_component" if dataset == "amazon" else "witness_anchor",
        "treatment_batches": "odd-numbered batches; each even batch reverses the preceding operations",
        "triangle_controls": "one distinct destroyed and created triangle per net deletion and insertion",
        "source_sha256": {str(path.relative_to(ROOT)): digest(path) for path in sources},
        "compiler": subprocess.check_output(["g++", "--version"], text=True).splitlines()[0],
        "compiler_flags": list(FLAGS),
    }
    with (output / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if manifest.exists():
            saved = load_json(manifest)
            if (any(saved.get(key) != value for key, value in plan.items()) or
                    saved["artifacts"] != {name: digest(output / name) for name in ARTIFACTS}):
                raise ValueError(f"{output}: existing locality inputs differ from the requested inputs")
            print(f"REUSED locality inputs {manifest}", flush=True)
            return saved
        with tempfile.TemporaryDirectory(dir=output, prefix="prepare.") as temp:
            stage = Path(temp)
            binary = stage / "prepare-locality"
            with (output / "prepare.compile.log").open("w") as log:
                subprocess.run(["g++", *FLAGS, "-I", ROOT / "src", source, "-o", binary],
                               stdout=log, stderr=subprocess.STDOUT, check=True)
            run = subprocess.run([binary, graph, dataset, str(batches), str(batch_size), str(seed), stage],
                                 text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            (output / "prepare.log").write_text(run.stdout + run.stderr)
            run.check_returncode()
            for name in ARTIFACTS:
                (stage / name).replace(output / name)
        vertices, edges, components, largest = map(int, run.stdout.split())
        result = dict(plan, initial_vertices=vertices, initial_edges=edges,
                      nontrivial_triangle_components=components, largest_triangle_component_edges=largest,
                      artifacts={name: digest(output / name) for name in ARTIFACTS})
        write_json(manifest, result)
    print(f"PREPARED locality inputs {manifest}", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dataset", choices=("amazon", "email"), required=True)
    parser.add_argument("--batches", type=int, default=20, help="number of treatment and restoration pairs")
    parser.add_argument("--batch-size", type=int, default=100, help="operations in each treatment and reset batch")
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()
    if (args.batches < 1 or args.batch_size < 2 or args.batch_size % 2
            or not 0 <= args.seed < 1 << 64):
        parser.error("need at least one treatment batch, an even batch size, and an unsigned 64-bit seed")
    try:
        prepare(args.output, args.dataset, args.batches, args.batch_size, args.seed)
    except subprocess.CalledProcessError as error:
        parser.error(f"{error}; see {args.output}/prepare*.log")
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
