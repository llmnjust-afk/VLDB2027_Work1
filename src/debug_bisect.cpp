#include "batch.h"
#include "incremental.h"
#include "stream.h"
#include "replay.h"
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
  auto stream = load_stream(stream_path, g.n);
  StaticTruss base = static_truss(g, nthreads);
  uint32_t tmax = 0;
  for (uint32_t e = 0; e < g.cap_edges; ++e)
    if (g.alive(e) && base.tau[e] > tmax) tmax = base.tau[e];
  printf("initial: n=%u m=%llu tau_max=%u\n", g.n, (unsigned long long)g.m, tmax);

  bool batch_mode = (method == "batch" || method == "batch-nomerge");
  BatchMaintainer* bm = nullptr;
  PerEdgeMaintainer* pm = nullptr;
  if (batch_mode) {
    bm = new BatchMaintainer(g, nthreads);
  } else {
    pm = new PerEdgeMaintainer(g);
    if (wu != UINT32_MAX) {
      int64_t we = g.find(DynGraph::ekey(wu, wv));
      if (we >= 0) { WATCH_EID = we; }
      fprintf(stderr, "[trace] watch eid=%lld\n", static_cast<long long>(we));
    }
  }

  ReplayOracle oracle(g);
  size_t batch_number = 0;
  StampSet wst;
  wst.init(g.n);
  std::vector<uint32_t> wmbuf;
  auto watch = [&](const char* tag) {
    if (wu == UINT32_MAX) return;
    int64_t we = g.find(DynGraph::ekey(wu, wv));
    if (we < 0) {
      printf("watch (%u,%u): absent %s\n", wu, wv, tag);
      return;
    }
    uint32_t mt = eval_trussness(wu, wv, g, batch_mode ? bm->tau : pm->tau, wst, wmbuf);
    uint32_t cur = batch_mode ? bm->tau[we] : pm->tau[we];
    printf("watch (%u,%u): tau=%u F=%u %s\n", wu, wv, cur, mt, tag);
  };

  for (const auto& batch : stream) {
    ++batch_number;
    if (!batch_mode) {
      for (const auto& op : batch) {
        if (op.del) pm->remove(op.u, op.v);
        else pm->insert(op.u, op.v);
      }
    } else if (method == "batch-nomerge") {
      bm->apply_batch_nomerge(batch);
    } else {
      bm->apply_batch(batch);
    }
    oracle.advance(batch);
    if ((check_every && batch_number % check_every == 0) || batch_number == stream.size()) {
      const auto& tau = batch_mode ? bm->tau : pm->tau;
      if (!oracle.check(g, tau, batch_number, batch)) return 1;
    }
    watch("after batch");
  }
  delete bm;
  delete pm;
  printf("ALL OK (%s, %zu batches)\n", method.c_str(), stream.size());
  return 0;
}
