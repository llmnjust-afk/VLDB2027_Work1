#!/usr/bin/env python3
"""Audit the locality experiment and generate its manuscript artifacts."""

import argparse
from math import isclose
from pathlib import Path
from statistics import mean, median

import matplotlib as mpl

mpl.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter, MaxNLocator, NullLocator

from compare_replay import csv_rows
from experiment_replays import audit_paired_case
from paper_plot_style import COPPER, FULL, GRID, INK, PAPER_RCPARAMS, PLUM, STAR
from revision_experiments import ROOT, load_json, read_batches
from run_locality_experiments import (
    CONDITIONS,
    DATASETS,
    METHODS,
    check_inputs,
    check_inverses,
    latency_rows,
    profile_rows,
    write_csv,
)


LABELS = {"email": "Email", "amazon": "Amazon", "full": "Regional", "star-resident": "Resident Star"}
METHOD_STYLE = {"full": (FULL, "o"), "star-resident": (STAR, "s")}
PHASE_STYLE = {
    "closure": ("Closure", FULL),
    "recount": ("Recount", "#81A7BA"),
    "peel": ("Peel", PLUM),
    "other": ("Other", "#B9C3CA"),
    "partition": ("Partition", "#6F7865"),
    "delete": ("Delete", COPPER),
    "insert": ("Insert", "#CA9066"),
    "refresh": ("Refresh", "#E2CEA1"),
}


def audit(run):
    plan = load_json(run / "protocol.json")
    if plan["test_mode"] or plan["threads"] != 1 or plan["replays"] != 3:
        raise ValueError("expected the formal three-repeat single-thread experiment")
    expected_cases = {(dataset, condition) for dataset in DATASETS
                      for condition in (*CONDITIONS, "observed")}
    cases = {(case["dataset"], case["condition"]): case for case in plan["cases"]}
    if len(plan["cases"]) != len(expected_cases) or cases.keys() != expected_cases:
        raise ValueError("unexpected datasets or conditions")

    latency, profile = [], []
    for case in plan["cases"]:
        check_inputs(case)
        condition = case["condition"]
        directory = run / f"{case['dataset']}-{condition}"
        if condition == "observed":
            if case["batch_operations"] != [1000] * 200 or case["selected"] != list(range(1, 201)):
                raise ValueError(f"{directory}: expected 200 observed batches of 1,000 events")
            replays, expected = 0, None
        else:
            if case["batch_operations"] != [100] * 40 or case["selected"] != list(range(1, 41, 2)):
                raise ValueError(f"{directory}: expected 20 selected batches with inverse resets")
            stream = Path(case["stream"])
            check_inverses(stream, read_batches(stream), 20, 100)
            replays = plan["replays"]
            expected = {method: [(50, 50, case["initial_edges"])] * 40 for method in METHODS}
        verified = audit_paired_case(directory, case, METHODS, replays, expected_traces=expected)
        for method in METHODS:
            profile.extend(profile_rows(directory, case, method, verified[method]))
        if replays:
            latency.extend(latency_rows(directory, case, replays))

    for name, actual in (("locality_summary.csv", latency), ("phase_summary.csv", profile)):
        recorded = csv_rows(run / name)
        if len(recorded) != len(actual) or any(
            given.keys() != found.keys() or any(
                not isclose(float(given[key]), value, rel_tol=1e-12, abs_tol=1e-12)
                if isinstance(value, float) else given[key] != str(value)
                for key, value in found.items())
            for given, found in zip(recorded, actual)
        ):
            raise ValueError(f"{run / name}: summary differs from re-audited raw replays")

    placement = {}
    for dataset in DATASETS:
        input_dir = run / "inputs" / dataset
        manifest = load_json(input_dir / "manifest.json")
        if (manifest["batches"], manifest["batch_size"], manifest["seed"]) != (20, 100, 42):
            raise ValueError(f"{input_dir}: unexpected generator settings")
        placement_rows = csv_rows(input_dir / "placement.csv")
        for condition in CONDITIONS:
            rows = [row for row in placement_rows if row["condition"] == condition]
            if [int(row["batch"]) for row in rows] != cases[dataset, condition]["selected"]:
                raise ValueError(f"{input_dir}: placement records differ from selected batches")
            units = [int(row["distinct_units"]) for row in rows]
            placement[dataset, condition] = {
                "placement_unit": manifest["placement_unit"],
                "placement_units_min": min(units),
                "placement_units_mean": mean(units),
                "placement_units_max": max(units),
                "initial_edges": manifest["initial_edges"],
                "initial_triangle_components": manifest["nontrivial_triangle_components"],
            }
    for row in latency:
        case_key = row["dataset"], row["condition"]
        row.update(placement[case_key])
        selected = set(cases[case_key]["selected"])
        from_replays = [
            [float(batch["elapsed_us"]) / 1000 for batch in csv_rows(
                Path(row["source"]) / f"{row['method']}.timing.{replay}.batches.csv")
             if int(batch["batch"]) in selected]
            for replay in range(row["replays"])
        ]
        first = [samples[0] for samples in from_replays]
        row.update(pooled_median_ms_per_batch=median(value for samples in from_replays for value in samples),
                   first_measured_batch_ms_min=min(first), first_measured_batch_ms_max=max(first))
    return latency, profile


