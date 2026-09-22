#pragma once
#include "fixpoint.h"

struct IncStats {
  uint64_t evals = 0;
  uint64_t region_max = 0;
  uint64_t region_sum = 0;
};

struct WRec {
  uint32_t eid_uw, eid_vw;
};

class PerEdgeMaintainer {
public:
  DynGraph& g;
  std::vector<uint16_t> tau;
  std::vector<uint32_t> sup;
  StampSet st;
  std::vector<uint16_t> mbuf;
  std::vector<uint32_t> queue;
  std::vector<uint8_t> inq;
  std::vector<WRec> wlist;
  IncStats stats;

  explicit PerEdgeMaintainer(DynGraph& graph) : g(graph) {
    StaticTruss s = static_truss(g, 1);
    tau = s.tau;
    sup = s.sup;
    inq.assign(g.cap_edges, 0);
  }

  inline void ensure_capacity(uint32_t eid) {
    if (eid >= tau.size()) {
      size_t nsz = (size_t)std::max<uint64_t>(eid + 1, tau.size() * 2 + 1024);
      tau.resize(nsz, 0);
      sup.resize(nsz, 0);
      inq.resize(nsz, 0);
    }
  }

  inline void push_incident(uint32_t x) {
    for (auto& r : g.adj[x])
      if (!inq[r.eid]) {
        inq[r.eid] = 1;
        queue.push_back(r.eid);
      }
  }

  void fixpoint(bool promote) {
    size_t head = 0;
    size_t region = queue.size();
    while (head < queue.size()) {
      uint32_t eid = queue[head++];
      inq[eid] = 0;
      if (!g.alive(eid)) continue;
      stats.evals++;
      uint16_t nt = eval_trussness(g.eu[eid], g.ev[eid], g, tau, st, mbuf);
      bool changed = promote ? (nt > tau[eid]) : (nt < tau[eid]);
      if (changed) {
        tau[eid] = nt;
        push_incident(g.eu[eid]);
        push_incident(g.ev[eid]);
      }
    }
    if (region > stats.region_max) stats.region_max = region;
    stats.region_sum += region;
    queue.clear();
  }

  bool insert(uint32_t u, uint32_t v) {
    if (u == v || g.has(u, v)) return false;
    wlist.clear();
    g.common_neighbors(u, v, st, [&](uint32_t, uint32_t e1, uint32_t e2) {
      wlist.push_back({e1, e2});
    });
    uint32_t eid = g.add_edge(u, v);
    ensure_capacity(eid);
    sup[eid] = (uint32_t)wlist.size();
    mbuf.clear();
    for (auto& w : wlist)
      mbuf.push_back((uint16_t)std::min(tau[w.eid_uw], tau[w.eid_vw]));
    std::sort(mbuf.begin(), mbuf.end(), std::greater<uint16_t>());
    uint16_t best = 2;
    for (size_t j = 0; j < mbuf.size(); ++j) {
      uint16_t cand = (uint16_t)std::min<uint32_t>(mbuf[j], (uint32_t)j + 3);
      if (cand > best) best = cand;
    }
    tau[eid] = best;
    inq[eid] = 1;
    queue.push_back(eid);
    for (auto& w : wlist) {
      sup[w.eid_uw]++;
      sup[w.eid_vw]++;
      if (!inq[w.eid_uw]) {
        inq[w.eid_uw] = 1;
        queue.push_back(w.eid_uw);
      }
      if (!inq[w.eid_vw]) {
        inq[w.eid_vw] = 1;
        queue.push_back(w.eid_vw);
      }
    }
    fixpoint(true);
    return true;
  }

  bool remove(uint32_t u, uint32_t v) {
    if (u == v) return false;
    int64_t eid = g.find(DynGraph::ekey(u, v));
    if (eid < 0) return false;
    uint32_t e = (uint32_t)eid;
    wlist.clear();
    g.common_neighbors(u, v, st, [&](uint32_t, uint32_t e1, uint32_t e2) {
      wlist.push_back({e1, e2});
    });
    for (auto& w : wlist) {
      if (sup[w.eid_uw] > 0) sup[w.eid_uw]--;
      if (sup[w.eid_vw] > 0) sup[w.eid_vw]--;
      if (!inq[w.eid_uw]) {
        inq[w.eid_uw] = 1;
        queue.push_back(w.eid_uw);
      }
      if (!inq[w.eid_vw]) {
        inq[w.eid_vw] = 1;
        queue.push_back(w.eid_vw);
      }
    }
    g.remove_edge(e);
    tau[e] = 0;
    sup[e] = 0;
    fixpoint(false);
    return true;
  }
};
