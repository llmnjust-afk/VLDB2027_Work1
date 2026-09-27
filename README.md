# VLDB2027_Work1 — Batch-Aware Parallel k-Truss Maintenance on Dynamic Graphs

Companion artifact for *Batch-Aware Parallel Maintenance of Exact k-Truss Decomposition
on Dynamic Graphs* (PVLDB Vol. 20, 2027).

## Layout

```
src/                       our C++ implementation (single binary, header-only library)
  common.h                 timing, RNG, StampSet epoch marks, parallel_for, latency stats
  graph.h                  DynGraph: fully dynamic adjacency + open-addressing edge id map
  truss_static.h           Wang-Chen bucket-peeling static k-truss (index build / ground truth)
  fixpoint.h               exact local trussness re-evaluation F(e)
  incremental.h            sequential per-edge incremental maintenance baseline (AFF-style)
  batch.h                  BatchMaintainer: batch-aware two-phase (demote, then promote)
                           parallel frontier fixpoint  <-- the paper's contribution
  stream.h                 stream generation, synthetic graphs, SNAP temporal splitting
  main.cpp                 CLI: synth|synth2|gen|snap|static|peredge|batch
  debug_bisect.cpp         differential debugger: verify maintained == recomputed after
                           every operation (or every batch)
  batch_v1_backup.h        development history snapshots (unused by the build)
  batch_mangled_trash.h
Makefile                   g++ -O3 -march=native -std=c++17 -pthread -> ./batchtruss
run_matrix.sh              full experiment driver used for the paper's tables
scripts/
  gen_streams.sh           stream generation for all eight graphs (fixed seeds)
  run_perf.sh              per-configuration runs + static/build index rows
  plot_results.py          emits every table in paper/tables + fig_regions from results/
  audit_counts.py          audits the experiment matrix against the paper's protocol
  supervise.sh             long-run supervision with auto-resume
baselines/                 third-party baseline suites (see "Baseline repos")
data/                      small synthetic graphs + generated streams for smoke tests
results/                   raw per-run CSV rows behind every table in the paper
paper/                     PVLDB Vol. 20 LaTeX source (acmart + pvldb.sty), final PDF,
                           tables, figures, refs.bib, CMT registration abstract
```

## Build

```
make
```

## Usage

```
./batchtruss synth  --n 200 --p 0.15 --out data/synth1.txt
./batchtruss synth2 --clusters 60 --size 25 --bridge 300 --out data/synth2.txt
./batchtruss gen    --graph data/synth1.txt --out data/s.txt --batches 40 --batch-size 100 --p-insert 0.8 --seed 7
./batchtruss static --graph data/synth1.txt --threads 8
./batchtruss peredge --graph data/synth1.txt --stream data/s.txt --check-every 10 --dataset synth1
./batchtruss batch  --graph data/synth1.txt --stream data/s.txt --threads 32 --check-every 10 --dataset synth1
./batchtruss batch  --graph data/synth1.txt --stream data/s.txt --threads 32 --ablate nomerge --dataset synth1
./batchtruss batch  --graph data/synth1.txt --stream data/s.txt --threads 32 --ablate allseeds --dataset synth1
./batchtruss snap   --temporal data/CollegeMsg.txt --fraction 0.9 --batch-size 1000 --mode append --graph-out data/cm_graph.txt --stream-out data/cm_stream.txt
```

Output is one CSV row per run (print header first). `--check-every K` re-verifies the
maintained decomposition against a full static recompute every K batches; the final CSV
column `verify_ok` is always 1 for a correct run.

## Reproducing the paper

1. `make`
2. Six smaller graphs (email, wiki-Vote, Enron, Amazon, Gnutella, YouTube) and all
   ablations: `bash scripts/run_perf.sh` or `bash run_matrix.sh`.
3. Giants (LiveJournal: 4.8M vertices / 42.8M edges after dedup; Skitter: 1.7M / 11.1M):
   download `soc-LiveJournal1.txt` and `as-skitter.txt` from SNAP, then `bash
   scripts/gen_streams.sh` (fixed seeds) to build the final graphs and streams. The
   giant runs take hours to days at 32 threads; the paper's Section 8 protocol lists
   every configuration (B=10..10K, p in {0.25, 0.5, 0.75}, three repetitions on every
   full-length batch workload; the B=1 giant leg of the batch path is a 200-operation
   single-run probe, and the per-edge path uses dedicated 200K-operation B=1 streams,
   one run each, for artifact-level auditing).
4. Tables + Figure: `python3 scripts/plot_results.py results paper/tables`
5. Matrix audit: `python3 scripts/audit_counts.py results`

Every published run carries `verify_ok=1` (maintained decomposition checked against
Wang-Chen static recomputation).

## Baseline repos

- https://github.com/RapidsAtHKUST/AccTrussDecomposition (PVLDB'20; contains Wang-Chen
  VLDB'12 sequential, ROSS, PKT, MSP, H-IDX)
- https://github.com/qqliu/batch-dynamic-kcore-decomposition (SPAA'22 batch k-core)
- Sun et al. PACMMOD'23 star-based truss maintenance has **no public code** — our
  `peredge` mode is an honest reimplementation of the per-edge incremental scheme
  (insert=monotone promotion, delete=monotone demotion, bounded fixpoint), labeled
  "our reimplementation" in the paper.

## Differential-testing matrix (results/matrix.log)

24 synthetic streams (s1/s2 x B in {1,10,100,1000} x p in {0.0,0.5,1.0}), 40 batches
each, replayed at T=4 with --check-every 1 on every batch of every stream against a
from-scratch Wang-Chen static recomputation: 72/72 rows ALL OK, 960 batches, zero
mismatched edges (s1 pairs with data/synth.txt, s2 with data/synth2.txt).

## Status

Data freeze. Every giant cell in the paper now comes from full-length measurements:
B=10..10K batch workloads (three repetitions) and full 200K-operation B=1 streams for
the per-edge path; the B=1 giant batch leg is a disclosed 200-operation single-run
probe. PVLDB Vol. 20 submission finalized (10 pages, official acmart + pvldb template;
all eight graphs measured; raw results in results/; tables and fig_regions.pdf are
emitted byte-stably by scripts/plot_results.py).
