#include "batch.h"
#include "replay.h"
#include "stream.h"

#include <iostream>

struct EdgeLabel {
  uint64_t key;
  uint32_t label;
};

// Check the maintained graph and labels against the independently ordered replay.
static void check_and_snapshot(const DynGraph& actual, const BatchMaintainer& maintainer,
                               const DynGraph& oracle, const StaticTruss& truth,
                               std::vector<EdgeLabel>& snapshot) {
  if (actual.m != oracle.m) throw std::runtime_error("edge count mismatch");
  snapshot.clear();
  for (uint32_t e = 0; e < oracle.cap_edges; ++e) {
    if (!oracle.alive(e)) continue;
    auto key = DynGraph::ekey(oracle.eu[e], oracle.ev[e]);
    auto id = actual.find(key);
    if (id < 0 || maintainer.tau.at(id) != truth.tau[e])
      throw std::runtime_error("edge or trussness mismatch at (" +
                               std::to_string(oracle.eu[e]) + ", " +
                               std::to_string(oracle.ev[e]) + ")");
    snapshot.push_back({key, truth.tau[e]});
  }
}

int main(int argc, char** argv) {
  if (argc != 3) {
    fprintf(stderr, "usage: %s graph stream\n", argv[0]);
    return 2;
  }
  size_t batch_number = 0;
  try {
    auto actual = DynGraph::load(argv[1]);
    auto batches = load_stream(argv[2], actual.n);
    auto oracle = actual.clone();
    BatchMaintainer maintainer(actual, 1);
    std::vector<EdgeLabel> previous, current;
    previous.reserve(actual.m);
    current.reserve(actual.m);
    check_and_snapshot(actual, maintainer, oracle, static_truss(oracle), previous);

    puts("batch,raw_ops,previous_m,final_m,net_deleted,net_inserted,region_edges,"
         "region_surviving,changed,promoted,demoted,changed_two_or_more,"
         "degree_mass,region_triangles,verify_ok");
    for (const auto& operations : batches) {
      ++batch_number;
      maintainer.stats = {};
      maintainer.apply_batch(operations);
      for (const auto& op : operations) replay_edge(oracle, op);
      auto truth = static_truss(oracle);
      check_and_snapshot(actual, maintainer, oracle, truth, current);

      uint64_t degree_mass = 0, triangle_incidence = 0;
      const auto& region = maintainer.last_region();
      std::vector<uint8_t> in_region(actual.cap_edges);
      for (auto e : region) {
        if (!actual.alive(e) || in_region[e])
          throw std::runtime_error("region contains a dead or duplicate edge");
        in_region[e] = 1;
        auto u = actual.eu[e], v = actual.ev[e];
        degree_mass += actual.adj[u].size() + actual.adj[v].size();
        triangle_incidence += truth.sup.at(oracle.get_eid(u, v));
      }
      if (triangle_incidence % 3 != 0 || region.size() != maintainer.stats.region_max)
        throw std::runtime_error("inconsistent triangle incidence or region size");

      uint64_t survivors = 0, promoted = 0, demoted = 0, changed_two_or_more = 0;
      for (const auto& old : previous) {
        auto id = oracle.find(old.key);
        if (id < 0) continue;
        ++survivors;
        auto label = truth.tau[id];
        if (label == old.label) continue;
        if (!in_region.at(actual.find(old.key)))
          throw std::runtime_error("a surviving label changed outside the region");
        promoted += label > old.label;
        demoted += label < old.label;
        changed_two_or_more += std::max(label, old.label) - std::min(label, old.label) >= 2;
      }
      uint64_t deleted = previous.size() - survivors;
      uint64_t inserted = current.size() - survivors;
      uint64_t changed = promoted + demoted;
      if (deleted != maintainer.stats.deleted || inserted != maintainer.stats.inserted ||
          inserted + changed > region.size())
        throw std::runtime_error("inconsistent net updates or surviving-label counts");
      std::cout << batch_number << ',' << operations.size() << ',' << previous.size() << ','
                << current.size() << ',' << deleted << ',' << inserted << ',' << region.size() << ','
                << region.size() - inserted << ',' << changed << ',' << promoted << ',' << demoted << ','
                << changed_two_or_more << ',' << degree_mass << ',' << triangle_incidence / 3 << ",1\n";
      if (!std::cout) throw std::runtime_error("cannot write label-change counts");
      previous.swap(current);
    }
  } catch (const std::exception& error) {
    fprintf(stderr, "checkpoint %zu: %s\n", batch_number, error.what());
    return 2;
  }
  return 0;
}
