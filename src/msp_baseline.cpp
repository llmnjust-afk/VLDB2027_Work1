#include "msp_baseline.h"
#include <limits>
#include <stdexcept>
#include <omp.h>

extern "C" {
#include "kt.h"
#include "yche/log.h"
}

std::vector<uint32_t> msp_truss(const DynGraph& graph, uint32_t threads,
                               double& preprocess_s, double& kernel_s) {
  if (graph.n > (uint32_t)std::numeric_limits<int32_t>::max())
    throw std::runtime_error("MSP requires signed 32-bit vertex identifiers");
  if (!graph.m) return std::vector<uint32_t>(graph.cap_edges);

  // Construct MSP's sorted, undirected CSR directly in memory.
  const double start = now_s();
  vault_t vault{};
  vault.graph = gk_graph_Create();
  vault.graph->nvtxs = static_cast<int32_t>(graph.n);
  char offsets_name[] = "CSR offsets", neighbors_name[] = "CSR neighbors";
  vault.graph->xadj = gk_zmalloc(static_cast<size_t>(graph.n) + 1, offsets_name);
  vault.graph->adjncy = gk_i32malloc(2 * graph.m, neighbors_name);
  auto* offsets = vault.graph->xadj;
  offsets[0] = 0;
  for (uint32_t u = 0; u < graph.n; ++u) offsets[u + 1] = offsets[u] + graph.adj[u].size();
  log_set_quiet(1);
  omp_set_dynamic(0);
  omp_set_num_threads(static_cast<int>(threads));
#pragma omp parallel for schedule(static)
  for (uint32_t u = 0; u < graph.n; ++u) {
    auto* row = vault.graph->adjncy + offsets[u];
    for (size_t i = 0; i < graph.adj[u].size(); ++i)
      row[i] = static_cast<int32_t>(graph.adj[u][i].nbr);
    std::sort(row, row + graph.adj[u].size());
  }
  const double kernel_start = now_s();
  preprocess_s = kernel_start - start;

  params_t params{};
  params.jbsize = params.ibsize = 1000000;
  char emit_labels[] = "in-memory";
  params.outfile = emit_labels;
  kt_msp(&params, &vault);
  kernel_s = now_s() - kernel_start;

  std::vector<uint32_t> labels(graph.cap_edges, 0);
  bool valid = vault.nedges == static_cast<int64_t>(graph.m);
  for (int64_t i = 0; i < vault.nedges; ++i) {
    const auto& edge = vault.ktedges[i];
    const int64_t eid = graph.find(DynGraph::ekey(edge.vi, edge.vj));
    if (eid < 0 || edge.k < 2 || labels[eid]) valid = false;
    else labels[eid] = static_cast<uint32_t>(edge.k);
  }
  gk_graph_Free(&vault.ugraph);
  gk_graph_Free(&vault.graph);
  gk_free((void**)&vault.perm, (void**)&vault.iperm, (void**)&vault.ktedges, LTERM);
  if (!valid) throw std::runtime_error("invalid MSP edge-label output");
  return labels;
}
