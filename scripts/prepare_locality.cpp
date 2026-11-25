#include "stream.h"
#include <algorithm>
#include <iostream>
#include <limits>
#include <numeric>
#include <random>
#include <unordered_set>

namespace {

class Components {
public:
  explicit Components(uint32_t n) : parent_(n), size_(n, 1) {
    std::iota(parent_.begin(), parent_.end(), 0);
  }

  uint32_t root(uint32_t edge) {
    while (edge != parent_[edge]) {
      parent_[edge] = parent_[parent_[edge]];
      edge = parent_[edge];
    }
    return edge;
  }

  void join(uint32_t a, uint32_t b) {
    a = root(a);
    b = root(b);
    if (a == b) return;
    if (size_[a] < size_[b]) std::swap(a, b);
    parent_[b] = a;
    size_[a] += size_[b];
  }

  uint32_t size(uint32_t edge) { return size_[root(edge)]; }

private:
  std::vector<uint32_t> parent_, size_;
};

struct Candidate {
  Op op;
  uint32_t component, anchor, left, right;
  uint32_t edge = UINT32_MAX;
};

struct Candidates {
  std::vector<Candidate> deletions, insertions;
  uint32_t nontrivial_components = 0;
  uint32_t largest_component = 0;
};

bool single_witness(const DynGraph& graph, uint32_t u, uint32_t v, uint32_t anchor) {
  if (graph.adj[u].size() > graph.adj[v].size()) std::swap(u, v);
  for (const auto& edge : graph.adj[u])
    if (edge.nbr != anchor && graph.has(v, edge.nbr)) return false;
  return true;
}

Candidates candidates(const DynGraph& graph) {
  Components components(graph.cap_edges);
  struct Witness {
    uint32_t count = 0, anchor = 0, left = 0, right = 0;
    void add(uint32_t w, uint32_t l, uint32_t r) {
      ++count;
      anchor = w;
      left = l;
      right = r;
    }
  };
  std::vector<Witness> witnesses(graph.cap_edges);
  StampSet stamps;
  stamps.init(graph.n);
  for (uint32_t e = 0; e < graph.cap_edges; ++e) {
    const auto u = graph.eu[e], v = graph.ev[e];
    graph.common_neighbors(u, v, stamps, [&](uint32_t w, uint32_t left, uint32_t right) {
      if (e >= left || e >= right) return;
      components.join(e, left);
      components.join(e, right);
      witnesses[e].add(w, left, right);
      witnesses[left].add(v, e, right);
      witnesses[right].add(u, e, left);
    });
  }

  Candidates result;
  for (uint32_t e = 0; e < graph.cap_edges; ++e) {
    const auto root = components.root(e);
    if (root == e && components.size(e) > 1) {
      ++result.nontrivial_components;
      result.largest_component = std::max(result.largest_component, components.size(e));
    }
    const auto& witness = witnesses[e];
    if (witness.count == 1)
      result.deletions.push_back({{1, graph.eu[e], graph.ev[e]}, root, witness.anchor,
                                  witness.left, witness.right, e});
  }
  for (uint32_t center = 0; center < graph.n; ++center) {
    const auto& neighbors = graph.adj[center];
    for (size_t i = 0; i < neighbors.size(); ++i) {
      const auto left = neighbors[i];
      const auto root = components.root(left.eid);
      if (components.size(root) == 1) continue;
      for (size_t j = i + 1; j < neighbors.size(); ++j) {
        const auto right = neighbors[j];
        if (components.root(right.eid) != root || graph.has(left.nbr, right.nbr)) continue;
        if (single_witness(graph, left.nbr, right.nbr, center)) {
          result.insertions.push_back({{0, std::min(left.nbr, right.nbr), std::max(left.nbr, right.nbr)},
                                       root, center, left.eid, right.eid});
        }
      }
    }
  }
  return result;
}

using Pool = std::vector<const Candidate*>;

struct Placement {
  Pool deletions, insertions;
  uint32_t center = UINT32_MAX;
  size_t area_size = 0;
};

Placement concentrated_pool(const DynGraph& graph, const Candidates& all, bool component,
                            size_t half_batch) {
  const auto unit = [component](const Candidate& change) {
    return component ? change.component : change.anchor;
  };
  const auto units = component ? graph.cap_edges : graph.n;
  std::vector<size_t> deletions(units), insertions(units);
  for (const auto& change : all.deletions) ++deletions[unit(change)];
  for (const auto& change : all.insertions) ++insertions[unit(change)];

  // Pick a populated triangle component on Amazon. On Email, where all
  // triangle-bearing edges share one component, use a closed ego neighborhood.
  Placement result;
  size_t smallest = std::numeric_limits<size_t>::max();
  for (uint32_t u = 0; u < units; ++u) {
    size_t dels = deletions[u], ins = insertions[u], area_size = 1;
    if (!component) {
      area_size += graph.adj[u].size();
      for (const auto& edge : graph.adj[u]) {
        dels += deletions[edge.nbr];
        ins += insertions[edge.nbr];
      }
    }
    if (dels < 2 * half_batch || ins < 4 * half_batch) continue;
    if (component) {
      // Largest eligible deletion pool, with original dense ID as tie break.
      if (result.center != UINT32_MAX && deletions[u] <= deletions[result.center]) continue;
    } else {
      if (area_size >= smallest) continue;
      smallest = area_size;
    }
    result.center = u;
    result.area_size = area_size;
  }
  if (result.center == UINT32_MAX)
    throw std::runtime_error("no eligible concentrated placement for this batch size");

  const auto in_area = [&](const Candidate& change) {
    const auto key = unit(change);
    return key == result.center || (!component && graph.has(result.center, key));
  };
  for (const auto& change : all.deletions)
    if (in_area(change)) result.deletions.push_back(&change);
  for (const auto& change : all.insertions)
    if (in_area(change)) result.insertions.push_back(&change);
  return result;
}

struct Selected {
  Pool deletions, insertions;
  std::unordered_set<uint32_t> used_units, deleted_edges, inserted_endpoints;
};

Selected sample(const Placement& pool, size_t half_batch, bool component, bool dispersed,
                std::mt19937_64& rng) {
  const auto unit = [component](const Candidate& change) {
    return component ? change.component : change.anchor;
  };
  std::uniform_int_distribution<size_t> del_index(0, pool.deletions.size() - 1);
  std::uniform_int_distribution<size_t> ins_index(0, pool.insertions.size() - 1);
  const size_t attempt_limit = 1000 * half_batch;
  for (unsigned retry = 0; retry < 16; ++retry) {
    Selected result;
    for (size_t attempt = 0; result.deletions.size() < half_batch && attempt < attempt_limit; ++attempt) {
      const auto* change = pool.deletions[del_index(rng)];
      const auto group = unit(*change);
      // A previously deleted witness leg would make this the same triangle.
      if (result.deleted_edges.count(change->edge) ||
          result.deleted_edges.count(change->left) || result.deleted_edges.count(change->right) ||
          (dispersed && result.used_units.count(group))) continue;
      result.deletions.push_back(change);
      result.used_units.insert(group);
      result.deleted_edges.insert(change->edge);
    }
    if (result.deletions.size() != half_batch) continue;
    for (size_t attempt = 0; result.insertions.size() < half_batch && attempt < attempt_limit; ++attempt) {
      const auto* change = pool.insertions[ins_index(rng)];
      const auto group = unit(*change);
      if ((dispersed && result.used_units.count(group)) ||
          result.deleted_edges.count(change->left) || result.deleted_edges.count(change->right) ||
          result.inserted_endpoints.count(change->op.u) || result.inserted_endpoints.count(change->op.v))
        continue;
      result.insertions.push_back(change);
      result.used_units.insert(group);
      result.inserted_endpoints.insert(change->op.u);
      result.inserted_endpoints.insert(change->op.v);
    }
    if (result.insertions.size() == half_batch) return result;
  }
  throw std::runtime_error("could not satisfy independent triangle and placement constraints");
}

void emit_batch(std::ofstream& stream, const Selected& batch,
                std::ofstream& stats, unsigned pair, const char* condition,
                const Placement& pool, bool component, const Candidates& all) {
  const auto size = batch.deletions.size() + batch.insertions.size();
  stream << "B " << size << '\n';
  std::unordered_set<uint32_t> components, anchors, vertices, witness_edges;
  for (const auto* changes : {&batch.deletions, &batch.insertions}) {
    for (const auto* change : *changes) {
      stream << (change->op.del ? "D " : "I ") << change->op.u << ' ' << change->op.v << '\n';
      components.insert(change->component);
      anchors.insert(change->anchor);
      vertices.insert(change->op.u);
      vertices.insert(change->op.v);
      witness_edges.insert(change->left);
      witness_edges.insert(change->right);
    }
  }
  // Restore the original topology exactly; these even batches are excluded
  // from treatment latency but still undergo replay verification.
  stream << "B " << size << '\n';
  for (const auto* changes : {&batch.insertions, &batch.deletions})
    for (auto item = changes->rbegin(); item != changes->rend(); ++item)
      stream << ((*item)->op.del ? "I " : "D ") << (*item)->op.u << ' ' << (*item)->op.v << '\n';
  stats << condition << ',' << pair << ',' << pair * 2 - 1 << ',' << size / 2 << ',' << size / 2
        << ',' << components.size() << ',' << anchors.size() << ',' << vertices.size()
        << ',' << witness_edges.size() << ',' << (component ? "triangle_component" : "witness_anchor")
        << ',' << batch.used_units.size() << ',';
  if (pool.center != UINT32_MAX) stats << pool.center << ',' << pool.area_size;
  else stats << ',';
  stats << ',' << all.nontrivial_components << ',' << all.largest_component << '\n';
}

}  // namespace

