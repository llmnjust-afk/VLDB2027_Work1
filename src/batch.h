#pragma once
#include "batch_updates.h"
#include "replay.h"
#include <condition_variable>
#include <functional>
#include <memory>
#include <mutex>

struct BatchStats {
  uint64_t evals = 0;
  uint64_t seeds = 0;
  uint64_t rounds = 0;
  uint64_t region_max = 0;
  uint64_t deleted = 0;
  uint64_t inserted = 0;
  uint64_t peels = 0;
  uint64_t virtual_events = 0;
  uint64_t parallel_jobs = 0;
  uint64_t peel_certified_edges = 0;
  uint64_t peel_scanned_edges = 0;
  uint64_t peel_scan_degree_sum = 0;
  uint64_t region_degree_sum = 0;
};

inline void apply_ordered_updates(DynGraph& graph, const std::vector<Op>& operations,
                                  BatchStats& stats) {
  for (const auto& op : operations)
    if (replay_edge(graph, op)) ++(op.del ? stats.deleted : stats.inserted);
}

enum class BatchRoute { Fused, Static };

struct BatchPhaseTimes {
  double coalesce_s = 0;
  double witness_s = 0;
  double apply_s = 0;
  double closure_s = 0;
  double recount_s = 0;
  double peel_s = 0;
};

class BatchMaintainer {
public:
  std::vector<uint32_t> tau;
  BatchStats stats;
  bool ablate_allseeds = false;
  bool certify_low_support = false;
  bool fuse_closure_recount = false;
  bool prune_below_four = false;
  bool record_peel_scans = false;
  bool record_region_degrees = false;

  BatchMaintainer(DynGraph& graph, uint32_t threads)
      : g(graph), T(threads ? threads : 1), local(T) {
    tau = static_truss(g, T, &stamps).tau;
    ensure_capacity();
    pool_start();
  }

  ~BatchMaintainer() { pool_shutdown(); }

  const std::vector<uint32_t>& last_region() const { return region; }

  void apply_batch(const std::vector<Op>& raw, BatchPhaseTimes* phases = nullptr) {
    PhaseClock clock(phases != nullptr);
    const auto updates = coalesce_batch_updates(g, raw);
    if (phases) phases->coalesce_s = clock.lap();
    apply_delta(updates.deletes, updates.inserts, phases);
  }

  BatchRoute apply_adaptive(const std::vector<Op>& raw, double& kernel_s) {
    if (static_checkpoints_left != 0) {
      --static_checkpoints_left;
      apply_routed_batch(raw, BatchRoute::Static, kernel_s);
      return BatchRoute::Static;
    }
    apply_routed_batch(raw, BatchRoute::Fused, kernel_s);
    if (region.size() > g.m / 2) static_checkpoints_left = 8;
    return BatchRoute::Fused;
  }

  void apply_routed_batch(const std::vector<Op>& raw, BatchRoute route, double& kernel_s) {
    if (route == BatchRoute::Static) {
      apply_ordered_updates(g, raw, stats);
      const double kernel_start = now_s();
      tau = static_truss(g, T).tau;
      kernel_s = now_s() - kernel_start;
      ++stats.peels;
      region.clear();
      return;
    }

    // Static checkpoints may grow the edge slots or replace tau with a
    // vector shorter than the already allocated regional scratch arrays.
    ensure_capacity();
    tau.resize(mark_capacity);
    fuse_closure_recount = true;
    apply_batch(raw);
  }

  void apply_batch_nomerge(const std::vector<Op>& raw) {
    for (const auto& op : raw) {
      if (op.u == op.v) continue;
      uint64_t key = DynGraph::ekey(op.u, op.v);
      int64_t eid = g.find(key);
      if (op.del && eid >= 0) apply_delta({static_cast<uint32_t>(eid)}, {});
      if (!op.del && eid < 0) apply_delta({}, {key});
    }
  }

private:
  DynGraph& g;
  uint32_t T;
  std::vector<StampSet> stamps;
  std::vector<std::vector<uint32_t>> local;
  std::vector<uint32_t> region;
  std::vector<uint64_t> region_degrees;
  std::vector<uint32_t> support;
  std::vector<uint8_t> alive;
  std::vector<uint64_t> triangle_xor;
  std::vector<uint32_t> low_support_edges;
  struct SupportOneTriangle {
    uint32_t edge;
    uint32_t mate1;
    uint32_t mate2;
  };
  std::vector<std::vector<SupportOneTriangle>> support_one_triangles;
  std::unique_ptr<std::atomic<uint8_t>[]> marked;
  size_t mark_capacity = 0;
  uint32_t static_checkpoints_left = 0;

