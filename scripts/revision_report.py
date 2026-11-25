#!/usr/bin/env python3
"""Audit a revision run and export CSV, a two-column LaTeX table, and vector plots."""

import argparse
import csv
import math
from pathlib import Path
import random
import shutil
import statistics
import sys

from compare_replay import check_rows, csv_rows
from revision_experiments import (BUILD, CORE, GENERATED, TEMPORAL, check_config, load_json)
from summarize_replay import verify

LABELS = {"email": "Email", "wikivote": "WikiVote", "amazon": "Amazon",
          "email-temporal": "Email interactions", "askubuntu": "Ask Ubuntu"}
METHODS = {"full": "Full", "static": "Static", "msp": "MSP", "nomerge": "No-merge"}
DEFAULT_GROUPS = ("main", "batch-size", "threads", "ablation-full")
COLORS = ("#205a92", "#a64127", "#48734b", "#775388")


def write_csv(path, rows):
    if rows:
        with path.open("w", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)


def expected_names(run, group):
    if group in ("main", "main-fresh"):
        return set(LABELS)
    if group == "batch-size":
        return {f"{dataset}-b{size}" for dataset in ("amazon", "wikivote")
                for size in (50, 100, 250, 500, 1000)}
    if group == "threads":
        grid = run / "threads-grid.json"
        sizes = load_json(grid)["threads"] if grid.is_file() else (1, 2, 4, 8)
        return {f"{dataset}-t{size}" for dataset in ("amazon", "wikivote") for size in sizes}
    if group in ("pilot", "ablation-full") or group.startswith("ablation-prefix-"):
        return {"email", "amazon"}
    if group == "smoke":
        return {"k4"}
    return set()


def audit(run, groups):
    status, valid = [], []
    for group in groups:
        plans = {path.stem: path for path in (run / "plan" / group).glob("*.json")}
        names = expected_names(run, group) | plans.keys()
        if not names:
            names = {"(no configurations scheduled)"}
        sweep_identity = set()
        for name in sorted(names):
            directory = run / group / name
            state = "MISSING"
            detail = "no run plan"
            if name in plans:
                if (directory / "complete").is_file():
                    try:
                        spec = load_json(plans[name])
                        meta = check_config(directory, spec)
                        if group in ("batch-size", "threads"):
                            environment = spec.get("environment", {})
                            scope = (tuple(meta["inputs"].values())[-1], meta["platform"],
                                     tuple(meta["affinity"]), repr(environment))
                            sweep_identity.add(scope)
                        valid.append((group, name, directory, spec))
                        state, detail = "VERIFIED", spec.get("archive", "new run")
                    except (OSError, ValueError, KeyError) as error:
                        state, detail = "FAILED", str(error)
                elif directory.exists() or (run / group / f"{name}.log").is_file():
                    state, detail = "INCOMPLETE", str(run / group / f"{name}.log")
                else:
                    detail = "planned replay has no output"
            status.append(dict(group=group, configuration=name, status=state, detail=detail))
        if len(sweep_identity) > 1:
            status.append(dict(group=group, configuration="sweep environment", status="FAILED",
                               detail="binary, host, affinity or runtime differ within sweep"))
            valid = [row for row in valid if row[0] != group]
        attempts = len(list((run / group / "_attempts").glob("*")))
        if attempts:
            print(f"RETAINED {group}: {attempts} prior attempts", file=sys.stderr)
    return status, valid


