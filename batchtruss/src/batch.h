#pragma once
#include "fixpoint.h"
#include <atomic>
#include <unordered_set>

struct BatchStats {
  uint64_t evals = 0;
  uint64_t seeds = 0;
  uint64_t rounds = 0;
  uint64_t region_max = 0;
  uint64_t region_sum = 0;
  uint64_t deltas = 0;
  uint64_t del_deltas = 0;
  uint64_t ins_deltas = 0;
};

class BatchMaintainer {
public:
  DynGraph& g;
  std::vector<uint16_t> tau;
  std::unique_ptr<std::atomic<uint32_t>[]> sup;
  uint32_t supcap = 0;
  uint32_t T;
  bool ablate_allseeds = false;
  BatchStats stats;

  std::vector<StampSet> stamps;
  std::vector<std::vector<uint16_t>> bufs;
  std::vector<uint8_t> in_next;
  std::vector<uint32_t> frontier, next;
  std::vector<std::vector<uint32_t>> chg_e, nxt_t, seeds_t;
  std::vector<std::vector<uint16_t>> chg_v;
  std::vector<std::vector<std::pair<uint32_t, uint32_t>>> mate_t;

  BatchMaintainer(DynGraph& graph, uint32_t threads, uint64_t reserve_edges = 0)
      : g(graph), T(threads ? threads : 1) {
    uint32_t cap = g.cap_edges;
    if (reserve_edges) {
      uint64_t want = (uint64_t)g.cap_edges + reserve_edges + 16;
      if (want > UINT32_MAX - 1) want = UINT32_MAX - 1;
      cap = (uint32_t)want;
      g.reserve_edges(cap);
    }
    StaticTruss s = static_truss(g, T, &stamps);
    tau.assign(cap, 0);
    for (uint32_t e = 0; e < g.cap_edges; ++e) tau[e] = s.tau[e];
    sup = std::make_unique<std::atomic<uint32_t>[]>(cap);
    supcap = cap;
    for (uint32_t e = 0; e < g.cap_edges; ++e) sup[e].store(s.sup[e]);
    bufs.resize(T);
    chg_e.resize(T);
    chg_v.resize(T);
    nxt_t.resize(T);
    seeds_t.resize(T);
    mate_t.resize(T);
    in_next.assign(cap, 0);
  }

  template <class F>
  void run_jobs(uint64_t count, F body) {
    if (T <= 1 || count < 2) {
      for (uint64_t i = 0; i < count; ++i) body(0, i);
      return;
    }
    std::vector<std::thread> ths;
    std::atomic<uint64_t> job(0);
    auto worker = [&](uint32_t tid) {
      for (;;) {
        uint64_t i = job.fetch_add(1);
        if (i >= count) break;
        body(tid, i);
      }
    };
    for (uint32_t t = 1; t < T; ++t) ths.emplace_back(worker, t);
    worker(0);
    for (auto& t : ths) t.join();
  }

  inline void expand_local(uint32_t tid, uint32_t eid) {
    uint32_t a = g.eu[eid], b = g.ev[eid];
    for (auto& r : g.adj[a])
      if (!in_next[r.eid]) {
        in_next[r.eid] = 1;
        nxt_t[tid].push_back(r.eid);
      }
    for (auto& r : g.adj[b])
      if (!in_next[r.eid]) {
        in_next[r.eid] = 1;
        nxt_t[tid].push_back(r.eid);
      }
  }

