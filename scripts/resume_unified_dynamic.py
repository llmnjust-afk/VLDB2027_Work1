#!/usr/bin/env python3
"""Resume a recorded unified comparison at whole-process replay boundaries."""

import argparse
import fcntl
import math
import os
from pathlib import Path
import subprocess
import tempfile

import run_unified_dynamic as runner
from compare_replay import csv_rows, digest
from compare_star_resident import audit_replay, write_summary
from revision_experiments import load_json, machine, write_json


def replay_command(prefix, case, method, tag, binaries):
    """Match the argv recorded by the fingerprinted unified runner."""
    command = [str(binaries.get(method, binaries["full"]))]
    if method in runner.ABLATIONS:
        command += ["batch", "--ablate", runner.ABLATIONS[method]]
    elif method not in binaries:
        command += [method]
    command += ["--graph", case["graph"], "--stream", case["stream"], "--dataset", case["dataset"],
                "--threads", "1", "--runs", "1", "--batch-csv", f"{prefix}.batches.csv"]
    command += ["--check-every", "1"] if tag == "verify" else ["--timing-only"]
    if method == "adaptive":
        command += ["--route-csv", f"{prefix}.routes.csv"]
    if tag == "profile":
        command += ["--phase-csv", f"{prefix}.phases.csv", "--peel-scan-csv", f"{prefix}.scans.csv"]
    return command


def check_saved_command(prefix, case, method, tag, binaries):
    if load_json(Path(f"{prefix}.runs.argv.json")) != replay_command(prefix, case, method, tag, binaries):
        raise ValueError("saved command differs from the recorded replay protocol")


def check_profile(prefix, regions, count):
    phases = csv_rows(Path(f"{prefix}.phases.csv"))
    scans = csv_rows(Path(f"{prefix}.scans.csv"))
    expected = [("0", str(batch)) for batch in range(1, count + 1)]
    if any([(row["run"], row["batch"]) for row in rows] != expected for rows in (phases, scans)):
        raise ValueError("incomplete profile")
    phase_values = (float(row[key]) for row in phases for key in
                    ("coalesce_us", "witness_us", "apply_us", "closure_us", "recount_us", "peel_us"))
    if any(not math.isfinite(value) or value < 0 for value in phase_values):
        raise ValueError("invalid phase timing")
    for region, row in zip(regions, scans, strict=True):
        fields = ("region_edges", "certified_edges", "scanned_edges", "scan_degree_sum")
        size, removed, scanned, degrees = (int(row[key]) for key in fields)
        if size != region or removed + scanned != region or min(removed, scanned, degrees) < 0:
            raise ValueError("invalid profile scan counts")


def check_replay(directory, case, method, tag, binaries, checkpoints, verified):
    prefix = directory / f"{method}.{tag}"
    check_saved_command(prefix, case, method, tag, binaries)
    trace, latency = audit_replay(directory, method, tag, case["batch_operations"], tag == "verify")
    run, = csv_rows(Path(f"{prefix}.runs.csv"))
    identity = ("batch", runner.ABLATIONS[method]) if method in runner.ABLATIONS else (method, "-")
    if (run["method"], run.get("ablate", "-")) != identity or run["dataset"] != case["dataset"]:
        raise ValueError("replay summary has a different method or workload")
    rows = csv_rows(Path(f"{prefix}.batches.csv"))
    regions = [int(row["region_edges"]) for row in rows] if method in runner.ABLATIONS else []
    if regions and any(not 0 <= size <= batch[2] for size, batch in zip(regions, trace, strict=True)):
        raise ValueError("invalid regional edge counts")
    data = trace, regions, latency, rows
    if tag == "verify":
        if checkpoints is not None and method != "adaptive":
            scope = "ordered" if method == "static" else "net"
            expected = [(int(row[f"{scope}_deletions"]), int(row[f"{scope}_insertions"]),
                         int(row["active_edges"])) for row in checkpoints]
            if trace != expected:
                raise ValueError("replay disagrees with the prepared checkpoints")
        if method in runner.PRUNING_PROFILES and "full" in verified and data[:2] != verified["full"][:2]:
            raise ValueError("regional trace differs from Full")
        if method == "adaptive" and {"full-fused", "static"} <= verified.keys():
            runner.audit_route(directory, case, tag, data, verified["full-fused"], verified["static"])
    elif method in verified and data[:2] != verified[method][:2]:
        raise ValueError("replay disagrees with its independent verification")
    if tag.startswith("timing."):
        build_s, rss = float(run["build_s"]), int(run["rss_mb"])
        if not math.isfinite(build_s) or build_s < 0 or rss <= 0:
            raise ValueError("invalid initialization time or peak RSS")
        if method == "adaptive" and {"full-fused", "static"} <= verified.keys():
            runner.audit_route(directory, case, tag, data, verified["full-fused"], verified["static"])
    if tag == "profile":
        check_profile(prefix, regions, len(trace))
    return data


def schedule(methods, runs, profiles):
    yield from ((method, "verify") for method in methods)
    for trial in range(runs):
        offset = trial % len(methods)
        yield from ((method, f"timing.{trial}") for method in methods[offset:] + methods[:offset])
    if profiles:
        yield from ((method, "profile") for method in runner.PRUNING_PROFILES)


def archive_partial(output, directory, method, tag):
    paths = list(directory.glob(f"{method}.{tag}.*"))
    if paths:
        failed = output / "failed"
        failed.mkdir(exist_ok=True)
        target = Path(tempfile.mkdtemp(prefix=f"{directory.name}.{method}.{tag}.", dir=failed))
        for path in paths:
            path.rename(target / path.name)
        print(f"ARCHIVED {target}", flush=True)


