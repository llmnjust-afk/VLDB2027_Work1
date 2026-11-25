// Run with: g++ -std=c++17 -O2 -pthread -Isrc tests/test_region_optimizations.cpp -o /tmp/test_region_optimizations
#include "batch.h"
#include <array>
#include <cassert>

using Mask = uint32_t;
constexpr uint32_t vertex_count = 5;
constexpr std::array<std::pair<uint32_t, uint32_t>, 10> edges{{
    {0, 1}, {0, 2}, {0, 3}, {0, 4}, {1, 2}, {1, 3}, {1, 4}, {2, 3}, {2, 4}, {3, 4},
}};
constexpr Mask snapshot_count = Mask{1} << edges.size();

constexpr Mask edge_bit(uint32_t u, uint32_t v) {
  for (size_t i = 0; i < edges.size(); ++i)
    if (edges[i].first == u && edges[i].second == v) return Mask{1} << i;
  return 0;
}

constexpr auto triangles = [] {
  std::array<Mask, 10> masks{};
  size_t i = 0;
  for (uint32_t u = 0; u < vertex_count; ++u)
    for (uint32_t v = u + 1; v < vertex_count; ++v)
      for (uint32_t w = v + 1; w < vertex_count; ++w)
        masks[i++] = edge_bit(u, v) | edge_bit(u, w) | edge_bit(v, w);
  return masks;
}();

// Independent threshold pruning on a snapshot. An entire round uses the same
// live mask, with neither bucket keys nor region selection from the maintainer.
std::array<uint32_t, edges.size()> threshold_labels(Mask snapshot) {
  std::array<uint32_t, edges.size()> labels{};
  for (uint32_t k = 2; k <= vertex_count; ++k) {
    Mask live = snapshot;
    while (true) {
      Mask kept = live;
      for (size_t i = 0; i < edges.size(); ++i) {
        Mask edge = Mask{1} << i;
        if (!(live & edge)) continue;
        uint32_t support = 0;
        for (Mask triangle : triangles)
          if ((triangle & edge) && (live & triangle) == triangle) ++support;
        if (support < k - 2) kept &= ~edge;
      }
      if (kept == live) break;
      live = kept;
    }
    for (size_t i = 0; i < edges.size(); ++i)
      if (live & (Mask{1} << i)) labels[i] = k;
  }
  return labels;
}

void test_snapshots(bool fused, bool certified, bool pruned = false) {
  std::vector<std::array<uint32_t, edges.size()>> truth;
  for (Mask mask = 0; mask < snapshot_count; ++mask) truth.push_back(threshold_labels(mask));
  std::vector<Op> ops;
  for (Mask before = 0; before < snapshot_count; ++before) {
    DynGraph initial;
    initial.set_vertices(vertex_count);
    initial.reserve_edges(edges.size());
    initial.init_hash(edges.size());
    for (const auto& [u, v] : edges)
      if (before & edge_bit(u, v)) initial.add_edge(u, v);
    for (Mask after = 0; after < snapshot_count; ++after) {
      DynGraph graph = initial.clone();
      BatchMaintainer batch(graph, 1);
      batch.fuse_closure_recount = fused;
      batch.certify_low_support = certified;
      batch.prune_below_four = pruned;
      batch.record_peel_scans = true;
      ops.clear();
      // Include a repeated update on (0,1), including canceling pairs.
      ops.push_back({static_cast<uint8_t>(bool(before & edge_bit(0, 1))), 0, 1});
      for (const auto& [u, v] : edges) {
        Mask bit = edge_bit(u, v);
        if ((bit & (before ^ after)) || bit == edge_bit(0, 1))
          ops.push_back({static_cast<uint8_t>(!(after & bit)), u, v});
      }
      batch.apply_batch(ops);
      for (size_t i = 0; i < edges.size(); ++i) {
        const auto& [u, v] = edges[i];
        int64_t actual = graph.find(DynGraph::ekey(u, v));
        assert((actual >= 0) == (truth[after][i] != 0));
        if (actual >= 0) assert(batch.tau[actual] == truth[after][i]);
      }
      uint64_t region_size = batch.last_region().size();
      assert(batch.stats.peel_certified_edges + batch.stats.peel_scanned_edges == region_size);
      assert(batch.stats.evals == (fused || pruned ? 1 : 2) * region_size + batch.stats.peel_scanned_edges);
    }
  }
  printf("Verified %u ordered snapshot pairs, fused=%d certified=%d pruned=%d\n",
         snapshot_count * snapshot_count, fused, certified, pruned);
}

void test_parallel(bool fused, bool certified, bool pruned = false) {
  // Each component is a K4 with a triangle attached along edge (0,1).
  // Each of the two free triangle edges has support 1. Removing their
  // common triangle twice would erroneously demote the surviving K4.
  constexpr Mask clique = edge_bit(0, 1) | edge_bit(0, 2) | edge_bit(0, 3) |
                         edge_bit(1, 2) | edge_bit(1, 3) | edge_bit(2, 3);
  constexpr Mask leaf = edge_bit(0, 4) | edge_bit(1, 4);
  constexpr uint32_t groups = 11'000;
  DynGraph graph;
  graph.set_vertices(groups * vertex_count);
  graph.reserve_edges(groups * 8);
  graph.init_hash(groups * 8);
  std::vector<Op> ops;
  for (uint32_t offset = 0; offset < groups * vertex_count; offset += vertex_count) {
    for (const auto& [u, v] : edges)
      if (edge_bit(u, v) & ((clique | leaf) ^ edge_bit(2, 3))) graph.add_edge(offset + u, offset + v);
    ops.push_back({0, offset + 2, offset + 3});
  }
  BatchMaintainer batch(graph, 4);
  batch.fuse_closure_recount = fused;
  batch.certify_low_support = certified;
  batch.prune_below_four = pruned;
  batch.record_peel_scans = true;
  batch.apply_batch(ops);
  assert(graph.m == 8 * groups && batch.last_region().size() == graph.m);
  for (uint32_t edge : batch.last_region()) {
    Mask bit = edge_bit(graph.eu[edge] % vertex_count, graph.ev[edge] % vertex_count);
    assert(batch.tau[edge] == ((bit & clique) ? 4 : 3));
  }
  assert(batch.stats.parallel_jobs > 0);
  assert(batch.stats.peel_scanned_edges == (certified || pruned ? 6 : 8) * groups);
  assert(batch.stats.peel_scan_degree_sum == (certified || pruned ? 42 : 54) * groups);
  printf("Verified %llu labels on T=4 and exact scan counts, fused=%d certified=%d pruned=%d\n",
         static_cast<unsigned long long>(graph.m), fused, certified, pruned);
}

void test_residual_pruning() {
  DynGraph graph;
  graph.set_vertices(4);
  graph.reserve_edges(5);
  graph.init_hash(5);
  BatchMaintainer batch(graph, 1);
  batch.prune_below_four = true;
  batch.record_peel_scans = true;
  // Two triangles share (0,1), whose initial support is two.
  batch.apply_batch({{0, 0, 1}, {0, 0, 2}, {0, 1, 2}, {0, 0, 3}, {0, 1, 3}});
  assert(batch.stats.peel_certified_edges == 5);
  assert(batch.stats.peel_scanned_edges == 0);
  for (uint32_t edge : batch.last_region()) assert(batch.tau[edge] == 3);
}

int main() {
  for (bool fused : {false, true})
    for (bool certified : {false, true}) {
      if (fused || certified) test_snapshots(fused, certified);
      test_parallel(fused, certified);
    }
  test_snapshots(false, false, true);
  test_parallel(false, false, true);
  test_residual_pruning();
}
