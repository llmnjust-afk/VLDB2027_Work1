"""Run and audit matched whole-process replays for supplemental experiments."""

from pathlib import Path
import tempfile
from time import perf_counter

from compare_star_resident import audit_replay, run_logged
from revision_experiments import load_json, write_json


BATCH_ABLATIONS = {"full": "full", "full-certified": "certified", "full-fused": "fused"}


def run_replay(directory, case, method, tag, binaries, env, *, extra_flags=()):
    prefix = directory / f"{method}.{tag}"
    if method in BATCH_ABLATIONS:
        command = [binaries["full"], "batch", "--ablate", BATCH_ABLATIONS[method]]
    elif method in binaries:
        command = [binaries[method]]
    else:
        command = [binaries["full"], method]
    threads = case.get("threads", 1)
    command += [
        "--graph", case["graph"], "--stream", case["stream"], "--dataset", case["dataset"],
        "--threads", str(threads), "--runs", "1", "--batch-csv", f"{prefix}.batches.csv",
    ]
    verify = tag == "verify"
    command += ["--check-every", "1"] if verify else ["--timing-only"]
    if tag == "profile":
        command += ["--phase-csv", f"{prefix}.phases.csv"]
    command.extend(extra_flags)
    wall_s = run_logged(command, Path(f"{prefix}.runs.csv"), Path(f"{prefix}.stderr"), env=env)
    counts = case["batch_operations"]
    trace, mean = audit_replay(directory, method, tag, counts, verify, threads=threads)
    print(f"{directory.name} {method} {tag}: {len(counts)} batches, {wall_s:.2f} s wall, "
          f"{mean:.3f} ms/batch inside timer", flush=True)
    return trace


def check_verified_traces(directory, verified, expected_traces):
    sizes = {tuple(final for _, _, final in trace) for trace in verified.values()}
    if len(sizes) != 1:
        raise ValueError(f"{directory}: batch-end edge counts disagree")
    if "full" in verified:
        for method in ("static-net", "full-certified", "full-fused", "star-resident", "luo-edge"):
            if method in verified and verified["full"] != verified[method]:
                raise ValueError(f"{directory}: Full and {method} disagree on net mutations")
    if "static" in verified and "msp" in verified and verified["static"] != verified["msp"]:
        raise ValueError(f"{directory}: Static and MSP disagree on ordered mutations")
    if expected_traces is not None and verified != expected_traces:
        raise ValueError(f"{directory}: verified mutations or edge counts disagree with prepared stream")


def audit_paired_case(directory, case, methods, runs, *, expected_traces=None):
    complete = load_json(directory / "complete.json")
    if (complete["case"], complete["methods"], complete["replays"]) != (case, list(methods), runs):
        raise ValueError(f"{directory}: completion record disagrees with configured inputs or repeats")
    counts = case["batch_operations"]
    threads = case.get("threads", 1)
    verified = {method: audit_replay(directory, method, "verify", counts, True, threads=threads)[0]
                for method in methods}
    check_verified_traces(directory, verified, expected_traces)
    for method in methods:
        for trial in range(runs):
            trace, _ = audit_replay(directory, method, f"timing.{trial}", counts, False, threads=threads)
            if trace != verified[method]:
                raise ValueError(f"{directory}: {method} timing trial {trial} diverged from verification")
    return verified


def run_paired_case(directory, case, methods, runs, binaries, env, *, expected_traces=None):
    success = directory / "complete.json"
    if success.exists():
        verified = audit_paired_case(directory, case, methods, runs, expected_traces=expected_traces)
        print(f"REUSED {directory.name}: {len(case['batch_operations'])} verified batches", flush=True)
        return verified
    if directory.exists():
        failed = directory.parent / "failed"
        failed.mkdir(exist_ok=True)
        archived = Path(tempfile.mkdtemp(prefix=f"{directory.name}.", dir=failed)) / directory.name
        directory.rename(archived)
        print(f"Archived incomplete attempt in {archived}", flush=True)
    directory.mkdir(parents=True)
    start = perf_counter()
    verified = {method: run_replay(directory, case, method, "verify", binaries, env) for method in methods}
    check_verified_traces(directory, verified, expected_traces)
    for trial in range(runs):
        offset = trial % len(methods)
        order = methods[offset:] + methods[:offset]
        for method in order:
            trace = run_replay(directory, case, method, f"timing.{trial}", binaries, env)
            if trace != verified[method]:
                raise ValueError(f"{directory}: {method} timing trial {trial} diverged from verification")
    wall = perf_counter() - start
    write_json(success, {"case": case, "methods": list(methods), "replays": runs, "wall_s": wall})
    print(f"VERIFIED {directory.name}: {len(case['batch_operations'])} batches, "
          f"{sum(case['batch_operations'])} operations, {runs} headline replays per method, "
          f"{wall:.1f} s wall", flush=True)
    return verified
