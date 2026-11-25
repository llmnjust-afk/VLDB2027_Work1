#!/usr/bin/env python3
"""Audit the Amazon cancellation cells and render the paper's TikZ panels."""

import argparse
import math
from pathlib import Path

from compare_replay import csv_rows
from revision_experiments import BUILD, ROOT, load_json
from run_main4_cancellation import CONDITIONS, METHODS, NET_OPS, NEUTRAL_PAIRS, complete_case


SERIES = (
    ("Static", "black!80", "solid", "dot", -0.065),
    ("Static-Net", "gray!85!black", "dashed", "circle", 0.065),
    ("Fused", "blue!70!black", "solid", "square", -0.065),
    ("Fused-Pruned", "teal!65!black", "solid", "diamond", 0.065),
)


def audit_saved_csv(path, rows):
    fields_to_ignore = {"source"}
    saved = csv_rows(path)
    for recorded, actual in zip(saved, rows, strict=True):
        for field in actual.keys() - fields_to_ignore:
            value = actual[field]
            if isinstance(value, float):
                matches = math.isclose(float(recorded[field]), value, rel_tol=1e-10)
            else:
                matches = recorded[field] == str(value)
            if not matches:
                raise ValueError(f"{path}: mismatch on {field} for {actual['method']}")


def audit_cells(results):
    plan = load_json(results / "protocol.json")
    expected = [(condition, fraction, neutral_pairs)
                for condition in CONDITIONS for fraction, neutral_pairs in NEUTRAL_PAIRS.items()]
    cases = plan["cases"]
    observed = [(case["condition"], case["cancellation_pct"], case["reverse_pairs"]) for case in cases]
    if (observed != expected or plan["test_mode"] or plan["threads"] != 1
            or plan["pairs"] != 20 or plan["replays"] != 3 or plan["methods"] != list(METHODS)):
        raise ValueError(f"{results}: expected the completed single-core formal cancellation workload")

    traces, summaries, replays = {}, [], []
    for case in cases:
        condition = case["condition"]
        expected_ops = [NET_OPS + case["reverse_pairs"] * 2, NET_OPS] * plan["pairs"]
        if case["batch_operations"] != expected_ops:
            raise ValueError(f"{case['dataset']}: unexpected target/reset operation counts")
        closure, metrics, trials = complete_case(
            results, case, plan["replays"], {}, None, audit_only=True, expected_regions=traces.get(condition)
        )
        traces.setdefault(condition, closure)
        summaries.extend(metrics)
        replays.extend(trials)
    audit_saved_csv(results / "summary.csv", summaries)
    audit_saved_csv(results / "timing_replays.csv", replays)
    return {(row["condition"], row["method"], row["cancellation_pct"]): row for row in summaries}


def log_y(milliseconds):
    return 0.76 * math.log10(milliseconds / 2)


def draw_marker(color, shape, x, y):
    center = f"({x:.4f},{y:.4f})"
    if shape == "dot":
        return rf"\fill[{color}] {center} circle (1.35pt);"
    if shape == "circle":
        return rf"\draw[{color},fill=white] {center} circle (1.35pt);"
    if shape == "square":
        return rf"\draw[{color},fill=white] {center} ++(-1.3pt,-1.3pt) rectangle +(2.6pt,2.6pt);"
    return (rf"\fill[{color}] {center} ++(0,1.7pt) -- +(1.7pt,-1.7pt) "
            r"-- +(0,-3.4pt) -- +(-1.7pt,-1.7pt) -- cycle;")


def draw_panel(condition, index, metrics):
    width = 3.18
    first, last = .22, width - .22
    panel_x = index * (width + .47)
    lower, upper = log_y(1.75), log_y(550)
    closure = metrics[condition, METHODS[0], 0]["mean_target_closure_pct"]
    lines = [
        rf"\begin{{scope}}[shift={{({panel_x:.2f},0)}}]",
        rf"\node[anchor=south] at ({width/2},{upper + .065:.4f}) "
        rf"{{{condition.capitalize()} ({closure:.2f}\%)}};",
    ]
    for tick in (2, 10, 50, 100, 500):
        y = log_y(tick)
        label = rf" node[left,black,inner sep=1.5pt] {{{tick}}}" if index == 0 else ""
        lines.append(rf"\draw[gray!22] (0,{y:.4f}) -- ({width},{y:.4f});")
        lines.append(rf"\draw[gray!60] (0,{y:.4f}) -- (-0.06,{y:.4f}){label};")
    lines.append(rf"\draw[gray!60] (0,{upper:.4f}) -- (0,{lower:.4f}) -- ({width},{lower:.4f});")
    locations = {fraction: first + (last - first) * fraction / 50 for fraction in NEUTRAL_PAIRS}
    for fraction, x in locations.items():
        lines.append(rf"\draw[gray!60] ({x:.4f},{lower:.4f}) -- ({x:.4f},{lower - .055:.4f}) "
                     rf"node[below,black,inner sep=1.5pt] {{{fraction}}};")
    for method, (_, color, style, shape, offset) in zip(METHODS, SERIES, strict=True):
        ports, errors, marks = [], [], []
        for fraction, x in locations.items():
            x += offset
            row = metrics[condition, method, fraction]
            latency = row["mean_target_ms"]
            deviation = row["replay_mean_sd_ms"]
            y = log_y(latency)
            low, high = log_y(latency - deviation), log_y(latency + deviation)
            ports.append(f"({x:.4f},{y:.4f})")
            errors.append(rf"\draw[{color}] ({x:.4f},{low:.4f}) -- ({x:.4f},{high:.4f});")
            for bound in (low, high):
                errors.append(rf"\draw[{color}] ({x - .037:.4f},{bound:.4f}) -- ({x + .037:.4f},{bound:.4f});")
            marks.append(draw_marker(color, shape, x, y))
        lines.append(rf"\draw[{color},{style}] " + " -- ".join(ports) + ";")
        lines.extend(errors + marks)
    return lines + [r"\end{scope}"]


def render(metrics):
    lines = [
        r"% Generated by scripts/generate_main4_cancellation_paper.py.",
        r"\begin{tikzpicture}[x=1cm,y=1cm,font=\scriptsize,line width=.45pt]",
        r"\node[rotate=90] at (-0.66,0.89) {Target latency (ms)};",
        r"\node at (3.45,-0.54) {Within-batch cancellation (\%)};",
    ]
    for index, condition in enumerate(CONDITIONS):
        lines.extend(draw_panel(condition, index, metrics))
    legend_y = 2.47
    for x, (label, color, style, shape, _) in zip((.08, 1.54, 3.40, 4.86), SERIES, strict=True):
        lines.append(rf"\draw[{color},{style}] ({x - .14},{legend_y}) -- ({x + .14},{legend_y});")
        lines.append(draw_marker(color, shape, x, legend_y))
        lines.append(rf"\node[anchor=west,inner sep=1.5pt] at ({x + .14},{legend_y}) {{{label}}};")
    return "\n".join(lines + [r"\end{tikzpicture}", ""])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=BUILD / "vldb2027-main4-cancellation")
    parser.add_argument("--figure", type=Path,
                        default=ROOT / "VLDB2027_Work1-main/paper/figures/main4-cancellation.tex")
    args = parser.parse_args()
    metrics = audit_cells(args.results.resolve())
    args.figure.write_text(render(metrics))
    print(f"Rendered {len(metrics)} method/cell means and replay SDs in {args.figure}")


if __name__ == "__main__":
    main()
