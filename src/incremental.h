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

static int64_t WATCH_EID = -1;

class PerEdgeMaintainer {
public:
  DynGraph& g;
  std::vector<uint16_t> tau;
  std::vector<uint32_t> sup;
  StampSet st;
  std::vector<uint16_t> mbuf;
  std::vector<uint32_t> queue;
  std::vector<uint8_t> inq;
  std::vector<uint32_t> ubc;
  std::vector<uint8_t> inreg;
  std::vector<uint32_t> region;
  std::vector<uint32_t> risen;
  std::vector<WRec> wlist;
  IncStats stats;

  explicit PerEdgeMaintainer(DynGraph& graph) : g(graph) {
    st.init(g.n);
    StaticTruss s = static_truss(g, 1);
    tau = s.tau;
    sup = s.sup;
    inq.assign(g.cap_edges, 0);
    ubc.assign(g.cap_edges, 0);
    inreg.assign(g.cap_edges, 0);
  }

  inline void ensure_capacity(uint32_t eid) {
    if (eid >= tau.size()) {
      size_t nsz = (size_t)std::max<uint64_t>(eid + 1, tau.size() * 2 + 1024);
      tau.resize(nsz, 0);
      sup.resize(nsz, 0);
      inq.resize(nsz, 0);
      ubc.resize(nsz, 0);
      inreg.resize(nsz, 0);
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
    size_t region_n = queue.size();
    while (head < queue.size()) {
      uint32_t eid = queue[head++];
      inq[eid] = 0;
      if (!g.alive(eid)) continue;
      stats.evals++;
      uint16_t nt = eval_trussness(g.eu[eid], g.ev[eid], g, tau, st, mbuf);
      if ((int64_t)eid == WATCH_EID) {
        fprintf(stderr, "[demote-trace] eid=%u (%u,%u) tau=%u F=%u mbuf:", eid, g.eu[eid], g.ev[eid],
                (unsigned)tau[eid], (unsigned)nt);
        for (auto& m : mbuf) fprintf(stderr, " %u", (unsigned)m);
        fprintf(stderr, "\n");
      }
      bool changed = promote ? (nt > tau[eid]) : (nt < tau[eid]);
      if (changed) {
        tau[eid] = nt;
        push_incident(g.eu[eid]);
        push_incident(g.ev[eid]);
      }
    }
    if (region_n > stats.region_max) stats.region_max = region_n;
    stats.region_sum += region_n;
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
    uint32_t W = (uint32_t)wlist.size();
    sup[eid] = W;
    uint32_t k0 = 0;
    for (auto& w : wlist) {
      uint32_t a = tau[w.eid_uw], b = tau[w.eid_vw];
      uint32_t mmn = a < b ? a : b;
      if (mmn > k0) k0 = mmn;
    }
    uint32_t ce = (W == 0) ? 2u : std::min(W + 2, k0 + 1);
    ubc[eid] = ce;
    tau[eid] = (uint16_t)ce;
    inreg[eid] = 1;
    region.push_back(eid);
    risen.push_back(eid);
    queue.clear();
    for (auto& w : wlist) {
      uint32_t f = w.eid_uw;
      sup[f]++;
      if (!inreg[f]) {
        uint32_t cf = std::min((uint32_t)sup[f] + 2, (uint32_t)tau[f] + 1);
        if (cf > tau[f]) {
          ubc[f] = cf;
          tau[f] = (uint16_t)cf;
          inreg[f] = 1;
          region.push_back(f);
          risen.push_back(f);
          inq[f] = 1;
          queue.push_back(f);
        }
      }
      f = w.eid_vw;
      sup[f]++;
      if (!inreg[f]) {
        uint32_t cf = std::min((uint32_t)sup[f] + 2, (uint32_t)tau[f] + 1);
        if (cf > tau[f]) {
          ubc[f] = cf;
          tau[f] = (uint16_t)cf;
          inreg[f] = 1;
          region.push_back(f);
          inq[f] = 1;
          queue.push_back(f);
        }
      }
    }
    size_t head = 0;
    while (head < queue.size()) {
      uint32_t f = queue[head++];
      inq[f] = 0;
      uint32_t a = g.eu[f], b = g.ev[f];
      st.next_epoch();
      for (auto& r : g.adj[a]) st.mark(r.nbr, r.eid);
      for (auto& r : g.adj[b]) {
        if (!st.test(r.nbr)) continue;
        uint32_t m1 = st.marked_eid(r.nbr), m2 = r.eid;
        for (uint32_t me = m1; ; me = m2) {
          if (!inreg[me]) {
            inreg[me] = 1;
            uint32_t cf = std::min((uint32_t)sup[me] + 2, (uint32_t)tau[me] + 1);
            if (cf > tau[me]) {
              ubc[me] = cf;
              tau[me] = (uint16_t)cf;
              risen.push_back(me);
            }
            region.push_back(me);
            inq[me] = 1;
            queue.push_back(me);
          }
          if (me == m2) break;
        }
      }
    }
    if (risen.size() > stats.region_max) stats.region_max = risen.size();
    stats.region_sum += risen.size();
    queue.clear();
    for (uint32_t f : risen) {
      inq[f] = 1;
      queue.push_back(f);
    }
    fixpoint(false);
    for (uint32_t f : region) inreg[f] = 0;
    region.clear();
    risen.clear();
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
