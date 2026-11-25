#!/usr/bin/env python3
"""Audit the matched replays and generate the manuscript's main results table."""

import argparse
import fcntl
from contextlib import ExitStack
from pathlib import Path

from compare_replay import csv_rows, digest
from revision_experiments import BUILD, ROOT, load_json
from run_adaptive_comparison import operation_digest
from run_unified_dynamic import (
    METHODS, POLICY, SCOPE, binary_paths, validate_case,
)


METHOD_ORDER = (
    "static", "static-net", "full", "full-fused", "full-fused-pruned", "adaptive",
    "star-resident", "luo-edge",
)
GROUPS = (
    ("Generated updates", (("amazon", "Amazon"),)),
    ("Timestamp replays, daily checkpoints", (
        ("askubuntu-7d", "Ask Ubuntu (7d)"),
        ("askubuntu-365d", "Ask Ubuntu (365d)"),
        ("stackoverflow-28d", "Stack Overflow (7d)"),
    )),
    ("Shared 112-day Stack Overflow replay, 7-day retention", (
        ("stackoverflow-b1d", "Daily checkpoints"),
        ("stackoverflow-b14d", "14-day checkpoints"),
    )),
    ("Stack Overflow stress tests, 90-day retention, different replay intervals", (
        ("stackoverflow-90d-b14d", "14-day checkpoints"),
        ("stackoverflow-90d-b112d", "112-day checkpoints"),
    )),
)
DAYS = {
    "askubuntu-7d": (7, 1, 365),
    "askubuntu-365d": (365, 1, 28),
    "stackoverflow-28d": (7, 1, 28),
    "stackoverflow-b1d": (7, 1, 112),
    "stackoverflow-b14d": (7, 14, 112),
    "stackoverflow-90d-b14d": (90, 14, 28),
    "stackoverflow-90d-b112d": (90, 112, 224),
}
STRESS = "stackoverflow-90d-b112d"
REPORTS = ("summary.csv", "timing_replays.csv", "pruning_profile.csv")


