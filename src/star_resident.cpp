#include "stream.h"
#include "replay.h"

#include <common.h>  // The generated release header, located in the include path.
#include <myG.h>
#include <decomp.h>
#include <delPart.h>
#include <insertPart.h>
#include <filesystem>
#include <queue>
#include <set>

// These counters belong to the two released kernels. Their command-line
// front ends originally supplied the definitions.
long g_lSimpleAddTime, g_lFillInfoTime, g_lBatchInsTotalTime, g_lBallDecTime;
long g_lLocalDecTime, g_lEdgeDecTime, g_lOutBallTime, g_lAllKTime, g_lRmQTime;
long g_lNeibCnt, g_lUsefulCnt;
long g_lPreLoadTime, g_lPreRmLoadTime, g_lPreRmTime, g_lPreRmCoreTime, g_lPreRmBasicTime;
long g_lPreFindTime, g_lLocalTime, g_lAfterLocalTime, g_lUpdateKTime, g_lRecalLTime, g_lUpdateLTime;

// Deletion already removes the triangle entries from the mate edges. Keep
// the insertion layout's neighbor counts synchronized when retiring an edge.
int myG::rm(int eid) {
  auto& edge = *findNode(eid);
  const auto [u, v] = edge.paXY;
  for (const auto& [from, to] : {std::pair{u, v}, std::pair{v, u}}) {
    auto& adj = m_vAdj[from];
    auto at = std::lower_bound(adj.begin(), adj.end(), to,
        [](const TPST_ADJ& a, int vertex) { return a.iPid < vertex; });
    if (at == adj.end() || at->iPid != to || at->iEid != eid)
      throw std::runtime_error("Star removal has an inconsistent adjacency");
    adj.erase(at);
    --(*m_pvPNeCnt)[from];
  }
  edge.eid = 0;
  return 0;
}

void myG::record_insertion_change(TPST_MAP_BY_EID* edge) {
  if (!edge->resident_dirty) {
    resident_changes.push_back(edge->eid);
    edge->resident_dirty = true;
  }
}

namespace {
using Edge = std::pair<int, int>;

struct Stars {
  std::map<int, std::vector<int>> edges;
  std::list<Edge> queries;
};

// Match the released CLI adapter's residual-degree partition and its
// deterministic tie break on vertex IDs.
Stars partition(const std::vector<Edge>& changes) {
  std::map<int, std::set<int>> adjacency;
  for (const auto& [u, v] : changes) {
    adjacency[u].insert(v);
    adjacency[v].insert(u);
  }
  std::priority_queue<Edge> queue;
  for (const auto& [node, neighbors] : adjacency)
    queue.emplace(static_cast<int>(neighbors.size()), -node);

  Stars stars;
  while (!queue.empty()) {
    const auto [degree, node_key] = queue.top();
    queue.pop();
    const int node = -node_key;
    auto& neighbors = adjacency.at(node);
    if (degree == 0 || static_cast<size_t>(degree) != neighbors.size()) continue;
    stars.queries.emplace_back(node, 0);
    stars.edges.emplace(node, std::vector<int>(neighbors.begin(), neighbors.end()));
    for (int other : neighbors) {
      auto& remaining = adjacency.at(other);
      remaining.erase(node);
      if (!remaining.empty())
        queue.emplace(static_cast<int>(remaining.size()), -other);
    }
    neighbors.clear();
  }
  return stars;
}

struct Counts {
  size_t deleted = 0;
  size_t inserted = 0;
};

struct StarPhaseTimes {
  double coalesce_s = 0;
  double partition_s = 0;
  double delete_s = 0;
  double insert_s = 0;
  double refresh_s = 0;
};

class ResidentStar {
 public:
  explicit ResidentStar(const DynGraph& initial) : edge_count_(initial.m) {
    if (initial.n >= INT_MAX || initial.m >= INT_MAX)
      throw std::runtime_error("Star vertex and edge IDs must fit in signed int");
    std::vector<std::pair<uint32_t, uint32_t>> edges;
    edges.reserve(initial.m);
    for (uint32_t eid = 0; eid < initial.cap_edges; ++eid)
      if (initial.alive(eid)) edges.emplace_back(initial.eu[eid], initial.ev[eid]);

    // Star's release uses an internal +1 offset and ID 0 as a sentinel.
    truss_maint::decomp::Decomp index(initial.n, std::move(edges));
    graph_.m_iMaxPId = static_cast<int>(initial.n);
    graph_.m_iMaxEId = static_cast<int>(initial.m);
    graph_.m_iMaxD = static_cast<int>(index.max_degree());
    graph_.m_iMaxK = static_cast<int>(index.max_trussness());
    graph_.m_iDensePId = 0;
    graph_.m_vAdj.resize(initial.n + 1);
    graph_.m_pvPNeCnt->resize(initial.n + 1);
    graph_.m_pvG->reserve(initial.m + 1);
    for (size_t i = 0; i < index.edges().size(); ++i) {
      const auto [u, v] = index.edges()[i];
      TPST_MAP_BY_EID edge{};
      edge.eid = static_cast<int>(i + 1);
      edge.paXY = {static_cast<int>(u + 1), static_cast<int>(v + 1)};
      edge.iTrussness = static_cast<int>(index.trussness()[i]);
      edge.iLayer = static_cast<int>(index.layer()[i]);
      edge.iSeSup = static_cast<int>(index.layer_support()[i]);
      graph_.m_pvG->push_back(edge);
      graph_.m_vAdj[u + 1].push_back({edge.paXY.second, edge.eid});
      graph_.m_vAdj[v + 1].push_back({edge.paXY.first, edge.eid});
      ++(*graph_.m_pvPNeCnt)[u + 1];
      ++(*graph_.m_pvPNeCnt)[v + 1];
    }
    graph_.init();
    for (int eid = 1; eid <= graph_.m_iMaxEId; ++eid) graph_.init(eid);
    for (int eid = 1; eid <= graph_.m_iMaxEId; ++eid) refresh_deletion_support(eid);
  }