def summarize(valid):
    replays, summary = [], []
    for group, name, directory, spec in valid:
        for method in spec["methods"]:
            verified = csv_rows(directory / f"{method}.verify.batches.csv")
            timing = csv_rows(directory / f"{method}.timing.batches.csv")
            count = len(verified)
            total = sum(int(row["raw_ops"]) for row in verified)
            run_summaries = csv_rows(directory / f"{method}.timing.runs.csv")
            provenance = dict(group=group, configuration=name, dataset=spec["dataset"], method=method,
                              threads=spec["threads"] if method != "peredge" else 1,
                              nominal_batch_size=spec["batch_size"] or "",
                              batches=count, raw_events=total,
                              source=str(directory), archive=spec.get("archive", ""))
            samples = []
            for repetition in range(spec["runs"]):
                rows = timing[repetition * count:(repetition + 1) * count]
                times = [float(row["elapsed_us"]) / 1000 for row in rows]
                if not all(math.isfinite(value) and value >= 0 for value in times) or not sum(times):
                    raise ValueError(f"invalid timing values in {directory} for {method}")
                coverage = (statistics.mean(int(row["region_edges"]) / int(row["final_m"])
                                            if int(row["final_m"]) else 0 for row in rows)
                            if method == "full" else "")
                peels = sum(int(row["peels"]) for row in rows)
                closure_sum = ""
                if method in ("full", "nomerge"):
                    # BatchStats counts each closure edge once in BFS and twice in peel_region.
                    closure_sum, remainder = divmod(int(run_summaries[repetition]["evals"]), 3)
                    if remainder or (method == "full" and closure_sum != sum(int(row["region_edges"]) for row in rows)):
                        raise ValueError(f"closure-work counter is inconsistent in {directory} for {method}")
                sample = dict(provenance, replay=repetition, mean_ms=statistics.mean(times),
                              events_per_s=total * 1000 / sum(times),
                              peels=peels, total_closure_edges=closure_sum,
                              mean_edges_per_peel=closure_sum / peels if closure_sum != "" and peels else "",
                              median_batch_max_closure=statistics.median(int(row["region_edges"]) for row in rows),
                              closure_fraction=coverage,
                              parallel_jobs=sum(int(row["parallel_jobs"]) for row in rows))
                replays.append(sample)
                samples.append(sample)
            aggregate = dict(provenance, timing_replays=len(samples))
            for metric in ("mean_ms", "events_per_s", "peels", "total_closure_edges",
                           "mean_edges_per_peel", "median_batch_max_closure", "closure_fraction",
                           "parallel_jobs"):
                values = [row[metric] for row in samples if row[metric] != ""]
                aggregate[metric] = statistics.mean(values) if values else ""
                aggregate[f"{metric}_sd"] = statistics.stdev(values) if len(values) > 1 else ""
            summary.append(aggregate)
    return replays, summary


def main_table(rows, out, group):
    entries = {(row["dataset"], row["method"]): row for row in rows if row["group"] == group}
    table_rows = []
    tex = [
        r"% Generated from validated replay logs by scripts/revision_report.py",
        r"\begin{table*}[t]", r"\centering",
        r"\caption{Single-thread batch latency and closure coverage on complete update streams. "
        r"Full is the proposed regional maintainer. Coverage is the mean fraction of "
        r"batch-end edges in Full's triangle closure. Speedup is Static latency divided by Full latency.}",
        r"\label{tab:main-results}",
        r"\begin{tabular*}{\textwidth}{@{\extracolsep{\fill}}lrrrrrrr@{}}",
        r"\toprule",
        r"& \multicolumn{3}{c}{Workload and region} & \multicolumn{3}{c}{Mean latency (ms/batch)} & \\",
        r"\cmidrule(lr){2-4}\cmidrule(lr){5-7}",
        r"Workload & Batches & Raw events & Closure & Full & Static & MSP & Speedup \\",
    ]
    suffixes = (" (Xeon Gold 6426Y, five replays)", " (QEMU CPU, three replays)") if group == "main" else ("", "")
    for group_index, (heading, names) in enumerate(
            (("Generated updates", GENERATED), ("Timestamp-driven replay", tuple(TEMPORAL)))):
        group_tex = []
        for name in names:
            if any((name, method) not in entries for method in ("full", "static", "msp")):
                continue
            full, static, msp = (entries[name, method] for method in ("full", "static", "msp"))
            latencies = [row["mean_ms"] for row in (full, static, msp)]
            speedup = static["mean_ms"] / full["mean_ms"]
            table_rows.append(dict(group=heading, dataset=name, batches=full["batches"],
                                   raw_events=full["raw_events"], closure_fraction=full["closure_fraction"],
                                   full_ms=latencies[0], static_ms=latencies[1], msp_ms=latencies[2],
                                   static_over_full=speedup, timing_replays=full["timing_replays"],
                                   source=full["source"], archive=full["archive"]))
            cells = [f"{value:.3f}" for value in latencies]
            for i, value in enumerate(latencies):
                if value == min(latencies):
                    cells[i] = r"\textbf{" + cells[i] + "}"
            group_tex.append(" & ".join([LABELS[name], f"{full['batches']:,}", f"{full['raw_events']:,}",
                                         f"{full['closure_fraction']:.1%}".replace("%", r"\%"),
                                         *cells, f"${speedup:.2f}\\times$"]) + r" \\")
        if group_tex:
            tex.extend([r"\midrule", r"\multicolumn{8}{@{}l}{\emph{" + heading +
                        suffixes[group_index] + r"}} \\", *group_tex])
    if table_rows:
        tex.extend([r"\bottomrule", r"\end{tabular*}", r"\end{table*}", ""])
        (out / "tab_main_results.tex").write_text("\n".join(tex))
        write_csv(out / "main_results.csv", table_rows)


