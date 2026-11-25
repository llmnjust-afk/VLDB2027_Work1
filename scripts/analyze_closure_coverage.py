#!/usr/bin/env python3
"""Pair audited Full and Static batch timings and plot them against closure coverage."""

import argparse
from collections import defaultdict
from pathlib import Path
from statistics import median
import math

from compare_replay import csv_rows
from paper_plot_style import AMAZON, ASKUBUNTU, EMAIL, MUTED, PAPER_RCPARAMS, WIKIVOTE
from revision_report import LABELS, audit, write_csv


ROOT = Path(__file__).resolve().parents[1]
PANELS = (
    ("generated", "(a) Generated", ("amazon", "wikivote", "email")),
    ("timestamp", "(b) Timestamped", ("askubuntu", "email-temporal")),
)
STYLES = {
    "amazon": (AMAZON, "o"),
    "wikivote": (WIKIVOTE, "s"),
    "email": (EMAIL, "^"),
    "askubuntu": (ASKUBUNTU, "D"),
    "email-temporal": (EMAIL, "^"),
}


def timing_samples(directory, method):
    samples = defaultdict(list)
    for row in csv_rows(directory / f"{method}.timing.batches.csv"):
        latency = float(row["elapsed_us"]) / 1000
        if not math.isfinite(latency) or latency <= 0:
            raise ValueError(f"invalid timing sample for {method} in {directory}")
        samples[int(row["batch"])].append((latency, int(row["region_edges"])))
    return samples


def pair_batches(directory, group, dataset, replays):
    # The audit checks counts, checkpoint sizes, verification status and replay IDs.
    methods = ("full", "static")
    checked = [csv_rows(directory / f"{method}.verify.batches.csv") for method in methods]
    full, static = (timing_samples(directory, method) for method in methods)
    pairs = []
    for full_check, static_check in zip(*checked):
        batch = int(full_check["batch"])
        region, edges = int(full_check["region_edges"]), int(full_check["final_m"])
        if not 0 <= region <= edges or any(size != region for _, size in full[batch]):
            raise ValueError(f"inconsistent closure size at {directory} batch {batch}")
        full_ms, static_ms = (median(latency for latency, _ in samples[batch])
                              for samples in (full, static))
        full_changes, static_changes = (int(row["inserted"]) + int(row["deleted"])
                                        for row in (full_check, static_check))
        pairs.append(dict(stream_type=group, dataset=dataset, batch=batch,
                          raw_ops=int(full_check["raw_ops"]), region_edges=region, final_m=edges,
                          full_edge_changes=full_changes, static_edge_changes=static_changes,
                          closure_fraction=region / edges if edges else 0.0,
                          full_median_ms=full_ms, static_median_ms=static_ms,
                          static_over_full=static_ms / full_ms, timing_replays=replays,
                          source=str(directory)))
    return pairs


def report_quartiles(dataset, pairs):
    ranked = sorted(pairs, key=lambda row: (row["closure_fraction"], row["batch"]))
    split = [ranked[q * len(ranked) // 4:(q + 1) * len(ranked) // 4] for q in range(4)]
    ratios = [median(row["static_over_full"] for row in quartile) for quartile in split]
    wins = sum(row["static_over_full"] > 1 for row in ranked)
    summaries = ", ".join(f"{ratio:.2f}" for ratio in ratios)
    print(f"{dataset}: Full faster at {wins}/{len(ranked)} checkpoints, "
          f"median ratios by coverage quartile {summaries}")


def plot_pairs(data, path):
    import matplotlib as mpl
    mpl.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D

    ratios = [row["static_over_full"] for pairs in data.values() for row in pairs]
    powers = range(math.floor(math.log2(min(ratios))), math.ceil(math.log2(max(ratios))) + 1)
    ticks = [2 ** power for power in powers if power % 2 == 0]
    style = dict(PAPER_RCPARAMS, **{
        "axes.labelsize": 7.2,
        "axes.titlesize": 7.1,
        "xtick.labelsize": 6.7,
        "ytick.labelsize": 6.7,
        "legend.fontsize": 7.0,
    })
    with mpl.rc_context(style):
        fig, axes = plt.subplots(1, 2, figsize=(3.30, 1.42), sharey=True)
        fig.subplots_adjust(left=0.17, right=0.97, bottom=0.24, top=0.77, wspace=0.21)
        for axis, (_, title, datasets) in zip(axes, PANELS):
            axis.set_title(title, pad=2)
            axis.set_yscale("log", base=2)
            axis.set(xlim=(-1, 104), ylim=(2 ** powers[0], 2 ** powers[-1]))
            axis.set_xticks([0, 50, 100])
            axis.set_yticks(ticks, [format(tick, "g") for tick in ticks])
            axis.minorticks_off()
            axis.axhline(1, color=MUTED, linestyle=(0, (3, 2)), linewidth=0.7)
            for dataset in datasets:
                color, marker = STYLES[dataset]
                pairs = data[dataset]
                axis.scatter([100 * row["closure_fraction"] for row in pairs],
                             [row["static_over_full"] for row in pairs], s=5.5, alpha=0.53,
                             color=color, marker=marker, linewidths=0)
        handles = [Line2D([], [], color=color, marker=marker, linestyle="none", markersize=3.6,
                          label=LABELS[dataset])
                   for dataset, (color, marker) in STYLES.items() if dataset != "email-temporal"]
        fig.legend(handles=handles, loc="upper center", ncols=4, handlelength=0.7,
                   handletextpad=0.35, columnspacing=0.8, borderaxespad=0.24)
        axes[0].set_ylabel("Latency ratio\n(Static / Regional)", labelpad=2)
        fig.supxlabel("Closure coverage (%)", x=0.57, y=0.015, fontsize=style["axes.labelsize"])
        fig.savefig(path, metadata={"CreationDate": None})
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / ".build/vldb2027-revision-phase1")
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    args = parser.parse_args()
    try:
        status, valid = audit(args.run.resolve(strict=True), ("main",))
        failures = [row["configuration"] for row in status if row["status"] != "VERIFIED"]
        if failures:
            raise ValueError(f"main replay audit failed for {', '.join(failures)}")
        specs = {dataset: (directory, spec) for _, dataset, directory, spec in valid}
        data = {}
        for group, _, datasets in PANELS:
            for dataset in datasets:
                directory, spec = specs[dataset]
                if not {"full", "static"}.issubset(spec["methods"]) or spec["threads"] != 1:
                    raise ValueError(f"expected matched one-thread Full/Static logs for {dataset}")
                data[dataset] = pair_batches(directory, group, dataset, spec["runs"])
                report_quartiles(dataset, data[dataset])
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))

    for subdirectory in ("tables", "figures"):
        (args.paper / subdirectory).mkdir(parents=True, exist_ok=True)
    write_csv(args.paper / "tables/closure_coverage.csv",
              [row for pairs in data.values() for row in pairs])
    plot_pairs(data, args.paper / "figures/revision_closure_coverage.pdf")


if __name__ == "__main__":
    main()