def check_protocol(plan, binaries):
    methods = tuple(plan["methods"])
    profile_pruned = plan.get("profile_pruned", False)
    if (methods not in (runner.METHODS, (*runner.METHODS, "full-fused-pruned"))
            or plan["timing_replays"] < 1 or plan["threads"] != 1
            or plan["timer_scope"] != runner.SCOPE or plan["adaptive_policy"] != runner.POLICY
            or not plan["cases"] or (profile_pruned and "full-fused-pruned" not in methods)
            or plan["flags"] != list(runner.FLAGS) or plan["sources"] != runner.fingerprint()
            or plan["binary_sha256"] != {method: digest(path) for method, path in binaries.items()}):
        raise ValueError("saved build, sources or comparison protocol differs")
    identity, _ = machine()
    if (plan["cpu"] not in identity["affinity"]
            or any(identity[key] != plan["machine"][key] for key in ("host", "cpu_model", "platform"))
            or any(identity["runtime_environment"].get(key) != plan["machine"]["runtime_environment"].get(key)
                   for key in ("GLIBC_TUNABLES", "LD_PRELOAD", "LD_LIBRARY_PATH"))):
        raise ValueError("resume on the recorded host and CPU with the original loader environment")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="existing unified result directory")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--preview", action="store_true", help="audit saved replays without starting processes")
    mode.add_argument("--max-new-replays", type=int, help="pause after at most this many new processes")
    args = parser.parse_args()
    if args.max_new_replays is not None and args.max_new_replays < 1:
        parser.error("--max-new-replays must be positive")
    output = args.output.resolve(strict=True)
    binaries = runner.binary_paths(output)
    with (output / ".lock").open("r" if args.preview else "r+") as lock:
        fcntl.flock(lock, (fcntl.LOCK_SH if args.preview else fcntl.LOCK_EX) | fcntl.LOCK_NB)
        plan = load_json(output / "protocol.json")
        check_protocol(plan, binaries)
        if not args.preview:
            os.sched_setaffinity(0, {plan["cpu"]})
        env = {key: value for key, value in os.environ.items() if not key.startswith(("OMP_", "GOMP_", "KMP_"))}
        env |= {"OMP_NUM_THREADS": "1", "OMP_DYNAMIC": "FALSE", "LC_ALL": "C"}
        methods, runs = plan["methods"], plan["timing_replays"]
        profiles = plan.get("profile_pruned", False)
        summary, details, diagnostics = [], [], []
        new_replays = 0
        for case in plan["cases"]:
            dataset = case["dataset"]
            directory = output / dataset
            complete = directory / "complete.json"
            for key in ("graph", "stream", "checkpoints", "source_stream", "input_manifest"):
                if key in case and digest(Path(case[key])) != case[f"{key}_sha256"]:
                    raise ValueError(f"{dataset}: recorded input {key} changed")
            checkpoints = runner.checkpoint_rows(case)
            if complete.is_file():
                for method, tag in schedule(methods, runs, profiles):
                    check_saved_command(directory / f"{method}.{tag}", case, method, tag, binaries)
                rows, trials, profile = runner.validate_case(output, case, methods, runs, binaries, env, True, profiles)
                summary.extend(rows)
                details.extend(trials)
                diagnostics.extend(profile)
                continue
            if not args.preview:
                directory.mkdir(exist_ok=True)
            verified = {}
            verification_checked = False
            for method, tag in schedule(methods, runs, profiles):
                if tag != "verify" and not args.preview and not verification_checked:
                    runner.audit_tag(directory, case, methods, checkpoints, "verify")
                    verification_checked = True
                try:
                    data = check_replay(directory, case, method, tag, binaries, checkpoints, verified)
                    print(f"REUSED {dataset} {method} {tag}", flush=True)
                except (OSError, KeyError, TypeError, ValueError) as error:
                    if args.preview:
                        reason = "files missing" if isinstance(error, FileNotFoundError) else str(error)
                        print(f"PENDING {dataset} {method} {tag}: {reason}", flush=True)
                        continue
                    if new_replays == args.max_new_replays:
                        print(f"PAUSED before {dataset} {method} {tag}; run the resume command again", flush=True)
                        return
                    archive_partial(output, directory, method, tag)
                    print(f"RUNNING {dataset} {method} {tag}", flush=True)
                    runner.replay(directory, case, method, tag, binaries, env)
                    data = check_replay(directory, case, method, tag, binaries, checkpoints, verified)
                    new_replays += 1
                if tag == "verify":
                    verified[method] = data
            if args.preview:
                continue
            rows, trials, profile = runner.summarize(directory, case, methods, runs, checkpoints, profiles)
            files = {path.name: digest(path) for path in sorted(directory.glob("*.csv"))}
            write_json(complete, dict(case=case, methods=methods, replays=runs, files=files))
            print(f"AUDIT OK {dataset}: {len(case['batch_operations'])} exact checkpoints", flush=True)
            summary.extend(rows)
            details.extend(trials)
            diagnostics.extend(profile)
        if not args.preview:
            write_summary(output, summary)
            write_summary(output, details, filename="timing_replays.csv")
            if diagnostics:
                write_summary(output, diagnostics, filename="pruning_profile.csv")
            print(f"REPORT {output / 'summary.csv'}", flush=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, KeyError, TypeError, ValueError, RuntimeError, subprocess.CalledProcessError) as error:
        raise SystemExit(f"ERROR: {error}")