  std::vector<std::thread> pool_ths;
  std::mutex pool_mx;
  std::condition_variable pool_cv;
  struct {
    alignas(64) std::atomic<uint64_t> seq{0};
    alignas(64) std::atomic<uint64_t> job{0};
    alignas(64) std::atomic<uint64_t> done{0};
    alignas(64) std::atomic<bool> stop{false};
    alignas(64) std::atomic<uint64_t> count{0};
  } P;
  std::atomic<uint64_t>& pool_seq = P.seq;
  std::atomic<uint64_t>& pool_job = P.job;
  std::atomic<uint64_t>& pool_done = P.done;
  std::atomic<bool>& pool_stop = P.stop;
  std::atomic<uint64_t>& pool_count = P.count;
  std::function<void(uint32_t, uint64_t)> pool_body = nullptr;

  void pool_worker(uint32_t tid) {
    uint64_t seen = 0;
    for (;;) {
      {
        std::unique_lock<std::mutex> lk(pool_mx);
        while (pool_seq.load(std::memory_order_acquire) == seen && !pool_stop.load())
          pool_cv.wait(lk);
      }
      if (pool_stop.load()) return;
      seen = pool_seq.load(std::memory_order_acquire);
      uint64_t n = pool_count.load(std::memory_order_acquire);
      for (;;) {
        uint64_t i0 = pool_job.fetch_add(64);
        if (i0 >= n) break;
        uint64_t iend = i0 + 64;
        if (iend > n) iend = n;
        for (uint64_t i = i0; i < iend; ++i) pool_body(tid, i);
      }
      pool_done.fetch_add(1, std::memory_order_release);
    }
  }

  void pool_start() {
    for (uint32_t t = 1; t < T; ++t)
      pool_ths.emplace_back([this, t] { pool_worker(t); });
  }

  void pool_shutdown() {
    {
      std::lock_guard<std::mutex> lk(pool_mx);
      pool_stop.store(true, std::memory_order_release);
      pool_cv.notify_all();
    }
    for (auto& t : pool_ths) if (t.joinable()) t.join();
    pool_ths.clear();
  }

  template <class F>
  void run_jobs(uint64_t count, F body) {
    if (T <= 1 || count < 2 || count < 32768) {
      for (uint64_t i = 0; i < count; ++i) body(0, i);
      return;
    }
    ++stats.parallel_jobs;
    pool_body = body;
    {
      std::lock_guard<std::mutex> lk(pool_mx);
      pool_count.store(count, std::memory_order_release);
      pool_job.store(0, std::memory_order_release);
      pool_done.store(0, std::memory_order_relaxed);
      pool_seq.fetch_add(1, std::memory_order_release);
      pool_cv.notify_all();
    }
    for (;;) {
      uint64_t i0 = pool_job.fetch_add(64);
      if (i0 >= count) break;
      uint64_t iend = i0 + 64;
      if (iend > count) iend = count;
      for (uint64_t i = i0; i < iend; ++i) body(0, i);
    }
    while (pool_done.load(std::memory_order_acquire) != T - 1)
      std::this_thread::yield();
  }

  void ensure_capacity() {
    if (g.cap_edges <= mark_capacity) return;
    mark_capacity = g.eu.size();
    tau.resize(mark_capacity);
    marked = std::make_unique<std::atomic<uint8_t>[]>(mark_capacity);
    for (size_t i = 0; i < mark_capacity; ++i)
      marked[i].store(0, std::memory_order_relaxed);
    support.resize(mark_capacity);
    alive.resize(mark_capacity);
  }

  void mark_once(uint32_t edge, std::vector<uint32_t>& out) {
    if (!marked[edge].load(std::memory_order_relaxed) &&
        !marked[edge].exchange(1, std::memory_order_relaxed))
      out.push_back(edge);
  }

  void apply_delta(const std::vector<uint32_t>& deletes,
                   const std::vector<uint64_t>& inserts,
                   BatchPhaseTimes* phases = nullptr) {
    PhaseClock clock(phases != nullptr);
    stats.deleted += deletes.size();
    stats.inserted += inserts.size();
    for (auto& edges : local) edges.clear();

    // Deletion witnesses must be collected on the pre-update graph.
    run_jobs(deletes.size(), [&](uint32_t tid, uint64_t i) {
      uint32_t edge = deletes[i];
      uint32_t u = g.eu[edge], v = g.ev[edge];
      g.common_neighbors(u, v, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        mark_once(e1, local[tid]);
        mark_once(e2, local[tid]);
      });
      if (ablate_allseeds) {
        for (const auto& ref : g.adj[u]) mark_once(ref.eid, local[tid]);
        for (const auto& ref : g.adj[v]) mark_once(ref.eid, local[tid]);
      }
    });
    if (phases) phases->witness_s = clock.lap();

