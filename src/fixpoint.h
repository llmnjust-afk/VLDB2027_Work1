#pragma once
#include "truss_static.h"

inline uint16_t eval_trussness(uint32_t u, uint32_t v, const DynGraph& g,
                               const std::vector<uint16_t>& tau, StampSet& st,
                               std::vector<uint16_t>& mbuf) {
  mbuf.clear();
  st.next_epoch();
  for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
  for (auto& r : g.adj[v])
    if (st.test(r.nbr)) {
      uint32_t e1 = st.marked_eid(r.nbr), e2 = r.eid;
      mbuf.push_back((uint16_t)(tau[e1] < tau[e2] ? tau[e1] : tau[e2]));
    }
  if (mbuf.empty()) return 2;
  std::sort(mbuf.begin(), mbuf.end(), std::greater<uint16_t>());
  uint16_t best = 2;
  for (size_t j = 0; j < mbuf.size(); ++j) {
    uint32_t need = (uint32_t)j + 3;
    uint16_t cand = mbuf[j] < need ? mbuf[j] : (uint16_t)need;
    if (j + 3 > 65000) break;
    if (cand > best) best = cand;
  }
  return best;
}

inline uint32_t eval_wmax(uint32_t u, uint32_t v, const DynGraph& g,
                          const std::vector<uint16_t>& tau, StampSet& st) {
  st.next_epoch();
  for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
  uint32_t best = 0;
  for (auto& r : g.adj[v])
    if (st.test(r.nbr)) {
      uint32_t e1 = st.marked_eid(r.nbr), e2 = r.eid;
      uint32_t m = (uint32_t)(tau[e1] < tau[e2] ? tau[e1] : tau[e2]);
      if (m > best) best = m;
    }
  return best;
}
