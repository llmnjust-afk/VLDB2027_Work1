# VLDB2027_Work1 — Batch-Aware Parallel k-Truss Maintenance on Dynamic Graphs

Paper-in-progress code for *Batch-Aware Parallel Maintenance of k-Truss Decomposition on
Dynamic Graphs* (PVLDB Vol. 20, deadline Oct 1 2026).

## Layout

```
batchtruss/            our C++ implementation (single binary, header-only library)
  src/
    common.h           timing, RNG, StampSet epoch marks, parallel_for, latency stats
    graph.h            DynGraph: fully dynamic adjacency + open-addressing edge id map
    truss_static.h     Wang-Chen bucket-peeling static k-truss (index build / ground truth)
    fixpoint.h         exact local trussness re-evaluation F(e)
    incremental.h      sequential per-edge incremental maintenance baseline (AFF-style)
    batch.h            BatchMaintainer: batch-aware two-phase (demote, then promote)
                       parallel frontier fixpoint  <-- the paper's contribution
    stream.h           stream generation, synthetic graphs, SNAP temporal splitting
    main.cpp           CLI: synth|synth2|gen|snap|static|peredge|batch
    debug_bisect.cpp   differential debugger: verify maintained == recomputed after
                       every operation (or every batch)
  Makefile             g++ -O3 -march=native -std=c++17 -pthread
  data/                small synthetic graphs + generated streams for smoke tests
baselines/
  AccTrussDecomposition/   PVLDB'20 static truss baseline suite (WC/ROSS/PKT/MSP/...)
  batch-dynamic-kcore/     Liu et al. SPAA'22 batch-dynamic k-core (GBBS)
```

## Build

```
cd batchtruss && make
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

## Baseline repos

- https://github.com/RapidsAtHKUST/AccTrussDecomposition (PVLDB'20; contains Wang-Chen
  VLDB'12 sequential, ROSS, PKT, MSP, H-IDX)
- https://github.com/qqliu/batch-dynamic-kcore-decomposition (SPAA'22 batch k-core)
- Sun et al. PACMMOD'23 star-based truss maintenance has **no public code** — our
  `peredge` mode is an honest reimplementation of the per-edge incremental scheme
  (insert=monotone promotion, delete=monotone demotion, bounded fixpoint), labeled
  "our reimplementation" in the paper.

## Status

Work in progress: batch implementation exists; differential testing (bisect) is being
used to drive correctness to 100% before performance experiments.