def pilot_estimates(valid, out):
    estimates = []
    for group, name, directory, spec in valid:
        if group != "pilot":
            continue
        count = len(spec["counts"])
        for method in spec["methods"]:
            checked = csv_rows(directory / f"{method}.verify.runs.csv")[0]
            timed = csv_rows(directory / f"{method}.timing.runs.csv")[0]
            verify_s = sum(float(checked[key]) for key in ("build_s", "total_maint_s", "verify_s"))
            timing_s = sum(float(timed[key]) for key in ("build_s", "total_maint_s"))
            estimates.append(dict(dataset=name, method=method, pilot_batches=count,
                                  pilot_raw_events=sum(spec["counts"]), measured_verify_s=verify_s,
                                  measured_timing_s=timing_s,
                                  estimate_5_batches_1_verify_3_timing_s=(verify_s + 3 * timing_s) * 5 / count,
                                  estimate_200_batches_1_verify_3_timing_s=(verify_s + 3 * timing_s) * 200 / count,
                                  source=str(directory)))
    write_csv(out / "pilot_estimates.csv", estimates)


def star_results(run, out):
    sources = {
        "cli": BUILD / "vldb2027-star-full.ThglUI",
        "ram": BUILD / "vldb2027-star-ram-replay.xdQj6W",
    }
    archived = run / "star-archives"
    for scope, source in sources.items():
        target = archived / scope
        if not target.exists():
            shutil.copytree(source, target)
    rows = []
    for dataset in GENERATED:
        for scope in ("cli", "ram"):
            star_root = archived / scope
            native = run / "main" / dataset if scope == "cli" else star_root / f"{dataset}-full"
            if scope == "cli" and not native.exists():
                native = CORE / f"{dataset}-t1"
            verify(native)
            manifest = load_json(native / "manifest.json")
            prefix = "star." if scope == "cli" else ""
            star = star_root / (f"{dataset}-t1" if scope == "cli" else f"{dataset}-star-ram")
            if not (star / "complete").is_file():
                raise ValueError(f"incomplete Star archive: {star}")
            if scope == "cli":
                meta = load_json(star / "manifest.json")
                if (list(meta["inputs"].values())[:2] != list(manifest["inputs"].values())[:2] or
                        meta["affinity"] != manifest["affinity"] or
                        meta["threads"] != manifest["threads_requested"] or
                        (native / "hardware.txt").read_text() != (star / "hardware.txt").read_text()):
                    raise ValueError(f"CLI Star and Full settings differ: {dataset}")
                star_runs = meta["runs"]
            else:
                if ((native / "hardware.txt").read_text() != (star_root / "hardware.txt").read_text() or
                        list(manifest["inputs"].values())[-1] !=
                        (star_root / "binaries.sha256").read_text().split()[0]):
                    raise ValueError(f"RAM bridge and Full settings differ: {dataset}")
                star_runs = int(csv_rows(star / "summary.csv")[0]["runs"])
            full_rows = csv_rows(native / "full.verify.batches.csv")
            counts = [int(row["raw_ops"]) for row in full_rows]
            sizes = [row["final_m"] for row in full_rows]
            checked = csv_rows(star / f"{prefix}verify.batches.csv")
            timed = csv_rows(star / f"{prefix}timing.batches.csv")
            check_rows(checked, counts, 1, True)
            check_rows(timed, counts, star_runs, False)
            if [row["final_m"] for row in checked] != sizes or [row["final_m"] for row in timed] != sizes * star_runs:
                raise ValueError(f"Star graph-size trace differs from Full: {star}")
            full_timing = csv_rows(native / "full.timing.batches.csv")
            measures = (("upstream update kernel", "kernel_ms"), ("CLI adapter end-to-end", "elapsed_ms")) if scope == "cli" else (
                ("RAM bridge including state transfer", "elapsed_ms"),)
            for boundary, field in measures:
                rows.append(dict(dataset=dataset, timing_boundary=boundary, batches=len(counts),
                                 raw_events=sum(counts), full_replays=manifest["runs"], star_replays=star_runs,
                                 full_ms=statistics.mean(float(row["elapsed_us"]) / 1000 for row in full_timing),
                                 star_ms=statistics.mean(float(row[field]) for row in timed),
                                 full_source=str(native), star_source=str(star)))
    write_csv(out / "star_results.csv", rows)