  Counts apply(const std::vector<Op>& raw, StarPhaseTimes* phases) {
    PhaseClock coalesce_clock(phases != nullptr);
    std::unordered_map<uint64_t, bool> last;
    last.reserve(raw.size());
    for (const auto& op : raw)
      if (op.u != op.v) last[DynGraph::ekey(op.u, op.v)] = !op.del;

    std::vector<Edge> deletes, inserts;
    for (const auto& [key, present] : last) {
      const Edge edge{static_cast<int>((key >> 32) + 1),
                      static_cast<int>((key & UINT32_MAX) + 1)};
      const bool existed = graph_.findNode(edge.first, edge.second) != nullptr;
      if (!present && existed) deletes.push_back(edge);
      if (present && !existed) inserts.push_back(edge);
    }
    if (inserts.size() >= static_cast<size_t>(INT_MAX - graph_.m_iMaxEId))
      throw std::runtime_error("Star edge IDs exhausted");
    if (phases) phases->coalesce_s = coalesce_clock.lap();

    if (!deletes.empty()) {
      PhaseClock clock(phases != nullptr);
      auto stars = partition(deletes);
      if (phases) phases->partition_s += clock.lap();
      for (const auto& [center, ignored] : stars.queries)
        delPart::delOneNodePart(graph_, stars.edges, center);
      if (phases) phases->delete_s = clock.lap();
    }
    if (!inserts.empty()) {
      PhaseClock clock(phases != nullptr);
      const int previous_edge_count = graph_.m_iMaxEId;
      auto stars = partition(inserts);
      if (phases) phases->partition_s += clock.lap();
      insertPart::insertAll(graph_, stars.edges, stars.queries);
      if (phases) phases->insert_s = clock.lap();
      for (int eid = previous_edge_count + 1; eid <= graph_.m_iMaxEId; ++eid)
        graph_.record_insertion_change(graph_.findNode(eid));
      refresh_insertion_changes();
      if (phases) phases->refresh_s = clock.lap();
    }
    edge_count_ = edge_count_ - deletes.size() + inserts.size();
    return {deletes.size(), inserts.size()};
  }

  uint64_t edges() const { return edge_count_; }