def draw_locality(rows, destination):
    measurements = {(row["dataset"], row["condition"], row["method"]): row for row in rows}
    settings = [(dataset, condition) for dataset in DATASETS for condition in CONDITIONS]
    location = range(len(settings))
    labels = [condition[0].upper() for _, condition in settings]
    fig, (closure, latency) = plt.subplots(1, 2, figsize=(3.30, 1.35))
    fig.subplots_adjust(left=0.16, right=0.986, bottom=0.26, top=0.758, wspace=0.61)

    sizes = [measurements[dataset, condition, "full"]["mean_full_closure_edges"]
             for dataset, condition in settings]
    closure.bar(location, sizes, width=0.6, color=METHOD_STYLE["full"][0], zorder=3)
    closure.set_title("(a) Closure size", loc="left", pad=3)
    closure.set_ylabel("Edges", labelpad=2)
    closure.set_yticks([0, 25_000, 50_000, 75_000])
    closure.yaxis.set_major_formatter(FuncFormatter(lambda x, _: f"{x / 1000:g}k" if x else "0"))
    closure.set_ylim(0, max(sizes) * 1.12)

    for method, shift in zip(METHODS, (-0.14, 0.14)):
        points = [measurements[dataset, condition, method] for dataset, condition in settings]
        color, marker = METHOD_STYLE[method]
        latency.errorbar([x + shift for x in location], [row["mean_ms_per_batch"] for row in points],
                         yerr=[row["replay_mean_sd_ms"] for row in points], color=color,
                         marker=marker, markersize=3.7, linestyle="none", capsize=1.7,
                         markerfacecolor=color if method == "full" else "white",
                         linewidth=0.95,
                         label=LABELS[method], zorder=3)
    latency.set_yscale("log")
    latency.set_yticks([1, 5, 20, 50], labels=["1", "5", "20", "50"])
    latency.yaxis.set_minor_locator(NullLocator())
    latency.set_ylabel("ms/batch", labelpad=2)
    latency.set_title("(b) Latency", loc="left", pad=3)
    latency.set_ylim(0.65, 85)
    fig.legend(*latency.get_legend_handles_labels(), loc="upper center",
               ncols=2, handlelength=1, handletextpad=0.35,
               columnspacing=1.0, borderaxespad=0.18)

    for axis in (closure, latency):
        axis.set_xticks(location, labels)
        axis.set_xlim(-0.54, 3.54)
        axis.axvline(1.5, color=GRID, linewidth=0.7)
        axis.tick_params(axis="x", length=0, pad=2)
        for i, dataset in enumerate(DATASETS):
            axis.text(2 * i + 0.5, -0.29, LABELS[dataset],
                      transform=axis.get_xaxis_transform(), ha="center",
                      va="top", fontsize=7.5)
    fig.savefig(destination, metadata={"CreationDate": None})
    plt.close(fig)


def draw_phases(rows, destination):
    means = {(row["dataset"], row["method"], row["phase"]): row["mean_us_per_batch"] / 1000
             for row in rows if row["condition"] == "observed"}
    figure, axes = plt.subplots(1, 2, figsize=(3.30, 1.42), sharey=True)
    figure.subplots_adjust(left=0.18, right=0.984, bottom=0.33, top=0.66, wspace=0.28)
    stages = {"full": ("closure", "recount", "peel"),
              "star-resident": ("partition", "delete", "insert", "refresh")}
    for axis, dataset, panel in zip(axes, DATASETS, ("a", "b")):
        method_totals = []
        for y, method in zip((1, 0), METHODS):
            elapsed = means[dataset, method, "elapsed"]
            parts = [(stage, means[dataset, method, stage]) for stage in stages[method]]
            parts.append(("other", elapsed - sum(value for _, value in parts)))
            left = 0.0
            for stage, length in parts:
                axis.barh(y, length, left=left, height=0.49, color=PHASE_STYLE[stage][1],
                          edgecolor="white", linewidth=0.25, zorder=3)
                left += length
            axis.annotate(f"{elapsed:.2f}", (elapsed, y), xytext=(2, 0), textcoords="offset points",
                          va="center", fontsize=7, color=INK)
            method_totals.append(elapsed)
        axis.set_title(f"({panel}) {LABELS[dataset]}", loc="left", pad=3)
        axis.set_xlim(0, max(method_totals) * 1.42)
        axis.set_ylim(-0.48, 1.48)
        axis.set_yticks([0, 1], ["Star", LABELS["full"]])
        axis.xaxis.set_major_locator(MaxNLocator(nbins=3))
        axis.yaxis.grid(False)
        axis.xaxis.grid(True)
        axis.tick_params(axis="y", length=0)
    figure.supxlabel("Mean phase time (ms/batch)", y=0.07,
                     fontsize=PAPER_RCPARAMS["axes.labelsize"])
    legend_order = ("closure", "partition", "recount", "delete",
                    "peel", "insert", "other", "refresh")
    legend = [Patch(label=PHASE_STYLE[stage][0], color=PHASE_STYLE[stage][1])
              for stage in legend_order]
    figure.legend(handles=legend, loc="upper center", ncols=4, handlelength=1,
                  handletextpad=0.35, columnspacing=0.95, borderaxespad=0.18,
                  labelspacing=0.23)
    figure.savefig(destination, metadata={"CreationDate": None})
    plt.close(figure)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, default=ROOT / ".build/vldb2027-locality")
    parser.add_argument("--paper", type=Path, default=ROOT / "VLDB2027_Work1-main/paper")
    args = parser.parse_args()
    latency, profile = audit(args.run.resolve(strict=True))
    tables, figures = args.paper / "tables", args.paper / "figures"
    tables.mkdir(parents=True, exist_ok=True)
    figures.mkdir(parents=True, exist_ok=True)
    write_csv(tables / "locality_results.csv", latency)
    write_csv(tables / "locality_phase_results.csv", profile)
    with mpl.rc_context(PAPER_RCPARAMS):
        draw_locality(latency, figures / "revision_locality.pdf")
        draw_phases(profile, figures / "revision_phase_costs.pdf")


if __name__ == "__main__":
    main()
