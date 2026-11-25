#!/usr/bin/env python3
"""Generate manuscript tables and vector plots from completed replays."""

import argparse
from math import isclose, isfinite
from pathlib import Path
from statistics import mean, median, stdev

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.lines import Line2D
from matplotlib.ticker import NullLocator

from compare_replay import csv_rows
from compare_star_resident import METHODS as RESIDENT_METHODS, SCOPE, read_case
from paper_plot_style import AMAZON, COPPER, FULL, MUTED, PAPER_RCPARAMS, WIKIVOTE
from revision_experiments import GENERATED, LATENCY_CLOCK, LATENCY_SCOPE, load_json
from revision_report import (
    LABELS,
    METHODS,
    audit,
    main_table,
    star_results,
    summarize,
    thread_speedups,
    write_csv,
)
from summarize_replay import verify as verify_replay


ROOT = Path(__file__).resolve().parents[1]
GROUPS = ("main", "batch-size", "threads", "ablation-full", "ablation-full-fresh", "ablation-prefix-5")
DATASET_STYLE = {"amazon": (AMAZON, "o"), "wikivote": (WIKIVOTE, "s")}
PLOT_METHODS = dict(METHODS, full="Regional")
FULL_ABLATION = {"email": "ablation-full-fresh", "amazon": "ablation-full"}
ARCHIVED_PARALLEL = ROOT / ".build/vldb2027-scale-t4.g3OKhs"
PARALLEL_METHODS = ("full", "static", "msp")


def save_pdf(fig, path):
    fig.savefig(path, metadata={"CreationDate": None})
    plt.close(fig)


def plot_batch_size(rows, out):
    fig, axes = plt.subplots(1, 2, figsize=(3.30, 1.43))
    fig.subplots_adjust(left=0.16, right=0.98, bottom=0.30, top=0.82, wspace=0.56)
    sizes = sorted({row["nominal_batch_size"] for row in rows})
    metrics = (
        ("events_per_s", "(a) Events/s"),
        ("mean_ms", "(b) ms/batch"),
    )
    for axis, (metric, ylabel) in zip(axes, metrics):
        for dataset, (color, marker) in DATASET_STYLE.items():
            for method, linestyle in (("full", "-"), ("msp", "--")):
                points = sorted(
                    (row for row in rows if (row["dataset"], row["method"]) == (dataset, method)),
                    key=lambda row: row["nominal_batch_size"],
                )
                axis.errorbar(
                    [row["nominal_batch_size"] for row in points],
                    [row[metric] for row in points],
                    yerr=[row[f"{metric}_sd"] for row in points],
                    color=color, marker=marker, linestyle=linestyle, linewidth=1.1,
                    markersize=3.2, markeredgewidth=0.8,
                    markerfacecolor=color if method == "full" else "white",
                    elinewidth=0.7, capsize=1.7, capthick=0.7,
                )
        axis.set(xscale="log", yscale="log", ylabel=ylabel)
        axis.set_xticks(sizes, ["1k" if size == 1000 else str(size) for size in sizes])
        axis.yaxis.labelpad = 2
        axis.minorticks_off()
    fig.supxlabel("Events per batch", y=0.045, fontsize=PAPER_RCPARAMS["axes.labelsize"])
    dataset_handles = [Line2D([], [], label=LABELS[dataset], marker=marker, color=color,
                              linestyle="none", markersize=3.4)
                       for dataset, (color, marker) in DATASET_STYLE.items()]
    method_handles = [Line2D([], [], label=PLOT_METHODS[method], color=MUTED, linewidth=1.1,
                             linestyle=linestyle)
                      for method, linestyle in (("full", "-"), ("msp", "--"))]
    fig.legend(handles=dataset_handles + method_handles, loc="upper center", ncols=4,
               handlelength=1.5, handletextpad=0.35, columnspacing=1.0, borderaxespad=0.25)
    save_pdf(fig, out / "revision_batch_size.pdf")