  bool verify(const DynGraph& expected, size_t batch) {
    const auto truth = static_truss(expected);
    std::vector<bool> matched(expected.cap_edges);
    uint64_t checked = 0;
    for (int eid = 1; eid <= graph_.m_iMaxEId; ++eid) {
      const auto& edge = *graph_.findNode(eid);
      if (edge.eid == 0) continue;
      ++checked;
      const uint32_t u = edge.paXY.first - 1;
      const uint32_t v = edge.paXY.second - 1;
      const int64_t expected_id = expected.find(DynGraph::ekey(u, v));
      if (expected_id < 0) {
        std::cerr << "batch " << batch << ": Star has extra edge (" << u << ',' << v << ")\n";
        return false;
      }
      if (matched[expected_id]) {
        std::cerr << "batch " << batch << ": Star has duplicate edge (" << u << ',' << v << ")\n";
        return false;
      }
      matched[expected_id] = true;
      if (truth.tau[expected_id] != static_cast<uint32_t>(edge.iTrussness)) {
        std::cerr << "batch " << batch << ": Star trussness mismatch (" << u << ',' << v
                  << "), expected " << truth.tau[expected_id] << ", got " << edge.iTrussness << '\n';
        return false;
      }
    }
    if (checked != expected.m || checked != edge_count_) {
      std::cerr << "batch " << batch << ": Star live edge count " << checked
                << " differs from the oracle " << expected.m << " or the running count "
                << edge_count_ << '\n';
      return false;
    }
    return true;
  }

 private:
  myG graph_;
  uint64_t edge_count_;
  std::vector<int> recount_;

  void schedule_recount(int eid) {
    auto& edge = *graph_.findNode(eid);
    if (!edge.resident_recount) {
      recount_.push_back(eid);
      edge.resident_recount = true;
    }
  }

  void refresh_insertion_changes() {
    // An edge's deletion counters can only change if its own annotation
    // changed, or a triangle it belongs to contains a changed/new edge.
    for (int eid : graph_.resident_changes) {
      auto& edge = *graph_.findNode(eid);
      schedule_recount(eid);
      for (int left : edge.vLfE) schedule_recount(left);
      for (int right : edge.vRtE) schedule_recount(right);
      edge.resident_dirty = false;
    }
    graph_.resident_changes.clear();
    for (int eid : recount_) refresh_deletion_support(eid);
    recount_.clear();
  }

  void refresh_deletion_support(int eid) {
    auto& edge = *graph_.findNode(eid);
    edge.resident_recount = false;
    edge.iKSup = 0;
    edge.iKMSup = 0;
    // The released deletion initializer uses the minimum layer among
    // the edges tied at the triangle's minimum trussness.
    for (size_t i = 0; i < edge.vLfE.size(); ++i) {
      const auto& left = *graph_.findNode(edge.vLfE[i]);
      const auto& right = *graph_.findNode(edge.vRtE[i]);
      if (left.iTrussness < edge.iTrussness || right.iTrussness < edge.iTrussness) continue;
      ++edge.iKSup;
      int layer = edge.iLayer;
      if (left.iTrussness == edge.iTrussness) layer = std::min(layer, left.iLayer);
      if (right.iTrussness == edge.iTrussness) layer = std::min(layer, right.iLayer);
      if (layer >= edge.iLayer - 1) ++edge.iKMSup;
    }
  }
};

struct Options {
  std::string graph, stream, dataset = "NA", batches, phases;
  unsigned long runs = 1;
  unsigned long check_every = 1;
  bool timing_only = false;
};

Options parse(int argc, char** argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string_view arg = argv[i];
    if (arg == "--timing-only") {
      options.timing_only = true;
      continue;
    }
    if (++i == argc) throw std::runtime_error("missing value for " + std::string(arg));
    const std::string value = argv[i];
    if (arg == "--graph") options.graph = value;
    else if (arg == "--stream") options.stream = value;
    else if (arg == "--dataset") options.dataset = value;
    else if (arg == "--batch-csv") options.batches = value;
    else if (arg == "--phase-csv") {
      if (value.empty() || value.rfind("--", 0) == 0)
        throw std::runtime_error("--phase-csv requires a file path");
      options.phases = value;
    }
    else if (arg == "--runs") options.runs = std::stoul(value);
    else if (arg == "--check-every") options.check_every = std::stoul(value);
    else if (arg != "--threads" || value != "1")
      throw std::runtime_error("unsupported argument: " + std::string(arg));
  }
  if (options.graph.empty() || options.stream.empty() || options.batches.empty() ||
      !options.runs || (!options.timing_only && options.check_every != 1))
    throw std::runtime_error("provide --graph, --stream, --batch-csv, positive --runs, and "
                             "--check-every 1 or --timing-only");
  if (!options.phases.empty() && !options.timing_only)
    throw std::runtime_error("--phase-csv requires --timing-only");
  if (options.phases == options.batches)
    throw std::runtime_error("--phase-csv requires a separate output file");
  return options;
}
}  // namespace