int main(int argc, char** argv) try {
  if (argc != 7)
    throw std::runtime_error("usage: prepare-locality GRAPH DATASET BATCHES BATCH_SIZE SEED OUTPUT");
  auto graph = DynGraph::load(argv[1]);
  const std::string dataset = argv[2], output = argv[6];
  if (dataset != "amazon" && dataset != "email")
    throw std::runtime_error("expected amazon or email");
  const unsigned pairs = std::stoul(argv[3]);
  const size_t size = std::stoul(argv[4]);
  if (pairs == 0 || size < 2 || size % 2)
    throw std::runtime_error("expected a positive pair count and an even batch size");
  const bool component = dataset == "amazon";
  std::mt19937_64 rng(std::stoull(argv[5]));
  const auto all = candidates(graph);
  const auto concentrated = concentrated_pool(graph, all, component, size / 2);
  Placement dispersed;
  for (const auto& change : all.deletions) dispersed.deletions.push_back(&change);
  for (const auto& change : all.insertions) dispersed.insertions.push_back(&change);
  std::ofstream left(output + "/concentrated.stream"), right(output + "/dispersed.stream"),
      stats(output + "/placement.csv");
  if (!left || !right || !stats) throw std::runtime_error("cannot open generator outputs");
  stats << "condition,pair,batch,deleted,inserted,initial_components,witness_anchors,updated_vertices,"
           "distinct_witness_edges,placement_unit,distinct_units,concentrated_center,concentrated_area_size,"
           "graph_nontrivial_components,graph_largest_triangle_component_edges\n";
  for (unsigned pair = 1; pair <= pairs; ++pair) {
    auto local = sample(concentrated, size / 2, component, false, rng);
    auto spread = sample(dispersed, size / 2, component, true, rng);
    if (local.used_units.size() >= spread.used_units.size())
      throw std::runtime_error("concentrated and dispersed weighting failed to separate");
    emit_batch(left, local, stats, pair, "concentrated", concentrated, component, all);
    emit_batch(right, spread, stats, pair, "dispersed", dispersed, component, all);
  }
  left.close();
  right.close();
  stats.close();
  if (!left || !right || !stats) throw std::runtime_error("failed writing locality streams");
  std::cout << graph.n << ' ' << graph.m << ' ' << all.nontrivial_components << ' '
            << all.largest_component << '\n';
} catch (const std::exception& e) {
  std::cerr << e.what() << '\n';
  return 1;
}
