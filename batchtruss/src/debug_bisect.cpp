#include "batch.h"
#include "incremental.h"
#include "stream.h"
#include <cstring>

int main(int argc, char** argv) {
  std::vector<std::string> a(argv + 1, argv + argc);
  std::string graph_path = "data/synth1.txt", stream_path = "", method = "peredge";
  for (size_t i = 0; i + 1 < a.size(); ++i) {
    if (a[i] == "--graph") graph_path = a[i + 1];
    if (a[i] == "--stream") stream_path = a[i + 1];
    if (a[i] == "--method") method = a[i + 1];
  }
  DynGraph g = DynGraph::load(graph_path);
  auto stream = load_stream(stream_path);
  StaticTruss base = static_truss(g, 1);
  printf("initial: n=%u m=%llu tau_max=%u sum=%llu\n", g.n, (unsigned long long)g.m,
         (unsigned)base.tau[0], (unsigned long long)0);
  uint32_t tmax = 0;
  uint64_t tsum = 0;
  for (uint32_t e = 0; e < g.cap_edges; ++e)
    if (g.alive(e)) {
      if (base.tau[e] > tmax) tmax = base.tau[e];
      tsum += base.tau[e];
    }
  printf("initial truth: tau_max=%u sum=%llu\n", tmax, (unsigned long long)tsum);

  bool batch_mode = (method == "batch" || method == "batch-nomerge");
  BatchMaintainer* bm = nullptr;
  PerEdgeMaintainer* pm = nullptr;
  if (batch_mode) {
    uint64_t reserve = 0;
    for (auto& b : stream)
      for (auto& op : b)
        if (!op.del) reserve++;
    bm = new BatchMaintainer(g, 1, reserve);
  } else {
    pm = new PerEdgeMaintainer(g);
  }

  auto check = [&](const char* tag, uint64_t opi, const Op* op) -> bool {
    StaticTruss s = static_truss(g, 1);
    for (uint32_t e = 0; e < g.cap_edges; ++e) {
      if (!g.alive(e)) continue;
      uint16_t got = batch_mode ? bm->tau[e] : pm->tau[e];
      if (got != s.tau[e]) {
        printf("FAIL after %s op#%llu", tag, (unsigned long long)opi);
        if (op) printf(" (%c %u %u)", op->del ? 'D' : 'I', op->u, op->v);
        printf(": eid=%u (%u,%u) maintained=%u truth=%u\n", e, g.eu[e], g.ev[e], got, s.tau[e]);
        return false;
      }
    }
    return true;
  };

  if (!batch_mode) {
    uint64_t opi = 0;
    for (auto& b : stream)
      for (auto& op : b) {
        if (op.del)
          pm->remove(op.u, op.v);
        else
          pm->insert(op.u, op.v);
        if (!check("peredge", opi, &op)) return 1;
        opi++;
      }
  } else {
    uint64_t opi = 0;
    for (size_t bi = 0; bi < stream.size(); ++bi) {
      auto& b = stream[bi];
      if (method == "batch-nomerge") {
        for (auto& op : b) {
          std::vector<Op> one = {op};
          bm->apply_batch(one);
          if (!check("batch-nomerge", opi, &op)) return 1;
          opi++;
        }
      } else {
        bm->apply_batch(b);
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