  void fixpoint(bool promote, BatchStats& st) {
    uint32_t round = 0;
    while (!frontier.empty()) {
      round++;
      if (st.region_max < frontier.size()) st.region_max = frontier.size();
      st.region_sum += frontier.size();
      st.evals += frontier.size();
      for (uint32_t t = 0; t < T; ++t) {
        chg_e[t].clear();
        chg_v[t].clear();
        nxt_t[t].clear();
      }
      uint64_t fsz = frontier.size();
      run_jobs(fsz, [&](uint32_t tid, uint64_t i) {
        uint32_t eid = frontier[i];
        if (!g.alive(eid)) return;
        uint16_t nt = eval_trussness(g.eu[eid], g.ev[eid], g, tau, stamps[tid], bufs[tid]);
        if (promote ? (nt > tau[eid]) : (nt < tau[eid])) {
          chg_e[tid].push_back(eid);
          chg_v[tid].push_back(nt);
        }
      });
      next.clear();
      for (uint32_t t = 0; t < T; ++t)
        for (size_t i = 0; i < chg_e[t].size(); ++i) {
          uint32_t eid = chg_e[t][i];
          tau[eid] = chg_v[t][i];
          expand_local(t, eid);
        }
      for (uint32_t e : frontier) in_next[e] = 0;
      for (uint32_t t = 0; t < T; ++t)
        for (uint32_t e : nxt_t[t]) next.push_back(e);
      std::swap(frontier, next);
      if (round > 100000) {
        fprintf(stderr, "fixpoint round overflow\n");
        exit(1);
      }
    }
    st.rounds += round;
  }

  void build_frontier(std::vector<uint32_t>& seeds, BatchStats& st) {
    for (uint32_t e : seeds) {
      if (!g.alive(e)) {
        in_next[e] = 0;
        continue;
      }
      if (!in_next[e]) {
        in_next[e] = 1;
        frontier.push_back(e);
      }
    }
    seeds.clear();
    st.seeds += frontier.size();
  }

  bool apply_batch(const std::vector<Op>& raw) {
    std::unordered_set<uint64_t> delset, insset;
    for (auto& op : raw) {
      if (op.u == op.v) continue;
      uint64_t k = DynGraph::ekey(op.u, op.v);
      if (op.del) {
        if (g.has(op.u, op.v)) delset.insert(k);
      } else {
        if (!g.has(op.u, op.v)) insset.insert(k);
      }
    }
    std::vector<uint32_t> dels;
    for (uint64_t k : delset) {
      if (insset.count(k)) continue;
      int64_t eid = g.find(k);
      if (eid >= 0) dels.push_back((uint32_t)eid);
    }
    std::vector<std::pair<uint32_t, uint32_t>> ins;
    for (uint64_t k : insset) {
      if (delset.count(k)) continue;
      ins.push_back({(uint32_t)(k >> 32), (uint32_t)(k & 0xffffffffULL)});
    }
    stats.deltas += dels.size() + ins.size();
    stats.del_deltas += dels.size();
    stats.ins_deltas += ins.size();
    if (!dels.empty()) phase_deletions(dels);
    if (!ins.empty()) phase_insertions(ins);
    return true;
  }

  bool apply_batch_nomerge(const std::vector<Op>& raw) {
    bool ok = true;
    for (auto& op : raw) {
      std::vector<Op> one = {op};
      ok = apply_batch(one) && ok;
    }
    return ok;
  }