def plot_thread_scaling(ratios, out):
    fig, axis = plt.subplots(figsize=(3.30, 1.28))
    fig.subplots_adjust(left=0.16, right=0.98, bottom=0.31, top=0.80)
    axis.axhline(1.0, linewidth=0.65, color=MUTED, linestyle=":")
    for (dataset, (color, marker)), linestyle in zip(DATASET_STYLE.items(), ("-", "--")):
        points = [row for row in ratios if row["dataset"] == dataset]
        lower = [max(0, row["speedup"] - row["bootstrap_lo"]) for row in points]
        upper = [max(0, row["bootstrap_hi"] - row["speedup"]) for row in points]
        axis.errorbar(
            [row["threads"] for row in points], [row["speedup"] for row in points],
            yerr=[lower, upper], color=color, marker=marker, linestyle=linestyle,
            linewidth=1.15, markersize=3.6, elinewidth=0.7, capsize=1.8, capthick=0.7,
            label=LABELS[dataset],
        )
    ticks = sorted({row["threads"] for row in ratios})
    axis.set_xscale("log", base=2)
    axis.set_xticks(ticks, [str(tick) for tick in ticks])
    axis.xaxis.set_minor_locator(NullLocator())
    axis.set_yticks([1.0, 1.2, 1.4, 1.6])
    axis.set(xlabel="Threads", ylabel="Speedup (×)")
    axis.yaxis.labelpad = 2
    fig.legend(loc="upper center", ncols=2, handlelength=1.65, handletextpad=0.4,
               columnspacing=1.0, borderaxespad=0.25)
    save_pdf(fig, out / "revision_thread_scaling.pdf")


def plot_full_ablation(rows, out):
    indexed = {(row["dataset"], row["method"]): row for row in rows}
    fig, axes = plt.subplots(1, 2, figsize=(3.30, 1.37))
    fig.subplots_adjust(left=0.16, right=0.98, bottom=0.20, top=0.80, wspace=0.56)
    metrics = (
        ("peels", "(a) Peels/replay"),
        ("mean_ms", "(b) ms/batch"),
    )
    for axis, (metric, ylabel) in zip(axes, metrics):
        bounds = []
        for i, (method, color, edge, hatch) in enumerate((
            ("full", FULL, FULL, ""), ("nomerge", mpl.colors.to_rgba(COPPER, 0.22), COPPER, "//"),
        )):
            for j, dataset in enumerate(FULL_ABLATION):
                row = indexed[dataset, method]
                x = j + (i - 0.5) * 0.34
                value, sd = row[metric], row[f"{metric}_sd"]
                axis.bar(
                    x, value, width=0.3, yerr=sd, label=PLOT_METHODS[method] if j == 0 else None,
                    color=color, hatch=hatch, linewidth=0.55, edgecolor=edge, capsize=1.7,
                    error_kw={"elinewidth": 0.7, "capthick": 0.7},
                )
                bounds.append((value - sd if sd < value else value, value + sd))
        axis.set(yscale="log", ylabel=ylabel)
        axis.set_xticks(range(len(FULL_ABLATION)), [LABELS[dataset] for dataset in FULL_ABLATION])
        axis.yaxis.labelpad = 2
        axis.minorticks_off()
        axis.set_ylim(min(low for low, _ in bounds) / 1.5, max(high for _, high in bounds) * 1.7)
    handles, labels = axes[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc="upper center", ncols=2, handlelength=1.45, handletextpad=0.45,
               columnspacing=1.1, borderaxespad=0.25)
    save_pdf(fig, out / "revision_merge_ablation.pdf")


def resident_star_results(run, main_specs):
    protocol = load_json(run / "protocol.json")
    cases = {case["dataset"]: case for case in protocol["cases"]}
    if (protocol["test_mode"] or protocol["threads"] != 1 or protocol["replays"] != 3
            or protocol["timer_scope"] != SCOPE
            or len(protocol["cases"]) != len(GENERATED) or set(cases) != set(GENERATED)):
        raise ValueError("expected three matched one-thread, full-stream Star replays")

    results = []
    for dataset in GENERATED:
        case = cases[dataset]
        directory = run / dataset
        complete = load_json(directory / "complete.json")
        inputs = [case["graph_sha256"], case["stream_sha256"]]
        spec = main_specs[dataset]
        if (case["scope"] != "full" or inputs != spec["inputs"] or case["batch_operations"] != spec["counts"]
                or complete["case"] != case or complete["replays"] != protocol["replays"]):
            raise ValueError(f"{directory}: expected complete results for the main generated stream")

        rows = read_case(directory, case, protocol["replays"])
        verified = csv_rows(directory / "full.verify.batches.csv")
        closure_edges = mean(int(row["region_edges"]) for row in verified)
        timings = {}
        for method in RESIDENT_METHODS:
            trials = [csv_rows(directory / f"{method}.timing.{i}.batches.csv")
                      for i in range(protocol["replays"])]
            timings[method] = [median(float(row["elapsed_us"]) for row in samples)
                               for samples in zip(*trials)]
        full, star = timings["full"], timings["star-resident"]
        wins = {"full": sum(a < b for a, b in zip(full, star)),
                "star-resident": sum(a > b for a, b in zip(full, star))}
        results.extend(dict(row, faster_batch_medians=wins[row["method"]],
                            mean_full_closure_edges=round(closure_edges, 3), source=str(directory))
                       for row in rows)
    return results