    region.clear();
    for (auto& edges : local)
      for (uint32_t edge : edges) marked[edge].store(0, std::memory_order_relaxed);
    apply_batch_deletions(g, deletes, [&](uint32_t edge) { tau[edge] = 0; });
    // Filter before adding edges because the graph reuses deleted edge IDs.
    for (auto& edges : local)
      for (uint32_t edge : edges)
        if (g.alive(edge)) region.push_back(edge);
    apply_batch_insertions(g, inserts, [&](uint32_t edge) { region.push_back(edge); });
    ensure_capacity();
    if (phases) phases->apply_s = clock.lap();
    if (region.empty()) return;

    stats.seeds += region.size();
    for (uint32_t edge : region) marked[edge].store(1, std::memory_order_relaxed);
    expand_region();
    if (phases) phases->closure_s = clock.lap();
    peel_region(phases);
    for (uint32_t edge : region) marked[edge].store(0, std::memory_order_relaxed);
  }

  static uint64_t pack_mates(uint32_t first, uint32_t second) {
    return (uint64_t(std::min(first, second)) << 32) | std::max(first, second);
  }

  // Specialize away census work in the standard expansion used by Full.
  template <bool CollectSupports, bool Certify, bool Prune = false, bool CollectDegrees = false>
  void expand_frontier(size_t begin, size_t end) {
    run_jobs(end - begin, [&](uint32_t tid, uint64_t i) {
      uint32_t edge = region[begin + i];
      uint32_t u = g.eu[edge], v = g.ev[edge];
      if constexpr (CollectDegrees)
        region_degrees[tid] += uint64_t(g.adj[u].size()) + g.adj[v].size();
      SupportOneTriangle triangle{edge, 0, 0};
      uint64_t signature = 0;
      uint32_t count = g.common_neighbors(u, v, stamps[tid],
                                          [&](uint32_t, uint32_t mate1, uint32_t mate2) {
        mark_once(mate1, local[tid]);
        mark_once(mate2, local[tid]);
        if constexpr (Certify) {
          triangle.mate1 = mate1;
          triangle.mate2 = mate2;
        }
        if constexpr (Prune) signature ^= pack_mates(mate1, mate2);
      });
      // This scan runs once for each closure edge on the immutable final
      // graph, so its count matches a separate support recount on that graph.
      if constexpr (Prune) {
        support[edge] = count;
        triangle_xor[edge] = signature;
        alive[edge] = count != 0;
        if (!alive[edge]) tau[edge] = 2;
      } else if constexpr (Certify) {
        save_certified_support(tid, count, triangle);
      } else if constexpr (CollectSupports) {
        support[edge] = count;
        alive[edge] = 1;
      }
    });
  }

  void expand_region() {
    if (fuse_closure_recount && certify_low_support) prepare_support_one_triangles();
    if (prune_below_four) triangle_xor.resize(mark_capacity);
    if (record_region_degrees) region_degrees.assign(T, 0);
    size_t begin = 0;
    while (begin < region.size()) {
      size_t end = region.size();
      ++stats.rounds;
      stats.evals += end - begin;
      for (auto& edges : local) edges.clear();
      if (prune_below_four) {
        if (record_region_degrees) expand_frontier<true, false, true, true>(begin, end);
        else expand_frontier<true, false, true>(begin, end);
      } else if (fuse_closure_recount) {
        if (certify_low_support) expand_frontier<true, true>(begin, end);
        else if (record_region_degrees) expand_frontier<true, false, false, true>(begin, end);
        else expand_frontier<true, false>(begin, end);
      } else {
        expand_frontier<false, false>(begin, end);
      }
      for (auto& edges : local) region.insert(region.end(), edges.begin(), edges.end());
      begin = end;
    }
    if (record_region_degrees)
      for (uint64_t subtotal : region_degrees) stats.region_degree_sum += subtotal;
    stats.region_max = std::max<uint64_t>(stats.region_max, region.size());
  }

  void prepare_support_one_triangles() {
    support_one_triangles.resize(T);
    for (auto& triangles : support_one_triangles) triangles.clear();
  }

  void save_certified_support(uint32_t tid, uint32_t count,
                              const SupportOneTriangle& triangle) {
    uint32_t edge = triangle.edge;
    support[edge] = count;
    // A triangle gives its edges a lower bound of 3. Raw support 0 or 1
    // makes support + 2 the exact label without an adjacency scan in the peel.
    alive[edge] = count >= 2;
    if (!alive[edge]) tau[edge] = count + 2;
    if (count == 1) support_one_triangles[tid].push_back(triangle);
  }

  void recount_region() {
    if (certify_low_support) {
      prepare_support_one_triangles();
      run_jobs(region.size(), [&](uint32_t tid, uint64_t i) {
        uint32_t edge = region[i];
        SupportOneTriangle triangle{edge, 0, 0};
        uint32_t count = g.common_neighbors(g.eu[edge], g.ev[edge], stamps[tid],
                                            [&](uint32_t, uint32_t mate1, uint32_t mate2) {
          triangle.mate1 = mate1;
          triangle.mate2 = mate2;
        });
        save_certified_support(tid, count, triangle);
      });
      return;
    }
    run_jobs(region.size(), [&](uint32_t tid, uint64_t i) {
      uint32_t edge = region[i];
      support[edge] = g.common_neighbors(g.eu[edge], g.ev[edge], stamps[tid],
                                        [](uint32_t, uint32_t, uint32_t) {});
      alive[edge] = 1;
    });
  }

  void remove_certified_triangles() {
    for (const auto& triangles : support_one_triangles) {
      for (const auto& triangle : triangles) {
        const auto [edge, mate1, mate2] = triangle;
        // All initially certified edges have been marked dead. Assign each
        // triangle to the lowest-ID certified edge so it affects live mates once.
        if ((!alive[mate1] && mate1 < edge) || (!alive[mate2] && mate2 < edge)) continue;
        for (uint32_t mate : {mate1, mate2})
          if (alive[mate] && support[mate] > 1) --support[mate];
      }
    }
  }

  void prune_to_four_truss() {
    low_support_edges.clear();
    for (uint32_t edge : region)
      if (support[edge] == 1) low_support_edges.push_back(edge);

    // Live supports and XOR signatures describe the same residual triangles.
    // A support-one edge exposes its last two mates without an intersection.
    for (size_t i = 0; i < low_support_edges.size(); ++i) {
      const uint32_t edge = low_support_edges[i];
      alive[edge] = 0;
      tau[edge] = 3;  // All queued edges had positive support in the final graph.
      if (support[edge] == 0) continue;
      const uint64_t signature = triangle_xor[edge];
      const uint32_t mate1 = uint32_t(signature >> 32), mate2 = uint32_t(signature);
      const auto remove_triangle = [&](uint32_t mate, uint32_t other) {
        triangle_xor[mate] ^= pack_mates(edge, other);
        if (--support[mate] == 1) low_support_edges.push_back(mate);
      };
      remove_triangle(mate1, mate2);
      remove_triangle(mate2, mate1);
    }
  }

  void peel_region(BatchPhaseTimes* phases) {
    PhaseClock clock(phases != nullptr);
    ++stats.peels;
    const bool recount = !fuse_closure_recount && !prune_below_four;
    if (recount) {
      recount_region();
      if (phases) phases->recount_s = clock.lap();
    } else if (phases) {
      phases->recount_s = 0;
    }
    if (prune_below_four) prune_to_four_truss();
    else if (certify_low_support) remove_certified_triangles();

    uint32_t max_support = 0;
    for (uint32_t edge : region) max_support = std::max(max_support, support[edge]);
    std::vector<std::vector<uint32_t>> buckets(max_support + 1);
    uint64_t scanned_edges = region.size();
    if (certify_low_support || prune_below_four) {
      scanned_edges = 0;
      for (uint32_t edge : region) {
        if (!alive[edge]) continue;
        buckets[support[edge]].push_back(edge);
        ++scanned_edges;
      }
    } else {
      for (uint32_t edge : region) buckets[support[edge]].push_back(edge);
    }
    stats.evals += scanned_edges;
    if (recount) stats.evals += region.size();
    if (record_peel_scans) {
      stats.peel_certified_edges += region.size() - scanned_edges;
      stats.peel_scanned_edges += scanned_edges;
      for (uint32_t edge : region)
        if (alive[edge])
          stats.peel_scan_degree_sum += uint64_t(g.adj[g.eu[edge]].size()) + g.adj[g.ev[edge]].size();
    }

    for (uint32_t level = 0; level <= max_support; ++level) {
      for (size_t i = 0; i < buckets[level].size(); ++i) {
        uint32_t edge = buckets[level][i];
        if (!alive[edge] || support[edge] != level) continue;
        tau[edge] = level + 2;
        alive[edge] = 0;
        g.common_neighbors(g.eu[edge], g.ev[edge], stamps[0],
                           [&](uint32_t, uint32_t e1, uint32_t e2) {
          if (!alive[e1] || !alive[e2]) return;
          for (uint32_t mate : {e1, e2})
            if (support[mate] > level)
              buckets[--support[mate]].push_back(mate);
        });
      }
    }
    // Every triangle touching the closed region is internal, so this sweep
    // has no external removals to schedule as virtual events.
    if (phases) phases->peel_s = clock.lap();
  }
};