  void phase_deletions(std::vector<uint32_t>& dels) {
    for (uint32_t t = 0; t < T; ++t) seeds_t[t].clear();
    run_jobs(dels.size(), [&](uint32_t tid, uint64_t i) {
      uint32_t e = dels[i];
      if (!g.alive(e)) return;
      uint32_t u = g.eu[e], v = g.ev[e];
      g.common_neighbors(u, v, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        uint32_t o1 = sup[e1].fetch_sub(1);
        uint32_t o2 = sup[e2].fetch_sub(1);
        if (o1 == 0 || o2 == 0) {
          fprintf(stderr, "support underflow\n");
          exit(1);
        }
        seeds_t[tid].push_back(e1);
        seeds_t[tid].push_back(e2);
      });
      if (ablate_allseeds) {
        for (auto& r : g.adj[u]) seeds_t[tid].push_back(r.eid);
        for (auto& r : g.adj[v]) seeds_t[tid].push_back(r.eid);
      }
    });
    for (uint32_t e : dels) {
      if (!g.alive(e)) continue;
      g.remove_edge(e);
      tau[e] = 0;
      sup[e].store(0);
    }
    frontier.clear();
    for (uint32_t t = 0; t < T; ++t) build_frontier(seeds_t[t], stats);
    fixpoint(false, stats);
  }

  void phase_insertions(std::vector<std::pair<uint32_t, uint32_t>>& ins) {
    uint64_t B = ins.size();
    std::vector<uint32_t> neweids(B);
    for (uint64_t i = 0; i < B; ++i) {
      uint32_t u = ins[i].first, v = ins[i].second;
      if (u > v) std::swap(u, v);
      uint32_t eid;
      if (!g.free_ids.empty()) {
        eid = g.free_ids.back();
        g.free_ids.pop_back();
      } else {
        eid = g.cap_edges++;
        if (eid >= supcap || eid >= g.eu.size()) grow_structures(eid);
      }
      g.eu[eid] = u;
      g.ev[eid] = v;
      g.slot0[eid] = (uint32_t)g.adj[u].size();
      g.slot1[eid] = (uint32_t)g.adj[v].size();
      g.adj[u].push_back({v, eid});
      g.adj[v].push_back({u, eid});
      g.hash_insert(DynGraph::ekey(u, v), eid + 1);
      g.m++;
      tau[eid] = 2;
      sup[eid].store(0);
      in_next[eid] = 0;
      neweids[i] = eid;
    }
    std::vector<uint32_t> wsize(B, 0);
    for (uint32_t t = 0; t < T; ++t) {
      seeds_t[t].clear();
      mate_t[t].clear();
    }
    run_jobs(B, [&](uint32_t tid, uint64_t i) {
      uint32_t eid = neweids[i];
      uint32_t u = g.eu[eid], v = g.ev[eid];
      uint32_t cnt = g.common_neighbors(u, v, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        seeds_t[tid].push_back(e1);
        seeds_t[tid].push_back(e2);
        mate_t[tid].push_back({e1, e2});
      });
      wsize[i] = cnt;
      uint16_t nt = eval_trussness(u, v, g, tau, stamps[tid], bufs[tid]);
      tau[eid] = nt;
      if (ablate_allseeds) {
        for (auto& r : g.adj[u]) seeds_t[tid].push_back(r.eid);
        for (auto& r : g.adj[v]) seeds_t[tid].push_back(r.eid);
      }
    });
    run_jobs(B, [&](uint32_t, uint64_t i) { sup[neweids[i]].store(wsize[i]); });
    std::vector<std::pair<uint32_t, uint32_t>> allmates;
    for (uint32_t t = 0; t < T; ++t)
      for (auto& p : mate_t[t]) allmates.push_back(p);
    run_jobs(allmates.size(), [&](uint32_t, uint64_t i) {
      sup[allmates[i].first].fetch_add(1);
      sup[allmates[i].second].fetch_add(1);
    });
    frontier.clear();
    for (uint32_t i = 0; i < B; ++i) {
      uint32_t eid = neweids[i];
      if (!in_next[eid]) {
        in_next[eid] = 1;
        frontier.push_back(eid);
      }
    }
    for (uint32_t t = 0; t < T; ++t) build_frontier(seeds_t[t], stats);
    fixpoint(true, stats);
  }

  void grow_structures(uint32_t eid) {
    size_t nsz = (size_t)std::max<uint64_t>(eid + 1, (g.eu.size() ? g.eu.size() * 2 : 1024));
    g.eu.resize(nsz, UINT32_MAX);
    g.ev.resize(nsz, UINT32_MAX);
    g.slot0.resize(nsz, 0);
    g.slot1.resize(nsz, 0);
    in_next.resize(nsz, 0);
    tau.resize(nsz, 0);
    auto nsup = std::make_unique<std::atomic<uint32_t>[]>(nsz);
    for (size_t e = 0; e < supcap && e < nsz; ++e) nsup[e].store(sup[e].load());
    sup = std::move(nsup);
    supcap = (uint32_t)nsz;
  }
};
