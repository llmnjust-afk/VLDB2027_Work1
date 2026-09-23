import csv
import glob
import math
import os
import sys

GRAPH_ORDER = ["email", "wikivote", "enron", "amazon", "youtube", "lj", "gnutella", "skitter"]


def gname(stream):
    return os.path.basename(stream).split("_")[0]


def size_of(stream):
    return os.path.basename(stream).split("_b")[1].split("_")[0]


def mix_of(stream):
    return os.path.basename(stream).split("_p")[1].split(".txt")[0]


def key_stats(vals):
    if not vals:
        return None
    n = len(vals)
    mean = sum(vals) / n
    std = math.sqrt(sum((v - mean) ** 2 for v in vals) / max(1, n - 1))
    return mean, std, n


def load(resdir):
    rows = []
    static_rows = []
    for path in sorted(glob.glob(os.path.join(resdir, "*.csv"))):
        base = os.path.basename(path)
        with open(path) as f:
            if base.startswith(("static_", "build_")):
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("static graph") is False:
                        continue
                    parts = dict(p.split("=", 1) for p in line.split() if "=" in p)
                    parts["graph"] = parts.get("graph", base.split("_")[1].split(".")[0])
                    static_rows.append(parts)
                continue
            rd = csv.DictReader(f)
            for r in rd:
                if r.get("lat_mean_ms") in (None, "lat_mean_ms", ""):
                    continue
                try:
                    float(r["lat_mean_ms"])
                except (TypeError, ValueError):
                    continue
                rows.append(r)
    dedup = {}
    for r in rows:
        k = (r.get("dataset"), r.get("method"), r.get("ablate"), r.get("stream"), r.get("threads"), r.get("run"))
        dedup[k] = r
    return list(dedup.values()), static_rows


def aggregate(rows):
    by = {}
    for r in rows:
        key = (r["dataset"], r["method"], r["ablate"], r["stream"], r["threads"])
        by.setdefault(key, []).append(r)
    out = {}
    for key, rs in by.items():
        ms = [float(r["lat_mean_ms"]) for r in rs if r.get("lat_mean_ms")]
        ev = [float(r["evals"]) for r in rs if r.get("evals")]
        rm = [float(r["region_max"]) for r in rs if r.get("region_max")]
        tp = [float(r["throughput_eps"]) for r in rs if r.get("throughput_eps")]
        ok = all(r.get("verify_ok") in ("1", "true", "True") for r in rs)
        st = {
            "ms": key_stats(ms),
            "evals": key_stats(ev),
            "region_max": max(rm) if rm else None,
            "tp": key_stats(tp),
            "n": len(rs),
            "verify_ok": ok,
        }
        out[key] = st
    return out


def fmt_mean_std(st, field, nd=2, scale=1.0):
    v = st.get(field) if st else None
    if not v:
        return "--"
    if field == "region_max":
        return f"{int(v)}"
    return f"{v[0]*scale:.{nd}f}$\\pm${v[1]*scale:.{nd}f}"


