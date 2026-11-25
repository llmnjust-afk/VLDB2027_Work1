#pragma once
#include "truss_static.h"

// The oracle applies each original operation directly, in input order.
inline bool replay_edge(DynGraph& graph, const Op& op) {
  if (op.u == op.v) return false;
  int64_t edge = graph.find(DynGraph::ekey(op.u, op.v));
  if (op.del && edge >= 0) {
    graph.remove_edge(static_cast<uint32_t>(edge));
    return true;
  }
  if (!op.del && edge < 0) {
    graph.add_edge(op.u, op.v);
    return true;
  }
  return false;
}

class ReplayOracle {
public:
  explicit ReplayOracle(const DynGraph& initial) : expected(initial.clone()) {}

  void advance(const std::vector<Op>& batch) {
    for (const auto& op : batch) replay_edge(expected, op);
  }

  bool check(const DynGraph& actual, const std::vector<uint32_t>& tau,
             size_t batch_number, const std::vector<Op>& operations) const {
    bool matches = true;
    for (uint32_t e = 0; e < expected.cap_edges; ++e) {
      if (expected.alive(e) && !actual.has(expected.eu[e], expected.ev[e])) {
        fprintf(stderr, "missing edge (%u,%u)\n", expected.eu[e], expected.ev[e]);
        matches = false;
        break;
      }
    }
    for (uint32_t e = 0; e < actual.cap_edges; ++e) {
      if (actual.alive(e) && !expected.has(actual.eu[e], actual.ev[e])) {
        fprintf(stderr, "extra edge (%u,%u)\n", actual.eu[e], actual.ev[e]);
        matches = false;
        break;
      }
    }
    if (!matches) {
      fprintf(stderr, "edge set mismatch: expected=%llu actual=%llu\n",
              (unsigned long long)expected.m, (unsigned long long)actual.m);
    } else {
      auto truth = static_truss(expected);
      for (uint32_t e = 0; e < expected.cap_edges; ++e) {
        if (!expected.alive(e)) continue;
        uint32_t u = expected.eu[e], v = expected.ev[e];
        uint32_t label = tau.at(actual.get_eid(u, v));
        if (label != truth.tau[e]) {
          fprintf(stderr, "trussness mismatch (%u,%u): expected=%u actual=%u\n",
                  u, v, truth.tau[e], label);
          matches = false;
          break;
        }
      }
    }
    if (!matches) {
      fprintf(stderr, "first failing checkpoint: batch %zu (%zu operations)\n",
              batch_number, operations.size());
      for (const auto& op : operations)
        fprintf(stderr, "%c %u %u\n", op.del ? 'D' : 'I', op.u, op.v);
    }
    return matches;
  }

private:
  DynGraph expected;
};
