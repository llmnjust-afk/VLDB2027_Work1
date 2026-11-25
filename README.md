# Exact Trussness Maintenance with Two-Snapshot Seeds

This repository implements exact edge trussness maintenance at the end of an ordered
batch of insertions and deletions in an undirected graph. Fused-Pruned coalesces the
events, finds a triangle closure using seeds from the old and final graphs, and
recovers the exact labels in that closure. Its fused support count and residual
low-support queue avoid adjacency scans during part of the peel.

`src/` contains the C++ implementation and baseline adapters. `scripts/` contains
experiment, verification, and analysis programs. `tests/` contains independent
small-graph oracles and fixtures. The repository history also contains the paper,
data, and results for an earlier implementation. Use the experiment entry points
below for the current code.

## Build and verify

A C++17 compiler, GNU Make and Python 3.10 or newer are required. The benchmark
runners use Linux CPU affinity and record the compiler and hardware.

```bash
make -j2 all
./batchtruss batch --ablate fused-pruned \
  --graph tests/fixtures/k4.graph --stream tests/fixtures/k4-mixed.stream \
  --threads 1 --check-every 1
python3 -B tests/test_replay.py
check_bin=$(mktemp)
g++ -O3 -std=c++17 -pthread -Isrc tests/test_region_optimizations.cpp -o "$check_bin"
"$check_bin"
rm -f -- "$check_bin"
```

The default build and fixture tests work without downloaded datasets or third-party
baselines. A verification replay checks edge membership and every live edge label
at each checkpoint. Run timing separately with `--timing-only` and `--batch-csv`
to collect batch measurements.

## Matched 2014 Stack Overflow replays

To recreate the input for the 2014 daily and 14-day comparisons, download the
original SNAP Stack Overflow trace and prepare both views of the same stream.

```bash
mkdir -p data .build
curl --fail --location --retry 3 \
  https://snap.stanford.edu/data/sx-stackoverflow.txt.gz \
  -o data/sx-stackoverflow.txt.gz.part
gzip -t data/sx-stackoverflow.txt.gz.part
mv data/sx-stackoverflow.txt.gz.part data/sx-stackoverflow.txt.gz
for days in 1 14; do
  python3 -B scripts/prepare_large_temporal_replay.py \
    --start-date 2014-05-03 --replay-days 168 --retention-days 7 \
    --checkpoint-days "$days" --output ".build/stackoverflow-2014-b${days}d"
done
```

The resident Star adapter uses the released implementation at commit
`8e9af42f2dfcbaa402edb5d052a1a5ff24bff5d0` of
[jinrdfh/TrussMaintenance](https://github.com/jinrdfh/TrussMaintenance).
Luo-Edge is an independent implementation of the paper's edge-update branch.

```bash
git clone https://github.com/jinrdfh/TrussMaintenance.git .build/TrussMaintenance
git -C .build/TrussMaintenance checkout --detach 8e9af42f2dfcbaa402edb5d052a1a5ff24bff5d0
python3 -B scripts/run_unified_dynamic.py --cases \
  --prepared stackoverflow-2014-heldout-b1d .build/stackoverflow-2014-b1d 7 \
  --prepared stackoverflow-2014-heldout-b14d .build/stackoverflow-2014-b14d 7 \
  --include-pruned --profile-pruned --runs 3 --output .build/vldb2027-main4-heldout
python3 -B scripts/run_unified_dynamic.py --check-only --output .build/vldb2027-main4-heldout
```

The runner checks the Star source hashes before building. It records raw batch
latencies, correctness, memory, provenance and timing summaries in the output
directory. It chooses an allowed logical CPU by default. Pass `--cpu` to select a
specific core.

## Cancellation, optional routing, and analyses

```bash
bash scripts/prepare_replay_inputs.sh .build/vldb2027-inputs
python3 -B scripts/run_main4_cancellation.py --pairs 20 --runs 3 \
  --output .build/vldb2027-main4-cancellation
python3 -B scripts/run_main4_cancellation.py --check-only \
  --output .build/vldb2027-main4-cancellation
python3 -B scripts/run_adaptive_online.py --pruned-routes --runs 3 \
  --output .build/vldb2027-main4-adaptive-pruned
```

The routing runner prepares the separate calibration and test periods using the
same SNAP source. It saves its selection before running the test. `--check-only`
re-audits a completed directory. The cancellation runner holds net changes fixed
and varies effective cancellation and the placement of the affected components.

`scripts/generate_main4_heldout_paper.py`, `scripts/generate_main4_cancellation_paper.py`
and `scripts/audit_adaptive_online_archive.py` can regenerate or audit the associated
outputs from recorded replay directories. Use `--help` for input paths. The
optional MSP baseline uses an upstream version with a recorded local patch.
`scripts/restore_measured_msp.sh` restores and verifies it into an empty
destination. The corresponding source checksums and notices are under
`provenance/`.
