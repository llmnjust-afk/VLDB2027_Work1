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
    for path in sorted(glob.glob(os.path.join(resdir, "*.csv"))):
        with open(path) as f:
            rows.extend(csv.DictReader(f))
    return rows


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


def emit_endtoend(agg, outdir, ref_b="1000", ref_p="0.5"):
    lines = ["% E5: end-to-end at T=32, B=%s, p=%s" % (ref_b, ref_p)]
    lines.append("\\begin{tabular}{lrrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & Batch & PerEdge & Static & batch/PerEdge & batch/Static \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        b = agg.get((g, "batch", "full", f"{g}_b{ref_b}_p{ref_p}", "32"))
        p = agg.get((g, "peredge", "full", f"{g}_b{ref_b}_p{ref_p}", "32"))
        s = agg.get((g, "static", "full", f"{g}_b{ref_b}_p{ref_p}", "32"))
        if not any([b, p, s]):
            continue
        bms = b["ms"][0] if b and b["ms"] else None
        pms = p["ms"][0] if p and p["ms"] else None
        sms = s["ms"][0] if s and s["ms"] else None
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
    lines = ["% E4: thread scaling at B=%s p=%s" % (ref_b, ref_p)]
    lines.append("\\begin{tabular}{lrrrr}")
    lines.append("\\toprule")
    lines.append("Graph & $T{=}1$ & $T{=}8$ & $T{=}32$ & speedup \\\\")
    lines.append("\\midrule")
    for g in GRAPH_ORDER:
        s = f"{g}_b{ref_b}_p{ref_p}"
        t1 = agg.get((g, "batch", "full", s, "1"))
        t8 = agg.get((g, "batch", "full", s, "8"))
        t32 = agg.get((g, "batch", "full", s, "32"))
        if not any([t1, t8, t32]):
            continue
        m1 = t1["ms"][0] if t1 and t1["ms"] else None
        m8 = t8["ms"][0] if t8 and t8["ms"] else None
        m32 = t32["ms"][0] if t32 and t32["ms"] else None
        c1 = f"{m1:.2f}" if m1 else "--"
        c8 = f"{m8:.2f}" if m8 else "--"
        c32 = f"{m32:.2f}" if m32 else "--"
        sp = f"{m1/m32:.1f}$\\times$" if m1 and m32 else "--"
        lines.append(f"{g} & {c1} & {c8} & {c32} & {sp} \\\\")
    lines.append("\\bottomrule")
    lines.append("\\end{tabular}")
    with open(os.path.join(outdir, "tab_scaling.tex"), "w") as f:
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


def main(resdir, outdir):
    os.makedirs(outdir, exist_ok=True)
    rows = load(resdir)
    if not rows:
        print("no csv rows")
        return
    agg = aggregate(rows)
    print(f"loaded {len(rows)} rows, {len(agg)} aggregates")
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
    emit_endtoend(agg, outdir)
    emit_scaling(agg, outdir)
    emit_regions(agg, outdir)
    print(f"wrote tables to {outdir}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else ".",
         sys.argv[2] if len(sys.argv) > 2 else "paperout")
