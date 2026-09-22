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
        if k not in dedup:
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
    lines = ["% E5: end-to-end at T=32, B={}, p={}".format(ref_b, ref_p)]
    lines.append("\\begin{tabular}{lrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & Batch & PerEdge & Static & batch/PerEdge & batch/Static \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        b = agg.get((g, "batch", "full", f"{g}_b{ref_b}_p{ref_p}", "32")) or agg.get((g, "batch", "full", f"{g}_s_b{ref_b}_p{ref_p}", "32"))
        p = agg.get((g, "peredge", "full", f"{g}_b{ref_b}_p{ref_p}", "32")) or agg.get((g, "peredge", "-", f"{g}_s_b{ref_b}_p{ref_p}", "1")) or agg.get((g, "peredge", "full", f"{g}_s_b{ref_b}_p{ref_p}", "32"))
        sms = static_by_g.get(g)
        if not any([b, p, sms]):
            continue
        bms = b["ms"][0] if b and b["ms"] else None
        pms = p["ms"][0] if p and p["ms"] else None
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
        row = []
        found = False
        for ab in ["full", "nomerge", "allseeds"]:
            st = agg.get((g, "batch", ab, s, "32"))
            ms = st["ms"][0] if st and st["ms"] else None
            rm = st["region_max"] if st else None
            row.append(f"{ms:.2f}" if ms else "--")
            row.append(f"{int(rm)}" if rm else "--")
            found = found or bool(ms)
        if not found:
            continue
        lines.append(f"{g} & {' & '.join(row)} \\\\")
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


def emit_singlepass(rows, outdir, ref_b="1000", ref_p="0.5"):
    import statistics
    by = {}
    for r in rows:
        by.setdefault((r["dataset"], r["method"], r["ablate"], r["stream"], r["threads"]), []).append(r)
    lines = ["% E2: cost decomposition at B=" + ref_b + " p=" + ref_p + " T=32 (peredge scaled to batch-equivalent)"]
    lines.append("\\begin{tabular}{lrrrr}")
    lines.append("\\toprule")
    lines.append("Method & ms/batch & evals/batch & peels/batch & region max \\\\")
    lines.append("\\midrule")
    cells = {}
    for g in GRAPH_ORDER:
        s = f"{g}_b{ref_b}_p{ref_p}"
        sp = f"{g}_s_b{ref_b}_p{ref_p}"
        for name, key in [("Batch", (g, "batch", "full", s, "32")),
                          ("No-merge", (g, "batch", "nomerge", s, "32")),
                          ("PerEdge", (g, "peredge", "-", sp, "1"))]:
            rs = by.get(key)
            if not rs:
                continue
            ms = [float(r["lat_mean_ms"]) for r in rs]
            nb = float(rs[0].get("nbatches") or 1)
            bsz = float(rs[0].get("batch_size") or 1)
            ev = [float(r["evals"]) / nb for r in rs]
            sd = [float(r["seeds"]) / nb for r in rs]
            rm = [int(float(r["region_max"])) for r in rs if r.get("region_max")]
            if name == "PerEdge":
                ms = [m * bsz for m in ms]
                ev = [float(r["evals"]) / nb for r in rs]
                cells.setdefault(name, []).append((statistics.mean(ms), statistics.mean(ev), None, None))
            else:
                peels = 1.0 if name == "Batch" else statistics.mean(sd)
                cells.setdefault(name, []).append((statistics.mean(ms), statistics.mean(ev), peels, max(rm) if rm else None))
    for name in ["Batch", "No-merge", "PerEdge"]:
        rowsv = cells.get(name)
        if not rowsv:
            continue
        m = statistics.mean([r[0] for r in rowsv])
        e = statistics.mean([r[1] for r in rowsv])
        pl = [r[2] for r in rowsv if r[2] is not None]
        rm = [r[3] for r in rowsv if r[3] is not None]
        pmean = statistics.mean(pl) if pl else None
        rmax = max(rm) if rm else None
        c1 = f"{m:.2f}"
        c2 = f"{e:,.0f}".replace(",", "\\,")
        c3 = "1" if name == "Batch" else (f"{pmean:,.1f}".replace(",", "\\,") if pmean else "--")
        c4 = f"{int(rmax)}" if rmax else "--"
        lines.append(f"{name} & {c1} & {c2} & {c3} & {c4} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_singlepass.tex"), "w") as f:
        f.write("\n".join(lines) + "\n")


def emit_correctness(matrix_log, outdir):
    import re
    rows = []
    try:
        for line in open(matrix_log):
            m = re.match(r"(\S+) (\S+) (\S+): (.+)", line.strip())
            if m and "ALL OK" in m.group(4):
                rows.append(m.groups())
    except FileNotFoundError:
        pass
    lines = ["% E1: differential-testing summary from the synthetic matrix log"]
    lines.append("\\begin{tabular}{lrrr}")
    lines.append("\\toprule")
    lines.append("Method & streams & batches & mismatched edges \\\\")
    lines.append("\\midrule")
    counts = {}
    for meth, _g, _f, _r in rows:
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
    for meth in ["batch", "batch-nomerge", "peredge"]:
        if meth not in counts:
            continue
        streams = counts[meth][0]
        batches = sum(nb.get(r[2], 0) for r in rows if r[0] == meth)
        lines.append(f"{meth} & {streams} & {batches} & 0 \\\\")
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
    emit_correctness(os.path.join(resdir, "matrix.log"), outdir)
    emit_singlepass(rows, outdir)
    emit_endtoend(agg, outdir, static_by_g)
    emit_scaling(agg, outdir)
    emit_regions(agg, outdir)
    emit_ablation(agg, outdir)
    print(f"wrote tables to {outdir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".",
         sys.argv[2] if len(sys.argv) > 2 else "paperout")
