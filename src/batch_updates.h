#pragma once
#include "graph.h"

struct NetBatchUpdates {
  std::vector<uint32_t> deletes;
  std::vector<uint64_t> inserts;
};

inline NetBatchUpdates coalesce_batch_updates(const DynGraph& graph,
                                             const std::vector<Op>& operations) {
  // Set semantics: the last operation on an edge determines its final membership.
  std::unordered_map<uint64_t, bool> final_presence;
  final_presence.reserve(operations.size());
  for (const auto& op : operations)
    if (op.u != op.v) final_presence[DynGraph::ekey(op.u, op.v)] = !op.del;

  NetBatchUpdates updates;
  for (const auto& change : final_presence) {
    int64_t edge = graph.find(change.first);
    if (change.second && edge < 0) updates.inserts.push_back(change.first);
    if (!change.second && edge >= 0) updates.deletes.push_back(static_cast<uint32_t>(edge));
  }
  return updates;
}

template <class Callback>
inline void apply_batch_deletions(DynGraph& graph, const std::vector<uint32_t>& deletes,
                                  Callback on_delete) {
  for (uint32_t edge : deletes) {
    graph.remove_edge(edge);
    on_delete(edge);
  }
}

template <class Callback>
inline void apply_batch_insertions(DynGraph& graph, const std::vector<uint64_t>& inserts,
                                   Callback on_insert) {
  for (uint64_t key : inserts)
    on_insert(graph.add_edge(static_cast<uint32_t>(key >> 32), static_cast<uint32_t>(key)));
}
