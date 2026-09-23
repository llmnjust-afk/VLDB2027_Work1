#include "batch.h"
#include "incremental.h"
#include "stream.h"
#include <cstring>

int main(int argc, char** argv) {
  std::vector<std::string> a(argv + 1, argv + argc);
  std::string graph_path = "data/synth1.txt", stream_path = "", method = "peredge";
  uint32_t wu = UINT32_MAX, wv = UINT32_MAX;
  uint32_t check_every = 1;
  uint32_t nthreads = 1;
  for (size_t i = 0; i + 1 < a.size(); ++i) {
    if (a[i] == "--graph") graph_path = a[i + 1];
    if (a[i] == "--stream") stream_path = a[i + 1];
    if (a[i] == "--method") method = a[i + 1];
    if (a[i] == "--check-every") check_every = (uint32_t)atoll(a[i + 1].c_str());
    if (a[i] == "--watch-u") wu = (uint32_t)atoll(a[i + 1].c_str());
    if (a[i] == "--watch-v") wv = (uint32_t)atoll(a[i + 1].c_str());
    if (a[i] == "--threads") nthreads = (uint32_t)atoll(a[i + 1].c_str());
  }
  DynGraph g = DynGraph::load(graph_path);
  auto stream = load_stream(stream_path);
  StaticTruss base = static_truss(g, nthreads);
  uint32_t tmax = 0;
  for (uint32_t e = 0; e < g.cap_edges; ++e)
    if (g.alive(e) && base.tau[e] > tmax) tmax = base.tau[e];
  printf("initial: n=%u m=%llu tau_max=%u\n", g.n, (unsigned long long)g.m, tmax);

  bool batch_mode = (method == "batch" || method == "batch-nomerge");
  BatchMaintainer* bm = nullptr;
  PerEdgeMaintainer* pm = nullptr;
  if (batch_mode) {
    uint64_t reserve = 0;
    for (auto& b : stream)
      for (auto& op : b)
        if (!op.del) reserve++;
    bm = new BatchMaintainer(g, nthreads, reserve);
    if (wu != UINT32_MAX) {
      int64_t we = g.find(DynGraph::ekey(wu, wv));
      if (we >= 0) { bm->watch_eid = we; }
    }
  } else {
    pm = new PerEdgeMaintainer(g);
    if (wu != UINT32_MAX) {
      int64_t we = g.find(DynGraph::ekey(wu, wv));
      if (we >= 0) { WATCH_EID = we; }
      fprintf(stderr, "[trace] watch eid=%lld\n", we);
    }
  }

  StampSet wst;
  wst.init(g.n);
  std::vector<uint16_t> wmbuf;
  auto watch = [&](const char* tag) {
    if (wu == UINT32_MAX) return;
    int64_t we = g.find(DynGraph::ekey(wu, wv));
    if (we < 0) {
      printf("watch (%u,%u): absent %s\n", wu, wv, tag);
      return;
    }
    uint16_t mt = eval_trussness(wu, wv, g, batch_mode ? bm->tau : pm->tau, wst, wmbuf);
    uint16_t cur = batch_mode ? bm->tau[we] : pm->tau[we];
    printf("watch (%u,%u): tau=%u F=%u %s\n", wu, wv, cur, mt, tag);
  };

  auto check = [&](const char* tag, uint64_t opi, const Op* op) -> bool {
    StaticTruss s = static_truss(g, 1);
    for (uint32_t e = 0; e < g.cap_edges; ++e) {
      if (!g.alive(e)) continue;
      uint16_t got = batch_mode ? bm->tau[e] : pm->tau[e];
      if (got != s.tau[e]) {
        printf("FAIL after %s op#%llu", tag, (unsigned long long)opi);
        if (op) printf(" (%c %u %u)", op->del ? 'D' : 'I', op->u, op->v);
        printf(": eid=%u (%u,%u) maintained=%u truth=%u\n", e, g.eu[e], g.ev[e], got, s.tau[e]);
        uint32_t u = g.eu[e], v = g.ev[e];
        wst.next_epoch();
        for (auto& r : g.adj[u]) wst.mark(r.nbr, r.eid);
        printf("  witnesses (maintained tau | truth):");
        for (auto& r : g.adj[v])
          if (wst.test(r.nbr)) {
            uint32_t e1 = wst.marked_eid(r.nbr), e2 = r.eid;
            printf(" w=%u:(%u,%u)|(%u,%u)", r.nbr,
                   batch_mode ? bm->tau[e1] : pm->tau[e1], batch_mode ? bm->tau[e2] : pm->tau[e2],
                   s.tau[e1], s.tau[e2]);
          }
        printf("\n");
        return false;
      }
    }
    return true;
  };

  if (!batch_mode) {
    uint64_t opi = 0;
    uint64_t vcount = 0;
    for (auto& b : stream)
      for (auto& op : b) {
        if (op.del)
          pm->remove(op.u, op.v);
        else
          pm->insert(op.u, op.v);
        vcount++;
        if (check_every && vcount % check_every == 0) {
          if (!check("peredge", opi, &op)) return 1;
        }
        watch("after peredge op");
        opi++;
      }
    if (!check("final", opi, nullptr)) return 1;
    printf("peredge stats: evals=%llu region_max=%u region_sum=%llu ops=%llu\n",
           (unsigned long long)pm->stats.evals, pm->stats.region_max,
           (unsigned long long)pm->stats.region_sum, (unsigned long long)opi);
  } else {
    std::vector<uint16_t> prev_tau;
    uint64_t opi = 0;
    for (size_t bi = 0; bi < stream.size(); ++bi) {
      auto& b = stream[bi];
      if (method == "batch-nomerge") {
        for (auto& op : b) {
          if (bm->watch_eid >= 0)
            fprintf(stderr, "[pre] op#%llu in_reg=%u tau=%u\n", (unsigned long long)opi,
                    (unsigned)bm->in_reg[bm->watch_eid], (unsigned)bm->tau[bm->watch_eid]);
          std::vector<Op> one = {op};
          bm->apply_batch(one);
          if (!check("batch-nomerge", opi, &op)) return 1;
          opi++;
        }
      } else {
        uint32_t ch = 0; uint64_t s1 = 0, s2 = 0;
        for (uint32_t e = 0; e < g.cap_edges; ++e)
          if (g.alive(e)) s1 += bm->tau[e];
        bm->apply_batch(b);
        for (uint32_t e = 0; e < g.cap_edges; ++e)
          if (g.alive(e)) { s2 += bm->tau[e]; if (e >= prev_tau.size() || bm->tau[e] != prev_tau[e]) ch++; }
        printf("BATCHSTAT batch %zu size %zu changed_edges=%u tausum %llu->%llu\n", bi, b.size(), ch,
               (unsigned long long)s1, (unsigned long long)s2);
        prev_tau.assign(bm->tau.begin(), bm->tau.end());
        if (!check("batch", opi, &b[0])) {
          printf("batch index %zu size %zu\n", bi, b.size());
          for (auto& op : b) printf("  %c %u %u\n", op.del ? 'D' : 'I', op.u, op.v);
          return 1;
        }
        opi += b.size();
      }
    }
  }
  printf("ALL OK (%s)\n", method.c_str());
  return 0;
}