def audit_cases(plan):
    cases = {case["dataset"]: case for case in plan["cases"]}
    if len(cases) != len(plan["cases"]):
        raise ValueError("duplicate workloads in the comparison protocol")
    for name, case in cases.items():
        counts = case["batch_operations"]
        if name == "amazon":
            if case["scope"] != "full" or counts != [1000] * 200:
                raise ValueError("Amazon must use the complete generated stream")
            continue
        retention, cadence, interval = DAYS[name]
        if (case["scope"] != "temporal" or case["window_days"] != retention
                or case["checkpoint_seconds"] != cadence * 86400
                or case["end_utc"] - case["start_utc"] != interval * 86400
                or len(counts) != interval // cadence):
            raise ValueError(f"{name}: replay does not match its retention or checkpoint cadence")
    if "stackoverflow-b1d" in cases:
        daily, fortnight = (cases[name] for name in ("stackoverflow-b1d", "stackoverflow-b14d"))
        if (any(daily[field] != fortnight[field] for field in ("graph_sha256", "start_utc", "end_utc"))
                or operation_digest(Path(daily["stream"])) != operation_digest(Path(fortnight["stream"]))):
            raise ValueError("checkpoint schedules must share an initial graph and ordered stream")
    return cases


def audit_run(output, expected_cases):
    plan = load_json(output / "protocol.json")
    methods = plan["methods"]
    runs = plan["timing_replays"]
    binaries = binary_paths(output)
    machine = plan["machine"]
    if (tuple(methods) != (*METHODS, "full-fused-pruned") or runs != 3
            or plan["threads"] != 1 or plan["cpu"] not in machine["affinity"]
            or "QEMU" not in machine["cpu_model"]
            or plan["timer_scope"] != SCOPE or plan["adaptive_policy"] != POLICY
            or not plan["profile_pruned"] or plan["smoke_only"]
            or {method: digest(path) for method, path in binaries.items()} != plan["binary_sha256"]):
        raise ValueError(f"{output}: expected the matched full-replay protocol and executables")
    cases = audit_cases(plan)
    if cases.keys() != expected_cases:
        raise ValueError(f"{output}: expected workloads {', '.join(sorted(expected_cases))}")

    by_case, reports = {}, {name: [] for name in REPORTS}
    for name, case in cases.items():
        result = validate_case(output, case, methods, runs, binaries, None, True, True)
        by_case[name] = {row["method"]: row for row in result[0]}
        for report, rows in zip(reports.values(), result, strict=True):
            report.extend({key: str(value) for key, value in row.items()} for row in rows)
    for name, expected in reports.items():
        if csv_rows(output / name) != expected:
            raise ValueError(f"{output / name}: differs from the audited logs")
    return plan, by_case


def audit(unified, stress):
    main_cases = {name for _, workloads in GROUPS for name, _ in workloads} - {STRESS}
    with ExitStack() as stack:
        for output in (unified, stress):
            lock = stack.enter_context((output / ".lock").open("rb"))
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        main_plan, main_rows = audit_run(unified, main_cases)
        stress_plan, stress_rows = audit_run(stress, {STRESS})
        fields = ("machine", "cpu", "flags", "compiler", "sources", "star_root")
        if any(main_plan[field] != stress_plan[field] for field in fields):
            raise ValueError("ordinary and stress runs differ in host, CPU or build configuration")
        if any(main_plan["binary_sha256"][name] != stress_plan["binary_sha256"][name]
               for name in ("full", "luo-edge")):
            raise ValueError("ordinary and stress runs differ in the Regional or Luo-Edge executable")
        # Star embeds absolute generated-source filenames in assertion messages.
        def star_files(output):
            source = output / "build/star-source"
            files = {path.relative_to(source): digest(path) for path in source.rglob("*") if path.is_file()}
            if not files:
                raise ValueError(f"{source}: expected the archived Star build inputs")
            return files

        if star_files(unified) != star_files(stress):
            raise ValueError("ordinary and stress runs differ in the generated Star sources")
    return main_rows | stress_rows


def table_row(label, rows):
    regional = rows["full"]
    coverage = f"{regional['regional_mean_coverage']:.2%}".replace("%", r"\%")
    cells = [label, f"{regional['batches']:,}", f"{regional['operations']:,}", coverage]
    means = [rows[method]["mean_ms_per_batch"] for method in METHOD_ORDER]
    best = min(means)
    for value in means:
        number = f"{value:,.2f}" if value >= 10 else f"{value:.3f}"
        cells.append(rf"\textbf{{{number}}}" if value == best else number)
    return " & ".join(cells) + r" \\"


def render(workloads):
    lines = [
        "% Generated by scripts/generate_main3_paper.py",
        "% Sources: .build/vldb2027-main3-unified and .build/vldb2027-main3-large-churn",
        r"\begin{table*}[t]",
        r"\centering",
        r"\caption{Mean batch latency (ms) from update application to exact labels,",
        r"with three complete replays per method on one pinned logical CPU.",
        r"Coverage is the mean share of live edges in the Regional closure.",
        r"Bold marks each row minimum. The 112-day stress cadence uses a separate",
        r"Star build from the same sources.}",
        r"\label{tab:main-results}",
        r"\small",
        r"\setlength{\tabcolsep}{1.8pt}",
        r"\begin{tabular*}{\textwidth}{@{\extracolsep{\fill}}lrrrrrrrrrrr@{}}",
        r"\toprule",
        r" & & & & \multicolumn{2}{c}{Full-graph peeling} & \multicolumn{4}{c}{Regional and hybrid}"
        r" & \multicolumn{2}{c}{Resident dynamic} \\",
        r"\cmidrule(lr){5-6}\cmidrule(lr){7-10}\cmidrule(l){11-12}",
        r"Workload & Batches & Events & Coverage & Static & Static-Net & Regional & Fused & Fused-Pruned"
        r" & Adaptive & Star & Luo-Edge \\",
    ]
    for heading, members in GROUPS:
        lines.extend((r"\midrule", rf"\multicolumn{{12}}{{@{{}}l}}{{\emph{{{heading}}}}} \\"))
        lines.extend(table_row(label, workloads[key]) for key, label in members)
    return "\n".join(lines + [r"\bottomrule", r"\end{tabular*}", r"\end{table*}", ""])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unified", type=Path, default=BUILD / "vldb2027-main3-unified")
    parser.add_argument("--stress", type=Path, default=BUILD / "vldb2027-main3-large-churn")
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    args = parser.parse_args()
    table = render(audit(args.unified, args.stress))
    path = args.paper / "tables/tab_main3_unified.tex"
    path.write_text(table)
    print(f"Wrote {path}")


if __name__ == "__main__":
    main()