def star_table(rows, resident, out):
    indexed = {(row["dataset"], row["timing_boundary"]): row for row in rows}
    resident = {(row["dataset"], row["method"]): row for row in resident}
    tex = [
        r"% Generated by scripts/generate_revision_paper.py",
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{One-thread Star comparisons on generated streams. Mean latencies are in ms per batch. Ratios use the matched Regional time.}",
        r"\label{tab:star-results}",
        r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}lrrr@{}}",
        r"\toprule",
        r"\multicolumn{4}{@{}l}{\emph{Resident Star port on QEMU VM}} \\",
        r"Graph & Regional & Star resident & Star/Regional \\",
        r"\midrule",
    ]
    for dataset in GENERATED:
        full = resident[dataset, "full"]["mean_ms_per_batch"]
        star = resident[dataset, "star-resident"]["mean_ms_per_batch"]
        full_cell, star_cell = f"{full:.2f}", f"{star:.2f}"
        if full < star:
            full_cell = r"\textbf{" + full_cell + "}"
        elif star < full:
            star_cell = r"\textbf{" + star_cell + "}"
        ratio = resident[dataset, "star-resident"]["latency_over_full"]
        tex.append(f"{LABELS[dataset]} & {full_cell} & {star_cell} & "
                   + "$" + f"{ratio:.3f}" + r"\times$ \\")
    tex.extend([
        r"\midrule",
        r"\multicolumn{4}{@{}l}{\emph{Released CLI on Xeon VM}} \\",
        r"Graph & Regional & Star kernel & Star CLI \\",
        r"\midrule",
    ])
    for dataset in GENERATED:
        kernel = indexed[dataset, "upstream update kernel"]
        cli = indexed[dataset, "CLI adapter end-to-end"]
        full = float(cli["full_ms"])
        tex.append(f"{LABELS[dataset]} & {full:.2f} & {float(kernel['star_ms']):.2f} & "
                   f"{float(cli['star_ms']):.2f}" + r" \\")
    tex.extend([
        r"\midrule",
        r"\multicolumn{4}{@{}l}{\emph{In-process RAM bridge on QEMU VM}} \\",
        r"Graph & Regional & Star bridge & Bridge/Regional \\",
        r"\midrule",
    ])
    for dataset in GENERATED:
        bridge = indexed[dataset, "RAM bridge including state transfer"]
        full, star = float(bridge["full_ms"]), float(bridge["star_ms"])
        tex.append(f"{LABELS[dataset]} & {full:.2f} & {star:.2f} & "
                   + "$" + f"{star / full:.2f}" + r"\times$ \\")
    tex.extend([r"\bottomrule", r"\end{tabular*}", r"\end{table}", ""])
    (out / "tab_star_results.tex").write_text("\n".join(tex))


def archived_parallel_rows(run, main_specs):
    results, environments = [], set()
    for dataset in GENERATED:
        directory = run / f"{dataset}-t4"
        summary = verify_replay(directory)
        metadata = load_json(directory / "manifest.json")
        inputs = list(metadata["inputs"].values())
        hardware = (directory / "hardware.txt").read_text().splitlines()
        cpu = next((line.partition(":")[2].strip() for line in hardware if line.startswith("Model name:")), "")
        if (metadata["dataset"] != dataset or len(inputs) != 3 or
                inputs[:2] != main_specs[dataset]["inputs"] or metadata["methods"] != list(PARALLEL_METHODS) or
                (metadata["threads_requested"], metadata["runs"], metadata["batch_count"],
                 metadata["operations"]) != (4, 3, 200, 200000) or
                metadata["clock"] != LATENCY_CLOCK or metadata["build"] != LATENCY_SCOPE or
                cpu != "Intel(R) Xeon(R) Gold 6426Y"):
            raise ValueError(f"{directory}: incompatible archived four-thread protocol")
        environments.add((cpu, metadata["platform"], tuple(metadata["affinity"]), inputs[-1]))
        checked = csv_rows(directory / "full.verify.batches.csv")
        if [int(row["raw_ops"]) for row in checked] != main_specs[dataset]["counts"]:
            raise ValueError(f"{directory}: archived batch boundaries differ")
        for row in summary:
            method = row["method"]
            timings = csv_rows(directory / f"{method}.timing.batches.csv")
            recorded_runs = csv_rows(directory / f"{method}.timing.runs.csv")
            if ([int(item["run"]) for item in recorded_runs] != [0, 1, 2] or
                    any(item["threads"] != "4" for item in recorded_runs) or row["threads"] != "4"):
                raise ValueError(f"{directory}: unexpected replay IDs or thread budget for {method}")
            samples = [float(item["elapsed_us"]) / 1000 for item in timings]
            if any(not isfinite(value) or value <= 0 for value in samples):
                raise ValueError(f"{directory}: invalid archived batch times for {method}")
            latency = mean(samples)
            if not isclose(latency, float(row["mean_ms"]), rel_tol=1e-10):
                raise ValueError(f"{directory}: archived latency summary differs for {method}")
            replay_means = [mean(samples[start:start + 200]) for start in range(0, len(samples), 200)]
            results.append(dict(dataset=dataset, method=method, cpu_model=cpu, threads=4, replays=3,
                                batches=200, operations=200000, mean_ms_per_batch=latency,
                                replay_mean_sd_ms=stdev(replay_means), graph_sha256=inputs[0],
                                stream_sha256=inputs[1], binary_sha256=inputs[2], source=str(directory)))
    if len(environments) != 1:
        raise ValueError(f"{run}: incompatible archived machines or binaries")
    return results


