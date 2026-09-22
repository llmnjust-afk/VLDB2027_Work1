#pragma once
#include "graph.h"
#include "common.h"

struct StaticTruss {
  std::vector<uint16_t> tau;
  std::vector<uint32_t> sup;
};

inline void compute_supports_seq(const DynGraph& g, std::vector<uint32_t>& sup, StampSet& st) {
  sup.assign(g.cap_edges, 0);
  for (uint32_t u = 0; u < g.n; ++u) {
    if (g.adj[u].empty()) continue;
    st.next_epoch();
    for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
    for (auto& r : g.adj[u]) {
      if (g.eu[r.eid] != u) continue;
      uint32_t c = 0;
      for (auto& r2 : g.adj[r.nbr])
        if (st.test(r2.nbr)) ++c;
      sup[r.eid] = c;
    }
  }
}

inline void compute_supports(const DynGraph& g, std::vector<uint32_t>& sup,
                             std::vector<StampSet>& sts, uint32_t nthreads) {
  uint32_t T = nthreads ? nthreads : 1;
  if (T == 1) {
    compute_supports_seq(g, sup, sts[0]);
    return;
  }
  sup.assign(g.cap_edges, 0);
  std::vector<std::thread> ths;
  std::atomic<uint64_t> job(0);
  auto worker = [&](uint32_t tid) {
    StampSet& st = sts[tid];
    for (;;) {
      uint64_t i = job.fetch_add(1);
      if (i >= g.n) break;
      uint32_t u = (uint32_t)i;
      if (g.adj[u].empty()) continue;
      st.next_epoch();
      for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
      for (auto& r : g.adj[u]) {
        if (g.eu[r.eid] != u) continue;
        uint32_t c = 0;
        for (auto& r2 : g.adj[r.nbr])
          if (st.test(r2.nbr)) ++c;
        sup[r.eid] = c;
      }
    }
  };
  for (uint32_t t = 1; t < T; ++t) ths.emplace_back(worker, t);
  worker(0);
  for (auto& t : ths) t.join();
}

inline StaticTruss static_truss(const DynGraph& g, uint32_t nthreads = 1,
                                std::vector<StampSet>* stamp_pool = nullptr) {
  StaticTruss out;
  out.tau.assign(g.cap_edges, 0);
  uint32_t T = nthreads ? nthreads : 1;
  std::vector<StampSet> local;
  std::vector<StampSet>& sts = stamp_pool ? *stamp_pool : local;
  if (sts.size() < T) {
    sts.resize(T);
    for (auto& x : sts) x.init(g.n);
  }
  std::vector<uint32_t> sup;
  if (T > 1)
    compute_supports(g, sup, sts, T);
  else
    compute_supports_seq(g, sup, sts[0]);
  out.sup = sup;

  uint32_t maxsup = 0;
  for (uint32_t e = 0; e < g.cap_edges; ++e)
    if (g.alive(e) && sup[e] > maxsup) maxsup = sup[e];
  std::vector<std::vector<uint32_t>> buckets(maxsup + 1);
  for (uint32_t e = 0; e < g.cap_edges; ++e)
    if (g.alive(e)) buckets[sup[e]].push_back(e);
  std::vector<uint8_t> alive(g.cap_edges, 1);

  StampSet st = sts[0];
  for (uint32_t s = 0; s <= maxsup; ++s) {
    auto& b = buckets[s];
    for (size_t i = 0; i < b.size(); ++i) {
      uint32_t eid = b[i];
      if (!alive[eid] || sup[eid] != s) continue;
      out.tau[eid] = (uint16_t)(s + 2);
      alive[eid] = 0;
      if (BT_WATCH >= 0) fprintf(stderr, "[spop] s=%u eid=%u sup=%u\n", s, eid, sup[eid]);
      uint32_t u = g.eu[eid], v = g.ev[eid];
      st.next_epoch();
      for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
      for (auto& r : g.adj[v]) {
        if (!st.test(r.nbr)) continue;
        uint32_t e1 = st.marked_eid(r.nbr), e2 = r.eid;
        if (!alive[e1] || !alive[e2]) continue;
        if (sup[e1] > s) {
          sup[e1]--;
          buckets[sup[e1]].push_back(e1);
        }
        if (sup[e2] > s) {
          sup[e2]--;
          buckets[sup[e2]].push_back(e2);
        }
      }
    }
  }
  return out;
}
