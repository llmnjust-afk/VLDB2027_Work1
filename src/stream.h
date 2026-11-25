#pragma once
#include "truss_static.h"
#include <fstream>
#include <sstream>
#include <stdexcept>

inline std::vector<std::vector<Op>> load_stream(const std::string& path,
                                                uint32_t vertices) {
  std::ifstream input(path);
  if (!input) throw std::runtime_error("cannot open stream: " + path);
  std::vector<std::vector<Op>> batches;
  std::string line;
  size_t line_number = 0;
  uint64_t declared = 0;
  while (std::getline(input, line)) {
    ++line_number;
    std::istringstream fields(line);
    char code;
    if (!(fields >> code) || code == '#' || code == '%') continue;
    auto invalid = [&] {
      throw std::runtime_error(path + ":" + std::to_string(line_number) +
                               ": invalid operation, endpoint or batch length");
    };
    std::string extra;
    if (code == 'B' || code == 'b') {
      if ((!batches.empty() && batches.back().size() != declared) ||
          !(fields >> declared) || declared > UINT32_MAX || (fields >> extra))
        invalid();
      batches.emplace_back();
      continue;
    }
    uint64_t u, v;
    if (batches.empty() || batches.back().size() >= declared ||
        (code != 'I' && code != 'i' && code != 'D' && code != 'd') ||
        !(fields >> u >> v) || u >= vertices || v >= vertices || (fields >> extra))
      invalid();
    batches.back().push_back({static_cast<uint8_t>(code == 'D' || code == 'd'),
                              static_cast<uint32_t>(u), static_cast<uint32_t>(v)});
  }
  if (!batches.empty() && batches.back().size() != declared)
    throw std::runtime_error(path + ": truncated final batch");
  return batches;
}

inline void save_graph_edgelist(const DynGraph& g, const std::string& path,
                                bool preserve_ids = false) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("cannot write graph: " + path);
  if (preserve_ids)
    for (uint32_t u = 0; u < g.n; ++u) out << u << " " << u << "\n";
  for (uint32_t u = 0; u < g.n; ++u)
    for (auto& r : g.adj[u])
      if (u < r.nbr) out << u << " " << r.nbr << "\n";
}

inline void save_stream(const std::vector<std::vector<Op>>& batches, const std::string& path) {
  std::ofstream out(path);
  if (!out) throw std::runtime_error("cannot write stream: " + path);
  for (auto& b : batches) {
    out << "B " << b.size() << "\n";
    for (auto& op : b) out << (op.del ? "D " : "I ") << op.u << " " << op.v << "\n";
  }
}

inline void gen_random_stream(const DynGraph& g, uint32_t nbatches, uint32_t batch_size,
                              double p_insert, uint64_t seed,
                              std::vector<std::vector<Op>>& out) {
  Rng rng(seed);
  out.clear();
  for (uint32_t b = 0; b < nbatches; ++b) {
    std::vector<Op> ops;
    for (uint32_t i = 0; i < batch_size; ++i) {
      if (rng.next() % 1000000 < (uint64_t)(p_insert * 1000000)) {
        for (uint32_t tries = 0; tries < 1000; ++tries) {
          uint32_t u = rng.below(g.n), v = rng.below(g.n);
          if (u != v && !g.has(u, v)) {
            ops.push_back({0, u, v});
            break;
          }
        }
      } else {
        for (uint32_t tries = 0; tries < 1000; ++tries) {
          uint32_t u = rng.below(g.n);
          if (!g.adj[u].empty()) {
            uint32_t idx = rng.below((uint32_t)g.adj[u].size());
            uint32_t v = g.adj[u][idx].nbr;
            ops.push_back({1, u, v});
            break;
          }
        }
      }
    }
    out.push_back(ops);
  }
}

inline void synth_graph(uint32_t n, double p, const std::string& path) {
  Rng rng(424242);
  std::vector<std::pair<uint32_t, uint32_t>> el;
  for (uint32_t u = 0; u < n; ++u)
    for (uint32_t v = u + 1; v < n; ++v)
      if (rng.next() % 1000000 < (uint64_t)(p * 1000000)) el.push_back({u, v});
  std::ofstream out(path);
  for (auto& e : el) out << e.first << " " << e.second << "\n";
}

inline void synth_clusters(uint32_t nclusters, uint32_t csize, uint32_t bridging,
                           const std::string& path) {
  Rng rng(777);
  std::vector<std::pair<uint32_t, uint32_t>> el;
  for (uint32_t c = 0; c < nclusters; ++c) {
    uint32_t base = c * csize;
    for (uint32_t u = 0; u < csize; ++u)
      for (uint32_t v = u + 1; v < csize; ++v)
        if (rng.next() % 100 < 70) el.push_back({base + u, base + v});
  }
  uint32_t n = nclusters * csize;
  for (uint32_t i = 0; i < bridging; ++i) {
    uint32_t u = rng.below(n), v = rng.below(n);
    if (u != v) el.push_back({u, v});
  }
  std::ofstream out(path);
  for (auto& e : el) out << e.first << " " << e.second << "\n";
}
