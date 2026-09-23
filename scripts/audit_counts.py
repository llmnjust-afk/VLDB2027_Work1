#!/usr/bin/env python3
import glob
import os
import sys
from collections import Counter

d = sys.argv[1] if len(sys.argv) > 1 else "."
runs_giants = {"lj", "skitter", "youtube"}
expect = Counter()
rows = Counter()
for f in sorted(glob.glob(os.path.join(d, "batch_*_t32.csv"))):
    base = os.path.basename(f)
    g = base[len("batch_"):-len("_t32.csv")]
    for line in open(f):
        p = line.rstrip("\n").split(",")
        if len(p) < 7 or p[0] in ("dataset", ""):
            continue
        ds, meth, thr, ab, st = p[0], p[1], p[2], p[3], p[4]
        if meth == "batch" and ab == "full":
            bs_part = st.rsplit("_b", 1)[1].split("_")[0] if "_b" in st else ""
            if st.endswith("_b1000_p0.5") and thr != "32":
                expect[(ds, meth, thr, ab, st)] = 5
            elif thr == "32":
                want = 3 if ds in runs_giants else 5
                expect[(ds, meth, thr, ab, st)] = want
        elif meth == "batch" and ab in ("nomerge", "allseeds"):
            expect[(ds, meth, thr, ab, st)] = 5
        elif meth == "peredge":
            expect[(ds, meth, thr, ab, st)] = 5
        elif meth == "static":
            expect[(ds, meth, thr, ab, st)] = 1
        rows[(ds, meth, thr, ab, st)] += 1
bad = 0
for k in sorted(expect):
    if rows[k] < expect[k]:
        print(f"SHORT {k}: {rows[k]}/{expect[k]}")
        bad += 1
extra = set(rows) - set(expect)
for k in sorted(extra):
    print(f"UNEXPECTED {k}: {rows[k]}")
print(f"audit: {len(expect)} slots, {bad} short")