int main(int argc, char** argv) try {
  const auto options = parse(argc, argv);
  std::ofstream output(options.batches);
  if (!output) throw std::runtime_error("cannot write batch CSV: " + options.batches);
  output.precision(12);
  output << "run,batch,raw_ops,deleted,inserted,peels,virtual_events,region_edges,"
            "parallel_jobs,elapsed_us,preprocess_us,kernel_us,verify_ok,final_m\n";
  std::ofstream phase_output;
  if (!options.phases.empty()) {
    phase_output.open(options.phases);
    if (!phase_output) throw std::runtime_error("cannot write phase CSV: " + options.phases);
    phase_output.precision(12);
    phase_output << "run,batch,elapsed_us,coalesce_us,partition_us,delete_us,insert_us,refresh_us\n";
  }
  std::cout.precision(12);
  std::cout << "dataset,method,threads,ablate,stream,batch_size,nbatches,run,build_s,total_maint_s,"
               "lat_mean_ms,lat_p50_ms,lat_p99_ms,lat_max_ms,throughput_eps,evals,seeds,rounds,"
               "region_max,rss_mb,verify_ok,verify_s,final_m,final_n\n";
  for (unsigned long run = 0; run < options.runs; ++run) {
    auto initial = DynGraph::load(options.graph);
    const uint32_t vertices = initial.n;
    const auto stream = load_stream(options.stream, vertices);
    if (stream.empty()) throw std::runtime_error("Star replay requires at least one batch");
    const auto start_build = now_s();
    ResidentStar star(initial);
    const double build_s = now_s() - start_build;
    DynGraph expected;
    if (options.timing_only) {
      initial = {};
    } else {
      expected = std::move(initial);
      if (!star.verify(expected, 0)) return 2;
    }
    std::vector<double> latencies;
    latencies.reserve(stream.size());
    double maintenance_s = 0;
    double verify_s = 0;
    uint64_t operations = 0;
    for (size_t i = 0; i < stream.size(); ++i) {
      const auto& batch = stream[i];
      StarPhaseTimes phases;
      const auto start = now_s();
      const auto counts = star.apply(batch, phase_output.is_open() ? &phases : nullptr);
      const double elapsed = now_s() - start;
      latencies.push_back(elapsed);
      maintenance_s += elapsed;
      operations += batch.size();
      bool verified = true;
      if (!options.timing_only) {
        const auto verify_start = now_s();
        for (const auto& op : batch) replay_edge(expected, op);
        verified = star.verify(expected, i + 1);
        verify_s += now_s() - verify_start;
      }
      const char* check = options.timing_only ? "" : (verified ? "1" : "0");
      output << run << ',' << i + 1 << ',' << batch.size() << ',' << counts.deleted << ','
             << counts.inserted << ",,,,," << elapsed * 1e6 << ",,," << check << ',' << star.edges() << '\n';
      if (phase_output.is_open()) {
        phase_output << run << ',' << i + 1 << ',' << elapsed * 1e6 << ',' << phases.coalesce_s * 1e6 << ','
                     << phases.partition_s * 1e6 << ',' << phases.delete_s * 1e6 << ','
                     << phases.insert_s * 1e6 << ',' << phases.refresh_s * 1e6 << '\n';
      }
      if (!verified) return 2;
    }
    LatStats stats;
    stats.compute(latencies);
    const double throughput = maintenance_s > 0 ? operations / maintenance_s : 0;
    std::cout << options.dataset << ",star-resident,1,-," << std::filesystem::path(options.stream).stem().string()
              << ',' << stream.front().size() << ',' << stream.size() << ',' << run << ',' << build_s << ','
              << maintenance_s << ',' << stats.mean * 1e3 << ',' << stats.p50 * 1e3 << ',' << stats.p99 * 1e3
              << ',' << stats.mx * 1e3 << ',' << throughput << ",,,,," << peak_rss_mb() << ','
              << (options.timing_only ? "" : "1") << ',' << verify_s << ',' << star.edges() << ',' << vertices << '\n';
  }
  if (!output) throw std::runtime_error("failed writing batch CSV");
  if (phase_output.is_open()) {
    phase_output.close();
    if (!phase_output) throw std::runtime_error("failed writing phase CSV");
  }
  return 0;
} catch (const std::exception& error) {
  std::cerr << error.what() << '\n';
  return 1;
}