def archived_parallel_table(rows, out):
    timings = {(row["dataset"], row["method"]): row["mean_ms_per_batch"] for row in rows}
    lines = [
        r"% Generated by scripts/generate_revision_paper.py",
        r"\begin{table}[t]",
        r"\centering",
        r"\caption{Matched four-thread replays on a Xeon Gold 6426Y VM.",
        r"Mean latency in ms per batch over three timing replays of each",
        r"complete generated stream. Bold marks the lowest mean.}",
        r"\label{tab:archived-parallel}",
        r"\begin{tabular*}{\columnwidth}{@{\extracolsep{\fill}}lrrr@{}}",
        r"\toprule",
        r"Graph & Regional & Static & MSP \\",
        r"\midrule",
    ]
    for dataset in GENERATED:
        values = [timings[dataset, method] for method in PARALLEL_METHODS]
        cells = [r"\textbf{" + f"{value:.2f}" + "}" if value == min(values) else f"{value:.2f}"
                 for value in values]
        lines.append(" & ".join([LABELS[dataset], *cells]) + r" \\")
    lines.extend([r"\bottomrule", r"\end{tabular*}", r"\end{table}", ""])
    (out / "tab_archived_parallel.tex").write_text("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / ".build/vldb2027-revision-phase1")
    parser.add_argument("--star-resident-run", type=Path, default=ROOT / ".build/vldb2027-star-resident")
    parser.add_argument("--archived-parallel-run", type=Path,
                        help="four-thread replay archive (uses the local archive if present)")
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    args = parser.parse_args()
    run = args.run.resolve(strict=True)
    status, valid = audit(run, GROUPS)
    failures = [f"{row['group']}/{row['configuration']}" for row in status if row["status"] != "VERIFIED"]
    if failures:
        parser.error(f"result audit failed for {', '.join(failures)}")
    if not all((run / "star-archives" / scope).is_dir() for scope in ("cli", "ram")):
        parser.error("expected both phase-one Star archives in the audited run")
    replays, summary = summarize(valid)
    ablation = [row for row in summary if FULL_ABLATION.get(row["dataset"]) == row["group"]]
    if len(ablation) != 4 or any(row["timing_replays"] != 3 for row in ablation):
        parser.error("expected three complete timing replays per method on Email and Amazon")
    main_specs = {name: spec for group, name, _, spec in valid if group == "main"}
    parallel_archive = args.archived_parallel_run or (ARCHIVED_PARALLEL if ARCHIVED_PARALLEL.is_dir() else None)
    try:
        resident = resident_star_results(args.star_resident_run.resolve(strict=True), main_specs)
        archived_parallel = (archived_parallel_rows(parallel_archive.resolve(strict=True), main_specs)
                             if parallel_archive else [])
    except (KeyError, OSError, ValueError) as error:
        parser.error(str(error))
    tables, figures = args.paper / "tables", args.paper / "figures"
    tables.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    main_table(summary, tables, "main")
    star_results(run, tables)
    write_csv(tables / "star_resident_results.csv", resident)
    star_table(csv_rows(tables / "star_results.csv"), resident, tables)
    if archived_parallel:
        write_csv(tables / "archived_parallel_results.csv", archived_parallel)
        archived_parallel_table(archived_parallel, tables)
    paper_rows = [row for row in summary if row["group"] in ("main", "batch-size", "threads", "ablation-prefix-5")]
    write_csv(tables / "revision_results.csv", paper_rows + ablation)
    ratios = thread_speedups(replays)
    write_csv(tables / "thread_speedup.csv", ratios)
    with mpl.rc_context(PAPER_RCPARAMS):
        plot_batch_size([row for row in summary if row["group"] == "batch-size"], figures)
        plot_thread_scaling(ratios, figures)
        plot_full_ablation(ablation, figures)


if __name__ == "__main__":
    main()
