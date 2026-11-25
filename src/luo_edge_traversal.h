#pragma once

#include "graph.h"

namespace luo {

template <class Visitor>
inline void for_each_supporting_triangle(const DynGraph& graph,
                                         const std::vector<uint32_t>& labels,
                                         uint32_t edge, uint32_t level, Visitor visit) {
  uint32_t u = graph.eu[edge], v = graph.ev[edge];
  if (graph.adj[u].size() > graph.adj[v].size()) std::swap(u, v);

  for (const auto& mate : graph.adj[u]) {
    if (labels[mate.eid] < level) continue;
    const int64_t other = graph.find(DynGraph::ekey(v, mate.nbr));
    if (other >= 0 && labels[other] >= level) visit(mate.eid, static_cast<uint32_t>(other));
  }
}

// Luo et al., IEEE TC 2023, Algorithms 2 and 4. All level searches for a group
// read the same labels. Commit their promotions or demotions after traversal.
inline std::vector<uint32_t> changed_at_level(const DynGraph& graph,
                                              const std::vector<uint32_t>& labels,
                                              uint32_t level,
                                              const std::vector<uint32_t>& seeds,
                                              bool inserting) {
  if (seeds.empty() || (!inserting && level == 2)) return {};

  struct Candidate {
    uint32_t support = 0;
    bool removed = false;
  };
  std::unordered_map<uint32_t, Candidate> state;
  state.reserve(seeds.size());
  std::vector<uint32_t> candidates;
  candidates.reserve(seeds.size());
  auto discover = [&](uint32_t edge) {
    if (state.try_emplace(edge).second) candidates.push_back(edge);
  };
  for (const uint32_t edge : seeds) discover(edge);

  // Only equal-level mates join the search. Higher-level mates supply support
  // but keep their labels above the threshold for this level's decision.
  for (size_t next = 0; next < candidates.size(); ++next) {
    const uint32_t edge = candidates[next];
    uint32_t support = 0;
    for_each_supporting_triangle(graph, labels, edge, level, [&](uint32_t a, uint32_t b) {
      ++support;
      if (labels[a] == level) discover(a);
      if (labels[b] == level) discover(b);
    });
    state.at(edge).support = support;
  }

  const uint32_t minimum = inserting ? level - 1 : level - 2;
  std::vector<uint32_t> rejected;
  for (const uint32_t edge : candidates) {
    if (state.at(edge).support < minimum) rejected.push_back(edge);
  }
  for (size_t next = 0; next < rejected.size(); ++next) {
    const uint32_t edge = rejected[next];
    state.at(edge).removed = true;
    for_each_supporting_triangle(graph, labels, edge, level, [&](uint32_t a, uint32_t b) {
      Candidate* left = labels[a] == level ? &state.at(a) : nullptr;
      Candidate* right = labels[b] == level ? &state.at(b) : nullptr;
      // Remove support only for the first rejection that destroys the triangle.
      if ((left && left->removed) || (right && right->removed)) return;
      if (left && left->support-- == minimum) rejected.push_back(a);
      if (right && right->support-- == minimum) rejected.push_back(b);
    });
  }

  if (!inserting) return rejected;
  candidates.erase(std::remove_if(candidates.begin(), candidates.end(), [&](uint32_t edge) {
    return state.at(edge).removed;
  }), candidates.end());
  return candidates;
}

}  // namespace luo