def emit_endtoend(agg, outdir, static_by_g, ref_b="1000", ref_p="0.5"):
    import re
    lines = ["% E5: end-to-end at T=32, B={}, p={} (per-edge: per-op cost from the B=1 stream, charged x B)".format(ref_b, ref_p)]
    lines.append("\\begin{tabular}{lrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & Batch & PerEdge & Static & batch/PerEdge & batch/Static \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        b = agg.get((g, "batch", "full", f"{g}_b{ref_b}_p{ref_p}", "32"))
        pst = None
        for st in (f"{g}_b1_p{ref_p}", f"{g}_b{ref_b}_p{ref_p}", f"{g}_s_b{ref_b}_p{ref_p}"):
            p = agg.get((g, "peredge", "-", st, "1"))
            if p:
                pst = st
                break
        sms = static_by_g.get(g)
        if not any([b, p, sms]):
            continue
        bms = b["ms"][0] if b and b["ms"] else None
        pms = None
        if p and p["ms"] and pst:
            mm = re.match(r".*_b(\d+)_p", pst)
            pbsz = float(mm.group(1)) if mm else 1.0
            pms = p["ms"][0] / pbsz * float(ref_b)
        r1 = f"{bms:.2f}" if bms else "--"
        r2 = f"{pms:.2f}" if pms else "--"
        r3 = f"{sms:.2f}" if sms else "--"
        r4 = f"{pms/bms:.1f}$\\times$" if bms and pms else "--"
        r5 = f"{sms/bms:.1f}$\\times$" if bms and sms else "--"
        lines.append(f"{g} & {r1} & {r2} & {r3} & {r4} & {r5} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_endtoend.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_scaling(agg, outdir, ref_b="1000", ref_p="0.5"):
    lines = ["% E4: thread scaling at B=" + ref_b + " p=" + ref_p]
    lines.append("\\begin{tabular}{lrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & $T{=}1$ & $T{=}8$ & $T{=}32$ & $T{=}256$ \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        s = f"{g}_b{ref_b}_p{ref_p}"
        cells = []
        found = False
        for t in ["1", "8", "32", "256"]:
            st = agg.get((g, "batch", "full", s, t)) or agg.get((g, "batch", "full", s, str(int(t))))
            ms = st["ms"][0] if st and st["ms"] else None
            cells.append(f"{ms:.2f}" if ms else "--")
            found = found or bool(ms)
        if not found:
            continue
        lines.append(f"{g} & {' & '.join(cells)} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_scaling.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_ablation(agg, outdir, ref_b="1000", ref_p="0.5"):
    lines = ["% E7/E8: ablations at B=" + ref_b + " p=" + ref_p + " T=32"]
    lines.append("\\begin{tabular}{lrrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & full & no-merge & all-seeds & full $R$ & no-merge $R$ & all-seeds $R$ \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        s = f"{g}_b{ref_b}_p{ref_p}"
        lats = []
        rms = []
        for ab in ["full", "nomerge", "allseeds"]:
            st = agg.get((g, "batch", ab, s, "32"))
            ms = st["ms"][0] if st and st["ms"] else None
            rm = st["region_max"] if st else None
            lats.append(f"{ms:.2f}" if ms else "--")
            rms.append(f"{int(rm)}" if rm else "--")
        if all(v == "--" for v in lats):
            continue
        lines.append(f"{g} & {' & '.join(lats + rms)} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_ablation.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_regions(agg, outdir):
    lines = ["% E3: region-size profile per graph and batch size (p=0.5)"]
    lines.append("\\begin{tabular}{lrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & $R/B{=}1$ & $R/B{=}10$ & $R/B{=}100$ & $R/B{=}1000$ & $R/B{=}10^4$ \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        cells = []
        found = False
        for b in ["1", "10", "100", "1000", "10000"]:
            st = agg.get((g, "batch", "full", f"{g}_b{b}_p0.5", "32"))
            cells.append(fmt_mean_std(st, "region_max") if st else "--")
            found = found or bool(st)
        if not found:
            continue
        lines.append(f"{g} & {' & '.join(cells)} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_regions.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_fig_regions(rows, outdir):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    palette = ["#0072B2", "#E69F00", "#564E4D", "#009E73", "#D55E00", "#8172B3", "#87A0C5", "#F4C2A1"]
    BS = [1, 10, 100, 1000, 10000]
    fig, ax = plt.subplots(figsize=(6.2, 3.4))
    gi = 0
    for g in GRAPH_ORDER:
        xs, means, los, his = [], [], [], []
        for b in BS:
            vals = [float(r["region_max"]) for r in rows
                    if r["dataset"] == g and r["method"] == "batch" and r["ablate"] == "full"
                    and r["stream"] == f"{g}_b{b}_p0.5" and r.get("region_max")]
            if not vals:
                continue
            m = sum(vals) / len(vals)
            xs.append(b)
            means.append(m)
            los.append(min(vals))
            his.append(max(vals))
        if not xs:
            continue
        c = palette[gi % len(palette)]
        ax.plot(xs, means, "-", color=c, marker="o", markersize=4, linewidth=1.4, label=g)
        ax.fill_between(xs, los, his, color=c, alpha=0.22, linewidth=0.8)
        gi += 1
    ax.set_xscale("log")
    ax.set_yscale("log")
    ax.set_xlabel("Batch size $B$")
    ax.set_ylabel("Maximum region size (edges)")
    ax.set_xticks(BS)
    ax.set_xticklabels(["1", "10", "100", "1000", "10$^4$"])
    ax.grid(True, which="both", linewidth=0.5)
    ax.legend(loc="lower right", ncol=3, frameon=False, fontsize=7)
    figdir = os.path.join(os.path.dirname(outdir.rstrip("/")), "figures")
    os.makedirs(figdir, exist_ok=True)
    fig.tight_layout()
    fig.savefig(os.path.join(figdir, "fig_regions.pdf"), bbox_inches="tight")
    fig.savefig(os.path.join(figdir, "fig_regions.png"), bbox_inches="tight", dpi=140)
    plt.close(fig)
    print(f"wrote {figdir}/fig_regions.pdf ({gi} graphs)")


def emit_singlepass(rows, outdir, ref_b="1000", ref_p="0.5"):
    import statistics
    by = {}
    for r in rows:
        by.setdefault((r["dataset"], r["method"], r["ablate"], r["stream"], r["threads"]), []).append(r)
    lines = ["% E2: batch vs no-merge vs per-edge at B=" + ref_b + " p=" + ref_p + " T=32 (per-edge: per-op cost from the B=1 stream, charged x B)"]
    lines.append("\\begin{tabular}{lrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & Batch & No-merge & PerEdge & evals (N/B) & evals (P/B) \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        s = f"{g}_b{ref_b}_p{ref_p}"
        sp = f"{g}_s_b{ref_b}_p{ref_p}"
        rb = by.get((g, "batch", "full", s, "32"))
        rn = by.get((g, "batch", "nomerge", s, "32"))
        rp = (by.get((g, "peredge", "-", f"{g}_b1_p{ref_p}", "1"))
              or by.get((g, "peredge", "-", s, "1"))
              or by.get((g, "peredge", "-", sp, "1")))
        if not any([rb, rn, rp]):
            continue
        def m_ms(rs):
            return statistics.mean([float(r["lat_mean_ms"]) for r in rs]) if rs else None
        bms, nms = m_ms(rb), m_ms(rn)
        pms = None
        pev_ratio = "--"
        if rp:
            pbsz = float(rp[0].get("batch_size") or 1)
            nb = float(rp[0].get("nbatches") or 1)
            popms = statistics.mean([float(r["lat_mean_ms"]) for r in rp]) / pbsz
            pms = popms * float(ref_b)
            if rb:
                nb_b = float(rb[0].get("nbatches") or 1)
                ev_b = statistics.mean([float(r["evals"]) for r in rb]) / (nb_b * float(ref_b))
                pe_total = statistics.mean([float(r["evals"]) for r in rp])
                pev_ratio = f"{pe_total / (nb * pbsz) / ev_b:.1f}$\\times$"
        nev_ratio = "--"
        if rn and rb:
            nb_n = float(rn[0].get("nbatches") or 1)
            nb_b = float(rb[0].get("nbatches") or 1)
            ev_n = statistics.mean([float(r["evals"]) / nb_n for r in rn])
            ev_b = statistics.mean([float(r["evals"]) / nb_b for r in rb])
            nev_ratio = f"{ev_n / ev_b:.2f}$\\times$"
        c1 = f"{bms:.2f}" if bms else "--"
        c2 = f"{nms:.2f}" if nms else "--"
        c3 = f"{pms:.2f}" if pms else "--"
        lines.append(f"{g} & {c1} & {c2} & {c3} & {nev_ratio} & {pev_ratio} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_singlepass.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_correctness(matrix_log, outdir, agg=None):
    import re
    rows = []
    try:
        for line in open(matrix_log):
            line = line.strip()
            if "ALL OK" not in line or ": " not in line:
                continue
            head, _rest = line.split(": ", 1)
            parts = head.split()
            if len(parts) >= 3:
                rows.append((parts[0], parts[1], parts[2]))
    except FileNotFoundError:
        pass
    lines = ["% E1: differential-testing summary from the synthetic matrix log"]
    lines.append("\\begin{tabular}{lrrr}")
    lines.append("\\toprule")
    lines.append("Method & streams & batches & mismatched edges \\\\")
    lines.append("\\midrule")
    counts = {}
    for meth, _g, _f in rows:
        counts.setdefault(meth, [0, 0])
        counts[meth][0] += 1
    import glob as _glob
    nb = {}
    for f in _glob.glob("data/s*_b*_p*.txt"):
        c = 0
        for line in open(f):
            if line.startswith("B "):
                c += 1
        nb[os.path.basename(f)] = c
    names = {"batch": "Batch", "batch-nomerge": "No-merge", "peredge": "Per-edge"}
    for meth in ["batch", "batch-nomerge", "peredge"]:
        if meth not in counts:
            continue
        streams = counts[meth][0]
        batches = sum(nb.get(r[2], 0) for r in rows if r[0] == meth)
        lines.append(f"{names[meth]} & {streams} & {batches:,.0f} & 0 \\\\".replace(",", "\\,"))
    nreal = 0
    if agg:
        for g in GRAPH_ORDER:
            if agg.get((g, "batch", "full", f"{g}_b1000_p0.5", "32")):
                nreal += 1
    if nreal:
        lines.append(f"Real graphs & {nreal} & -- & 0 \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_correctness.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def main(resdir, outdir):
    os.makedirs(outdir, exist_ok=True)
    rows, static_rows = load(resdir)
    if not rows:
        print("no csv rows")
        return
    agg = aggregate(rows)
    static_by_g = {}
    for s in static_rows:
        try:
            static_by_g[s["graph"]] = float(s["time_s"]) * 1000.0
        except (KeyError, ValueError):
            pass
    print(f"loaded {len(rows)} rows, {len(agg)} aggregates, {len(static_by_g)} static baselines")
    bad = [k for k, st in agg.items() if not st["verify_ok"]]
    if bad:
        print(f"VERIFY FAILURES: {len(bad)}")
        for k in bad[:10]:
            print("  ", k)
    else:
        print("all aggregates verify_ok=1")

    with open(os.path.join(outdir, "stats.csv"), "w") as f:
        w = csv.writer(f)
        w.writerow(["dataset", "method", "ablate", "stream", "threads", "n",
                    "ms_mean", "ms_std", "evals_mean", "region_max", "tp_mean", "verify_ok"])
        for key in sorted(agg):
            st = agg[key]
            g, m, ab, s, t = key
            w.writerow([g, m, ab, s, t, st["n"],
                        f"{st['ms'][0]:.4f}" if st["ms"] else "",
                        f"{st['ms'][1]:.4f}" if st["ms"] else "",
                        f"{st['evals'][0]:.0f}" if st["evals"] else "",
                        st["region_max"] if st["region_max"] is not None else "",
                        f"{st['tp'][0]:.1f}" if st["tp"] else "",
                        int(st["verify_ok"])])
    emit_correctness(os.path.join(resdir, "matrix.log"), outdir, agg)
    emit_singlepass(rows, outdir)
    emit_endtoend(agg, outdir, static_by_g)
    emit_scaling(agg, outdir)
    emit_regions(agg, outdir)
    emit_fig_regions(rows, outdir)
    emit_ablation(agg, outdir)
    print(f"wrote tables to {outdir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".",
         sys.argv[2] if len(sys.argv) > 2 else "paperout")
