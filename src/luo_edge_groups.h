#pragma once

#include "graph.h"

#include <unordered_set>

namespace luo {
namespace detail {

// Assign each update to the first group whose reserved triangle edges are
// disjoint from its own. The inverted index marks forbidden groups without
// scanning groups that do not reserve any of this update's edges.
template <class Update>
class DisjointGroups {
 public:
  void add(const Update& update, const std::vector<uint64_t>& triangle_edges) {
    ++update_number_;
    for (auto edge : triangle_edges) {
      const auto entry = groups_by_edge_.find(edge);
      if (entry == groups_by_edge_.end()) continue;
      for (auto group : entry->second) blocked_at_[group] = update_number_;
    }

    size_t group = 0;
    while (group < groups_.size() && blocked_at_[group] == update_number_) ++group;
    if (group == groups_.size()) {
      groups_.emplace_back();
      blocked_at_.push_back(0);
    }
    groups_[group].push_back(update);
    for (auto edge : triangle_edges) groups_by_edge_[edge].push_back(group);
  }

  std::vector<std::vector<Update>> take() { return std::move(groups_); }

 private:
  size_t update_number_ = 0;
  std::vector<std::vector<Update>> groups_;
  std::vector<size_t> blocked_at_;
  std::unordered_map<uint64_t, std::vector<size_t>> groups_by_edge_;
};

}  // namespace detail

using EdgeEndpoints = std::pair<uint32_t, uint32_t>;

// Edge-only specialization of Luo et al.'s triangle-independent groups.
// Net insertions are distinct non-edges. The sparse overlay enumerates
// triangles in the phase-final graph, so no later insertion can introduce
// a conflict missed while constructing a group.
inline std::vector<std::vector<EdgeEndpoints>> group_insertions(
    const DynGraph& graph, const std::vector<EdgeEndpoints>& insertions) {
  std::unordered_map<uint32_t, std::vector<uint32_t>> added_neighbors;
  std::unordered_set<uint64_t> added_edges;
  added_edges.reserve(insertions.size());
  for (auto [u, v] : insertions) {
    added_neighbors[u].push_back(v);
    added_neighbors[v].push_back(u);
    added_edges.insert(DynGraph::ekey(u, v));
  }

  detail::DisjointGroups<EdgeEndpoints> groups;
  std::vector<uint64_t> triangle_edges;
  for (const auto& insertion : insertions) {
    auto [u, v] = insertion;
    if (graph.adj[u].size() + added_neighbors.at(u).size() >
        graph.adj[v].size() + added_neighbors.at(v).size()) {
      std::swap(u, v);
    }

    triangle_edges.clear();
    const auto include_neighbor = [&](uint32_t w) {
      if (w == v) return;
      const auto mate = DynGraph::ekey(v, w);
      if (graph.find(mate) >= 0 || added_edges.count(mate)) {
        triangle_edges.push_back(DynGraph::ekey(u, w));
        triangle_edges.push_back(mate);
      }
    };
    for (const auto& edge : graph.adj[u]) include_neighbor(edge.nbr);
    for (auto w : added_neighbors.at(u)) include_neighbor(w);
    if (!triangle_edges.empty()) triangle_edges.push_back(DynGraph::ekey(u, v));
    groups.add(insertion, triangle_edges);
  }
  return groups.take();
}

// Deletion footprints are computed in the graph before removing any edge.
// Later groups may destroy triangles, which can only shrink the footprints.
inline std::vector<std::vector<uint32_t>> group_deletions(
    const DynGraph& graph, const std::vector<uint32_t>& deletions) {
  detail::DisjointGroups<uint32_t> groups;
  std::vector<uint64_t> triangle_edges;
  for (auto deletion : deletions) {
    auto u = graph.eu[deletion], v = graph.ev[deletion];
    if (graph.adj[u].size() > graph.adj[v].size()) std::swap(u, v);

    triangle_edges.clear();
    for (const auto& edge : graph.adj[u]) {
      const auto mate = DynGraph::ekey(v, edge.nbr);
      if (graph.find(mate) >= 0) {
        triangle_edges.push_back(DynGraph::ekey(u, edge.nbr));
        triangle_edges.push_back(mate);
      }
    }
    if (!triangle_edges.empty()) triangle_edges.push_back(DynGraph::ekey(u, v));
    groups.add(deletion, triangle_edges);
  }
  return groups.take();
}

}  // namespace luo