def thread_speedups(replays):
    ratios = []
    rng = random.Random(2027)
    for dataset in ("amazon", "wikivote"):
        values = {}
        for row in replays:
            if row["group"] == "threads" and row["dataset"] == dataset:
                values.setdefault(row["threads"], []).append(row["mean_ms"])
        if 1 not in values:
            continue
        for threads, times in sorted(values.items()):
            center = statistics.mean(values[1]) / statistics.mean(times)
            lo = hi = center
            if threads != 1 and min(len(times), len(values[1])) > 1:
                samples = sorted(statistics.mean(rng.choices(values[1], k=len(values[1]))) /
                                 statistics.mean(rng.choices(times, k=len(times))) for _ in range(5000))
                lo, hi = samples[125], samples[4874]
            ratios.append(dict(dataset=dataset, method="full", threads=threads, speedup=center,
                               bootstrap_lo=lo, bootstrap_hi=hi, replays=len(times)))
    return ratios


def plot_results(rows, replays, out):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.ticker import NullFormatter

    plt.rcParams.update({"font.size": 8, "pdf.fonttype": 42, "axes.spines.top": False,
                         "axes.spines.right": False, "axes.grid": True, "axes.axisbelow": True,
                         "grid.alpha": 0.25})
    batch = [row for row in rows if row["group"] == "batch-size"]
    if batch:
        fig, axes = plt.subplots(1, 2, figsize=(7, 2.7), layout="constrained")
        for axis, metric, label in zip(axes, ("events_per_s", "mean_ms"), ("Throughput (events/s)", "Mean latency (ms/batch)")):
            for i, (dataset, method) in enumerate((d, m) for d in ("amazon", "wikivote") for m in ("full", "msp")):
                data = sorted((row for row in batch if (row["dataset"], row["method"]) == (dataset, method)),
                              key=lambda row: row["nominal_batch_size"])
                if data:
                    axis.errorbar([row["nominal_batch_size"] for row in data], [row[metric] for row in data],
                                  yerr=[row[f"{metric}_sd"] or 0 for row in data], capsize=2,
                                  label=f"{LABELS[dataset]} {METHODS[method]}", color=COLORS[i],
                                  marker=("o" if method == "full" else "s"), linestyle=("-" if method == "full" else "--"))
            axis.set(xlabel="Raw events per batch", ylabel=label, xscale="log", yscale="log")
            sizes = sorted({row["nominal_batch_size"] for row in batch})
            axis.set_xticks(sizes, [str(size) for size in sizes])
            axis.xaxis.set_minor_formatter(NullFormatter())
        axes[0].legend(fontsize=7, frameon=False, loc="best")
        fig.savefig(out / "batch_size.pdf")
        plt.close(fig)

    ratios = thread_speedups(replays)
    if ratios:
        fig, axis = plt.subplots(figsize=(3.35, 2.25), layout="constrained")
        for i, dataset in enumerate(("amazon", "wikivote")):
            points = [row for row in ratios if row["dataset"] == dataset]
            if not points:
                continue
            axis.errorbar([row["threads"] for row in points], [row["speedup"] for row in points],
                          yerr=([max(0, row["speedup"] - row["bootstrap_lo"]) for row in points],
                                [max(0, row["bootstrap_hi"] - row["speedup"]) for row in points]),
                          label=LABELS[dataset], color=COLORS[i], marker=("o" if i == 0 else "s"), capsize=2)
        ticks = sorted({row["threads"] for row in ratios})
        axis.axhline(1, linewidth=0.7, color="#737373")
        axis.set(xlabel="Threads", ylabel="Speedup over Full at 1 thread", xscale="log")
        axis.set_xticks(ticks, [str(value) for value in ticks])
        axis.xaxis.set_minor_formatter(NullFormatter())
        axis.legend(frameon=False)
        fig.savefig(out / "thread_scaling.pdf")
        plt.close(fig)
        write_csv(out / "thread_speedup.csv", ratios)

    for group in sorted({row["group"] for row in rows if row["group"].startswith("ablation-")}):
        ablation = {(row["dataset"], row["method"]): row for row in rows if row["group"] == group}
        names = [dataset for dataset in ("email", "amazon") if
                 all((dataset, method) in ablation for method in ("full", "nomerge"))]
        if not names:
            continue
        fig, axes = plt.subplots(1, 2, figsize=(7, 2.55), layout="constrained")
        for axis, metric, label in zip(axes, ("peels", "mean_ms"),
                                       ("Peels per replay", "Mean latency (ms/batch)")):
            for i, method in enumerate(("full", "nomerge")):
                for index, dataset in enumerate(names):
                    row = ablation[dataset, method]
                    axis.bar(index + (i - 0.5) * 0.35, row[metric], width=0.32,
                             yerr=row[f"{metric}_sd"] or None, capsize=3, color=COLORS[i],
                             hatch=("//" if i else ""), edgecolor="#303030", linewidth=0.35,
                             label=METHODS[method] if index == 0 else None)
            axis.set(ylabel=label, yscale="log")
            axis.set_xticks(range(len(names)), [LABELS[dataset] for dataset in names])
            axis.xaxis.grid(False)
        axes[0].legend(loc="upper center", bbox_to_anchor=(0.5, 1.19), ncol=2, frameon=False)
        fig.savefig(out / f"{group}.pdf")
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("action", choices=("check", "summarize"))
    parser.add_argument("--groups", nargs="+")
    parser.add_argument("--allow-incomplete", action="store_true")
    parser.add_argument("--star", action="store_true", help="export the existing paired Star archives")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    discovered = sorted(path.name for path in (run / "plan").glob("ablation-prefix-*") if path.is_dir())
    groups = args.groups or (*DEFAULT_GROUPS, *discovered)
    try:
        status, valid = audit(run, groups)
        out = run / "report"
        out.mkdir(exist_ok=True)
        write_csv(out / "status.csv", status)
        for row in status:
            print(f"{row['status']} {row['group']}/{row['configuration']} {row['detail']}")
        if any(row["status"] != "VERIFIED" for row in status) and not args.allow_incomplete:
            parser.exit(1, f"Configurations missing or invalid. See {out / 'status.csv'}\n")
        if args.action == "summarize":
            replays, rows = summarize(valid)
            write_csv(out / "timing_replays.csv", replays)
            write_csv(out / "summary.csv", rows)
            main_table(rows, out, "main-fresh" if "main-fresh" in groups else "main")
            pilot_estimates(valid, out)
            if not args.no_plots:
                plot_results(rows, replays, out)
            if args.star:
                star_results(run, out)
        print(f"REPORT {out}")
    except (OSError, ValueError, KeyError, ImportError) as error:
        parser.exit(1, f"{error}\n")


if __name__ == "__main__":
    main()
