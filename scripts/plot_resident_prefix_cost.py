#!/usr/bin/env python3
"""Plot startup-inclusive timing ratios from the audited resident replays."""

import argparse
import csv
from math import isfinite
from pathlib import Path
from statistics import mean

import matplotlib as mpl
from paper_plot_style import AMAZON, EMAIL, MUTED, PAPER_RCPARAMS, WIKIVOTE

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import NullLocator


ROOT = Path(__file__).resolve().parents[1]
BATCHES = range(1, 201)
METHODS = ("full", "star-resident")
STYLES = {
    "amazon": ("Amazon", AMAZON, "-", "o"),
    "wikivote": ("WikiVote", WIKIVOTE, "-.", "s"),
    "email": ("Email", EMAIL, "--", "^"),
}


def read_ratios(source):
    times = {}
    with source.open(newline="") as stream:
        for row in csv.DictReader(stream):
            key = (row["dataset"], row["method"], int(row["replay"]), int(row["batches"]))
            value = float(row["cumulative_latency_s"])
            if key in times or not isfinite(value) or value <= 0:
                raise ValueError(f"Invalid cumulative latency for {key} in {source}")
            times[key] = value

    expected = {(dataset, method, replay, batch) for dataset in STYLES for method in METHODS
                for replay in range(3) for batch in BATCHES}
    if times.keys() != expected:
        raise ValueError(f"Expected 200 complete prefixes for each of three replays per method in {source}")
    ratios = {}
    for dataset in STYLES:
        totals = {method: [mean(times[dataset, method, replay, batch] for replay in range(3))
                           for batch in BATCHES] for method in METHODS}
        ratios[dataset] = [star / full for star, full in zip(totals["star-resident"], totals["full"])]
    return ratios


def plot_ratios(ratios, destination):
    style = dict(PAPER_RCPARAMS, **{
        "axes.labelsize": 7.2,
        "xtick.labelsize": 6.8,
        "ytick.labelsize": 6.8,
        "legend.fontsize": 7.0,
    })
    with mpl.rc_context(style):
        fig, ax = plt.subplots(figsize=(3.30, 1.29))
        fig.subplots_adjust(left=0.135, right=0.963, bottom=0.25, top=0.86)
        ax.axhline(1, color=MUTED, linestyle=(0, (2.5, 2)), linewidth=0.7)
        for dataset, (label, color, line, marker) in STYLES.items():
            values = ratios[dataset]
            ax.plot(BATCHES, values, color=color, linestyle=line, linewidth=1.3, label=label)
            first_below = next((batch for batch, value in zip(BATCHES, values) if value < 1), None)
            if first_below is not None:
                value = values[first_below - 1]
                ax.plot(first_below, value, marker=marker, markerfacecolor="white",
                        markeredgewidth=0.95, color=color, markersize=3.8)
                ax.annotate(str(first_below), (first_below, value), xytext=(3, 3),
                            textcoords="offset points", color=color, fontsize=6.8)

        ax.set(xscale="log", xlim=(0.94, 215), ylim=(0, 3.63),
               xlabel="Batches replayed", ylabel="Cumulative ratio\n(Star / Regional)")
        ax.xaxis.labelpad = 1.5
        ax.yaxis.labelpad = 2
        ax.set_xticks([1, 5, 20, 50, 200], ["1", "5", "20", "50", "200"])
        ax.xaxis.set_minor_locator(NullLocator())
        ax.set_yticks([0, 1, 2, 3])
        ax.annotate("Equal time", (200, 1), xytext=(-2, 2), textcoords="offset points",
                    color=MUTED, fontsize=6.7, ha="right")
        fig.legend(loc="upper center", ncols=3, handlelength=1.6,
                   handletextpad=0.4, columnspacing=1.1, borderaxespad=0.24)
        fig.savefig(destination, metadata={"CreationDate": None})
        plt.close(fig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    args = parser.parse_args()
    ratios = read_ratios(args.paper / "tables/star_resident_prefix_times.csv")
    destination = args.paper / "figures/revision_prefix_cost.pdf"
    destination.parent.mkdir(parents=True, exist_ok=True)
    plot_ratios(ratios, destination)


if __name__ == "__main__":
    main()
