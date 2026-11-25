#include "adaptive_online.h"
#include "incremental.h"
#include "stream.h"
#include "replay.h"
#ifdef HAVE_MSP
#include "msp_baseline.h"
#endif
#include <array>
#include <cstring>
#include <optional>
#include <unordered_set>

static std::string g_dataset = "NA";

static std::string g_stream_name = "NA";
static std::string g_graph_out = "";


static void csv_header() {
  printf("dataset,method,threads,ablate,stream,batch_size,nbatches,run,build_s,total_maint_s,"
         "lat_mean_ms,lat_p50_ms,lat_p99_ms,lat_max_ms,throughput_eps,evals,seeds,rounds,"
         "region_max,rss_mb,verify_ok,verify_s,final_m,final_n\n");
}

static void csv_row(const std::string& method, uint32_t threads, const std::string& ablate,
                    uint32_t bsize, uint32_t nb, uint32_t run, uint64_t operations, double build_s, double maint_s,
                    std::vector<double>& lats, uint64_t evals, uint64_t seeds, uint64_t rounds,
                    uint64_t region_max, const char* verify_ok, double verify_s, uint64_t fm,
                    uint32_t fn) {
  LatStats L;
  L.compute(lats);
  double eps = maint_s > 0 ? static_cast<double>(operations) / maint_s : 0;
  printf("%s,%s,%u,%s,%s,%u,%u,%u,%.6f,%.6f,%.4f,%.4f,%.4f,%.4f,%.1f,%llu,%llu,%llu,%llu,%.0f,"
         "%s,%.6f,%llu,%u\n",
         g_dataset.c_str(), method.c_str(), threads, ablate.c_str(), g_stream_name.c_str(), bsize, nb, run, build_s,
         maint_s, L.mean * 1e3, L.p50 * 1e3, L.p99 * 1e3, L.mx * 1e3, eps,
         (unsigned long long)evals, (unsigned long long)seeds, (unsigned long long)rounds,
         (unsigned long long)region_max, (double)peak_rss_mb(), verify_ok, verify_s,
         (unsigned long long)fm, fn);
}

static uint32_t arg_u32(const std::vector<std::string>& a, const std::string& k, uint32_t dflt) {
  for (size_t i = 0; i + 1 < a.size(); ++i)
    if (a[i] == k) return (uint32_t)strtoull(a[i + 1].c_str(), nullptr, 10);
  return dflt;
}
static double arg_f(const std::vector<std::string>& a, const std::string& k, double dflt) {
  for (size_t i = 0; i + 1 < a.size(); ++i)
    if (a[i] == k) return atof(a[i + 1].c_str());
  return dflt;
}
static std::string arg_s(const std::vector<std::string>& a, const std::string& k,
                         const std::string& dflt) {
  for (size_t i = 0; i + 1 < a.size(); ++i)
    if (a[i] == k) return a[i + 1];
  return dflt;
}

static void write_labels(const DynGraph& graph, const std::vector<uint32_t>& tau,
                         const std::string& path) {
  std::ofstream output(path);
  if (!output) throw std::runtime_error("cannot write labels: " + path);
  for (uint32_t e = 0; e < graph.cap_edges; ++e)
    if (graph.alive(e)) output << graph.eu[e] << ' ' << graph.ev[e] << ' ' << tau[e] << '\n';
}

static bool is_pruned_online_adaptive(const std::string& ablate) {
  return ablate == "adaptive-pruned-recent" || ablate == "adaptive-pruned-cost";
}

static bool is_online_adaptive(const std::string& ablate) {
  return ablate == "adaptive-recent" || ablate == "adaptive-cost" || is_pruned_online_adaptive(ablate);
}

static const char* batch_route_name(BatchRoute route, bool pruned = false) {
  if (route == BatchRoute::Static) return "static";
  return pruned ? "fused-pruned" : "fused";
}

static bool run_replay(DynGraph& graph, const std::vector<std::vector<Op>>& stream,
                       const std::string& method, const std::string& ablate, uint32_t threads,
                       uint32_t run_id, uint32_t check_every, bool timing_only,
                       std::ostream* batch_output, std::ostream* phase_output,
                       std::ostream* peel_scan_output, std::ostream* route_output,
                       std::ostream* online_route_output, uint32_t online_probe_interval,
                       const std::string& labels_out) {
  std::unique_ptr<ReplayOracle> oracle;
  if (!timing_only) oracle = std::make_unique<ReplayOracle>(graph);

  std::unique_ptr<BatchMaintainer> batch;
  std::unique_ptr<PerEdgeMaintainer> peredge;
  std::optional<OnlineBatchRouter> online;
  std::vector<uint32_t> static_labels;
  const bool pruned_online = is_pruned_online_adaptive(ablate);
  const auto initial_start = now_s();
  if (method == "batch") {
    batch = std::make_unique<BatchMaintainer>(graph, threads);
    batch->ablate_allseeds = ablate == "allseeds";
    batch->certify_low_support = ablate == "certified";
    batch->fuse_closure_recount = ablate == "fused";
    batch->prune_below_four = ablate == "fused-pruned" || pruned_online;
    batch->record_peel_scans = peel_scan_output != nullptr;
    if (is_online_adaptive(ablate)) {
      const bool use_work = ablate == "adaptive-cost" || ablate == "adaptive-pruned-cost";
      online.emplace(use_work, online_probe_interval);
      batch->record_region_degrees = use_work;
    }
  } else if (method == "peredge") {
    threads = 1;
    peredge = std::make_unique<PerEdgeMaintainer>(graph);
  }
  const double build_s = now_s() - initial_start;
  const auto& tau = batch ? batch->tau : peredge ? peredge->tau : static_labels;

  std::vector<double> latencies;
  latencies.reserve(stream.size());
  bool ok = true;
  const bool adaptive = ablate == "adaptive";
  double verify_s = 0, maint_s = 0;
  uint64_t operations = 0, evals = 0, seeds = 0, rounds = 0, region = 0;

  for (size_t i = 0; i < stream.size(); ++i) {
    const auto& ops = stream[i];
    BatchPhaseTimes phases;
    const auto start = now_s();
    BatchStats measures;
    BatchRoute route = BatchRoute::Fused;
    OnlineRoute online_route{};
    double decision_s = 0, path_s = 0;
    double prep_s = 0, kernel_s = 0;
    if (batch) {
      batch->stats = {};
      if (online) {
        const double decision_start = now_s();
        online_route = online->choose(graph.m, ops.size());
        const double path_start = now_s();
        decision_s = path_start - decision_start;
        batch->apply_routed_batch(ops, online_route.path, kernel_s);
        path_s = now_s() - path_start;
        online->observe(online_route, batch->stats, path_s);
      } else if (adaptive) route = batch->apply_adaptive(ops, kernel_s);
      else if (ablate == "nomerge") batch->apply_batch_nomerge(ops);
      else batch->apply_batch(ops, phase_output ? &phases : nullptr);
      measures = batch->stats;
    } else if (peredge) {
      peredge->stats = {};
      for (const auto& op : ops) {
        const bool changed = op.del ? peredge->remove(op.u, op.v) : peredge->insert(op.u, op.v);
        if (changed) ++(op.del ? measures.deleted : measures.inserted);
      }
      measures.evals = peredge->stats.evals;
      measures.region_max = peredge->stats.region_max;
    } else {
      if (method == "static-net") {
        const auto updates = coalesce_batch_updates(graph, ops);
        apply_batch_deletions(graph, updates.deletes, [](uint32_t) {});
        apply_batch_insertions(graph, updates.inserts, [](uint32_t) {});
        measures.deleted = updates.deletes.size();
        measures.inserted = updates.inserts.size();
      } else {
        apply_ordered_updates(graph, ops, measures);
      }
      const auto kernel_start = now_s();
#ifdef HAVE_MSP
      if (method == "msp") static_labels = msp_truss(graph, threads, prep_s, kernel_s);
      else
#endif
      {
        static_labels = static_truss(graph, threads).tau;
        kernel_s = now_s() - kernel_start;
      }
      measures.peels = 1;
    }
    const double latency = now_s() - start;
    maint_s += latency;
    operations += ops.size();
    latencies.push_back(latency);
    evals += measures.evals;
    seeds += measures.seeds;
    rounds += measures.rounds;
    region = std::max(region, measures.region_max);

    const char* checked = "";
    if (oracle) {
      const auto verify_start = now_s();
      oracle->advance(ops);
      if ((check_every && (i + 1) % check_every == 0) || i + 1 == stream.size()) {
        ok = oracle->check(graph, tau, i + 1, ops);
        checked = ok ? "1" : "0";
      }
      verify_s += now_s() - verify_start;
    }
    if (batch_output) {
      *batch_output << run_id << ',' << i + 1 << ',' << ops.size() << ','
                    << measures.deleted << ',' << measures.inserted << ',' << measures.peels << ','
                    << measures.virtual_events << ',' << measures.region_max << ','
                    << measures.parallel_jobs << ',' << latency * 1e6 << ','
                    << prep_s * 1e6 << ',' << kernel_s * 1e6 << ','
                    << checked << ',' << graph.m << '\n';
    }
    if (phase_output) {
      *phase_output << run_id << ',' << i + 1 << ',' << latency * 1e6 << ','
                    << phases.coalesce_s * 1e6 << ',' << phases.witness_s * 1e6 << ','
                    << phases.apply_s * 1e6 << ',' << phases.closure_s * 1e6 << ','
                    << phases.recount_s * 1e6 << ',' << phases.peel_s * 1e6 << '\n';
    }
    if (peel_scan_output) {
      *peel_scan_output << run_id << ',' << i + 1 << ',' << measures.region_max << ','
                        << measures.peel_certified_edges << ',' << measures.peel_scanned_edges << ','
                        << measures.peel_scan_degree_sum << '\n';
    }
    if (route_output)
      *route_output << run_id << ',' << i + 1 << ',' << batch_route_name(route) << '\n';
    if (online_route_output)
      *online_route_output << run_id << ',' << i + 1 << ','
                           << batch_route_name(online_route.path, pruned_online)
                           << ',' << online_route.reason << ',' << online_route.switched << ','
                           << decision_s * 1e6 << ',' << path_s * 1e6 << ','
                           << online_route.pre_edges << ',' << online_route.raw_ops << ','
                           << online_route.prior_fused_region_edges << ','
                           << online_route.prior_fused_degree_sum << ',' << measures.region_max
                           << ',' << measures.region_degree_sum << '\n';
    if (!ok) break;
  }
  if (ok && !g_graph_out.empty()) save_graph_edgelist(graph, g_graph_out);
  if (ok && !labels_out.empty()) {
    if (!batch && !peredge && stream.empty()) static_labels = static_truss(graph).tau;
    write_labels(graph, tau, labels_out);
  }
  const uint32_t first_size = stream.empty() ? 0 : static_cast<uint32_t>(stream.front().size());
  csv_row(method, threads, ablate, first_size, latencies.size(), run_id, operations,
          build_s, maint_s, latencies, evals, seeds, rounds, region,
          timing_only ? "" : ok ? "1" : "0", verify_s, graph.m, graph.n);
  return ok;
}

static void cmd_snap(const std::vector<std::string>& a) {
  std::string tp = arg_s(a, "--temporal", "");
  double frac = arg_f(a, "--fraction", 0.9);
  uint32_t bsize = arg_u32(a, "--batch-size", 1000);
  std::string mode = arg_s(a, "--mode", "append");
  FILE* f = fopen(tp.c_str(), "r");
  if (!f) {
    fprintf(stderr, "cannot open %s\n", tp.c_str());
    exit(1);
  }
  std::vector<std::array<uint64_t, 4>> rows;
  char line[1 << 10];
  uint64_t idx = 0;
  while (fgets(line, sizeof(line), f)) {
    if (line[0] == '#' || line[0] == '%') continue;
    uint64_t x, y, t;
    if (sscanf(line, "%lu %lu %lu", &x, &y, &t) == 3) rows.push_back({t, idx++, x, y});
  }
  fclose(f);
  std::sort(rows.begin(), rows.end());
  std::unordered_map<uint64_t, uint32_t> idmap;
  auto map_id = [&](uint64_t x) {
    auto it = idmap.find(x);
    if (it == idmap.end()) {
      uint32_t id = (uint32_t)idmap.size();
      idmap[x] = id;
      return id;
    }
    return it->second;
  };
  for (auto& r : rows) {
    map_id(r[2]);
    map_id(r[3]);
  }
  uint64_t cut = (uint64_t)(rows.size() * frac);
  std::unordered_set<uint64_t> seen;
  std::vector<std::pair<uint32_t, uint32_t>> initial;
  std::vector<std::vector<Op>> batches;
  std::vector<Op> cur;
  auto key2 = [&](uint32_t u, uint32_t v) {
    return u < v ? ((uint64_t)u << 32) | v : ((uint64_t)v << 32) | u;
  };
  if (mode == "rewind") {
    for (auto& r : rows) {
      uint32_t u = map_id(r[2]), v = map_id(r[3]);
      if (u == v) continue;
      uint64_t k = key2(u, v);
      if (seen.insert(k).second) initial.push_back({u, v});
    }
    for (uint64_t i = rows.size(); i-- > cut;) {
      uint32_t u = map_id(rows[i][2]), v = map_id(rows[i][3]);
      if (u == v) continue;
      uint64_t k = key2(u, v);
      if (!seen.count(k)) continue;
      if (seen.erase(k))
        cur.push_back({1, u, v});
      if (cur.size() >= bsize) {
        batches.push_back(cur);
        cur.clear();
      }
    }
  } else {
    for (uint64_t i = 0; i < cut; ++i) {
      uint32_t u = map_id(rows[i][2]), v = map_id(rows[i][3]);
      if (u == v) continue;
      uint64_t k = key2(u, v);
      if (seen.insert(k).second) initial.push_back({u, v});
    }
    for (uint64_t i = cut; i < rows.size(); ++i) {
      uint32_t u = map_id(rows[i][2]), v = map_id(rows[i][3]);
      if (u == v) continue;
      uint64_t k = key2(u, v);
      if (!seen.insert(k).second) continue;
      cur.push_back({0, u, v});
      if (cur.size() >= bsize) {
        batches.push_back(cur);
        cur.clear();
      }
    }
  }
  if (!cur.empty()) batches.push_back(cur);
  {
    DynGraph g;
    g.set_vertices((uint32_t)idmap.size());
    g.reserve_edges((uint32_t)(initial.size() + 16));
    g.init_hash(initial.size() * 2 + 64);
    for (auto& e : initial) g.add_edge(e.first, e.second);
    save_graph_edgelist(g, arg_s(a, "--graph-out", "initial.txt"), true);
  }
  save_stream(batches, arg_s(a, "--stream-out", "stream.txt"));
  fprintf(stderr, "snap: initial edges=%zu stream batches=%zu ops=%zu\n", initial.size(),
          batches.size(), (size_t)(rows.size() - cut));
}

int main(int argc, char** argv) try {
  if (argc < 2) {
    fprintf(stderr,
            "subcommands:\n"
            "  synth --n N --p P --out G | synth2 --clusters C --size S --bridge B --out G\n"
            "  gen --graph G --out S --batches K --batch-size B --p-insert P --seed R\n"
            "  snap --temporal T --fraction F --batch-size B --mode append|rewind "
            "--graph-out GO --stream-out SO\n"
            "  static --graph G --threads T [--stream S]\n"
            "  static-net --graph G --threads T --stream S\n"
            "  msp --graph G --threads T --stream S (MSP-enabled build)\n"
            "  peredge --graph G --stream S [--check-every K] [--dataset NAME]\n"
            "  batch --graph G --stream S --threads T [--check-every K]"
            " [--ablate full|nomerge|allseeds|certified|fused|fused-pruned|adaptive|adaptive-recent|adaptive-cost"
            "|adaptive-pruned-recent|adaptive-pruned-cost]"
            " [--runs R]"
            " [--dataset NAME]\n"
            "  replay options: --batch-csv FILE --labels-out FILE --timing-only\n"
            "  profile Full, Certified, Fused or Fused-Pruned with --timing-only --phase-csv FILE\n"
            "  count Full, Certified, Fused or Fused-Pruned peel scans with --peel-scan-csv FILE\n"
            "  record Adaptive routes with --route-csv FILE\n"
            "  online Adaptive variants: --online-probe-interval N (default 8) --online-route-csv FILE\n");
    return 1;
  }
  std::vector<std::string> a(argv + 1, argv + argc);
  std::string cmd = a[0];
  g_dataset = arg_s(a, "--dataset", "NA");
  const std::string ablate = cmd == "batch" ? arg_s(a, "--ablate", "full") : "-";
  const bool adaptive = cmd == "batch" && ablate == "adaptive";
  const bool online_adaptive = cmd == "batch" && is_online_adaptive(ablate);
  const bool can_profile = cmd == "batch" &&
                           (ablate == "full" || ablate == "certified" ||
                            ablate == "fused" || ablate == "fused-pruned");
  const auto phase_option = std::find(a.begin(), a.end(), "--phase-csv");
  const bool timing_only = std::find(a.begin(), a.end(), "--timing-only") != a.end();
  const auto phase_path = arg_s(a, "--phase-csv", "");
  if (phase_option != a.end()) {
    if (phase_path.empty() || phase_path.rfind("--", 0) == 0)
      throw std::runtime_error("--phase-csv requires a file path");
    if (!can_profile || !timing_only)
      throw std::runtime_error("--phase-csv requires Full, Certified, Fused or Fused-Pruned with --timing-only");
    for (const auto* flag : {"--batch-csv", "--labels-out", "--graph-out"})
      if (phase_path == arg_s(a, flag, ""))
        throw std::runtime_error("--phase-csv requires a separate output file");
  }
  const auto peel_scan_option = std::find(a.begin(), a.end(), "--peel-scan-csv");
  const auto peel_scan_path = arg_s(a, "--peel-scan-csv", "");
  if (peel_scan_option != a.end()) {
    if (peel_scan_path.empty() || peel_scan_path.rfind("--", 0) == 0)
      throw std::runtime_error("--peel-scan-csv requires a file path");
    if (!can_profile)
      throw std::runtime_error("--peel-scan-csv requires Full, Certified, Fused or Fused-Pruned");
    for (const auto* flag : {"--batch-csv", "--labels-out", "--graph-out", "--phase-csv"})
      if (peel_scan_path == arg_s(a, flag, ""))
        throw std::runtime_error("--peel-scan-csv requires a separate output file");
  }
  const auto route_option = std::find(a.begin(), a.end(), "--route-csv");
  const auto route_path = arg_s(a, "--route-csv", "");
  if (route_option != a.end()) {
    if (route_path.empty() || route_path.rfind("--", 0) == 0)
      throw std::runtime_error("--route-csv requires a file path");
    if (!adaptive) throw std::runtime_error("--route-csv requires Adaptive");
    for (const auto* flag : {"--batch-csv", "--labels-out", "--graph-out"})
      if (route_path == arg_s(a, flag, ""))
        throw std::runtime_error("--route-csv requires a separate output file");
  }
  const auto online_path = arg_s(a, "--online-route-csv", "");
  if (std::find(a.begin(), a.end(), "--online-route-csv") != a.end()) {
    if (online_path.empty() || online_path.rfind("--", 0) == 0)
      throw std::runtime_error("--online-route-csv requires a file path");
    if (!online_adaptive)
      throw std::runtime_error("--online-route-csv requires an online Adaptive variant");
    for (const auto* flag : {"--batch-csv", "--labels-out", "--graph-out"})
      if (online_path == arg_s(a, flag, ""))
        throw std::runtime_error("--online-route-csv requires a separate output file");
  }
  uint32_t online_probe_interval = 8;
  if (std::find(a.begin(), a.end(), "--online-probe-interval") != a.end()) {
    if (!online_adaptive)
      throw std::runtime_error("--online-probe-interval requires an online Adaptive variant");
    const auto interval = arg_s(a, "--online-probe-interval", "");
    if (interval.empty() || interval.find_first_not_of("0123456789") != std::string::npos)
      throw std::runtime_error("--online-probe-interval requires a positive integer");
    const auto parsed = std::stoul(interval);
    if (!parsed || parsed > UINT32_MAX)
      throw std::runtime_error("--online-probe-interval is outside the supported range");
    online_probe_interval = static_cast<uint32_t>(parsed);
  }

  if (cmd == "synth") {
    synth_graph(arg_u32(a, "--n", 200), arg_f(a, "--p", 0.2), arg_s(a, "--out", "synth.txt"));
    return 0;
  }
  if (cmd == "synth2") {
    synth_clusters(arg_u32(a, "--clusters", 50), arg_u32(a, "--size", 30),
                   arg_u32(a, "--bridge", 200), arg_s(a, "--out", "synth2.txt"));
    return 0;
  }
  if (cmd == "gen") {
    DynGraph g = DynGraph::load(arg_s(a, "--graph", ""));
    std::vector<std::vector<Op>> out;
    gen_random_stream(g, arg_u32(a, "--batches", 100), arg_u32(a, "--batch-size", 100),
                      arg_f(a, "--p-insert", 0.8), arg_u32(a, "--seed", 1), out);
    save_stream(out, arg_s(a, "--out", "stream.txt"));
    return 0;
  }
  if (cmd == "snap") {
    cmd_snap(a);
    return 0;
  }
  const std::string stream_path = arg_s(a, "--stream", "");
  if (cmd == "static" && stream_path.empty()) {
    DynGraph g = DynGraph::load(arg_s(a, "--graph", ""));
    double t0 = now_s();
    StaticTruss s = static_truss(g, arg_u32(a, "--threads", 1));
    double el = now_s() - t0;
    const std::string labels_out = arg_s(a, "--labels-out", "");
    if (!labels_out.empty()) write_labels(g, s.tau, labels_out);
    uint64_t sum = 0;
    uint32_t mx = 0;
    for (uint32_t e = 0; e < g.cap_edges; ++e)
      if (g.alive(e)) {
        sum += s.tau[e];
        if (s.tau[e] > mx) mx = s.tau[e];
      }
    printf("static graph=%s n=%u m=%llu time_s=%.3f tau_sum=%llu tau_max=%u\n", g_dataset.c_str(),
           g.n, (unsigned long long)g.m, el, (unsigned long long)sum, mx);
    return 0;
  }
  if (cmd == "peredge" || cmd == "batch" || cmd == "static" || cmd == "static-net" || cmd == "msp") {
#ifndef HAVE_MSP
    if (cmd == "msp") throw std::runtime_error("use the MSP-enabled binary for msp");
#endif
    if (cmd == "batch" && !can_profile && !adaptive && !online_adaptive &&
        ablate != "nomerge" && ablate != "allseeds")
      throw std::runtime_error("unknown ablation: " + ablate);
    const uint32_t runs = arg_u32(a, "--runs", 1);
    const uint32_t check_every = arg_u32(a, "--check-every", 1);
    const uint32_t threads = arg_u32(a, "--threads", 1);
    if (!runs || !threads) throw std::runtime_error("runs and threads must be positive");
    const auto csv_path = arg_s(a, "--batch-csv", "");
    std::ofstream batch_csv;
    if (!csv_path.empty()) {
      batch_csv.open(csv_path);
      if (!batch_csv) throw std::runtime_error("cannot write batch CSV: " + csv_path);
      batch_csv.precision(12);
      batch_csv << "run,batch,raw_ops,deleted,inserted,peels,virtual_events,region_edges,"
                   "parallel_jobs,elapsed_us,preprocess_us,kernel_us,verify_ok,final_m\n";
    }
    std::ofstream phase_csv;
    if (!phase_path.empty()) {
      phase_csv.open(phase_path);
      if (!phase_csv) throw std::runtime_error("cannot write phase CSV: " + phase_path);
      phase_csv.precision(12);
      phase_csv << "run,batch,elapsed_us,coalesce_us,witness_us,apply_us,closure_us,recount_us,peel_us\n";
    }
    std::ofstream peel_scan_csv;
    if (!peel_scan_path.empty()) {
      peel_scan_csv.open(peel_scan_path);
      if (!peel_scan_csv) throw std::runtime_error("cannot write peel scan CSV: " + peel_scan_path);
      peel_scan_csv << "run,batch,region_edges,certified_edges,scanned_edges,scan_degree_sum\n";
    }
    std::ofstream route_csv;
    if (!route_path.empty()) {
      route_csv.open(route_path);
      if (!route_csv) throw std::runtime_error("cannot write route CSV: " + route_path);
      route_csv << "run,batch,route\n";
    }
    std::ofstream online_route_csv;
    if (!online_path.empty()) {
      online_route_csv.open(online_path);
      if (!online_route_csv) throw std::runtime_error("cannot write online route CSV: " + online_path);
      online_route_csv.precision(12);
      online_route_csv << "run,batch,route,reason,switched,decision_us,path_us,pre_edges,raw_ops,"
                          "prior_fused_region_edges,prior_fused_degree_sum,region_edges,region_degree_sum\n";
    }
    const auto basename = stream_path.find_last_of("/\\");
    g_stream_name = stream_path.substr(basename == std::string::npos ? 0 : basename + 1);
    const auto extension = g_stream_name.find_last_of('.');
    if (extension != std::string::npos && g_stream_name.substr(extension) == ".txt")
      g_stream_name.resize(extension);
    const auto graph_path = arg_s(a, "--graph", "");
    csv_header();
    for (uint32_t run = 0; run < runs; ++run) {
      auto graph = DynGraph::load(graph_path);
      const auto stream = load_stream(stream_path, graph.n);
      g_graph_out = run + 1 == runs ? arg_s(a, "--graph-out", "") : "";
      const std::string labels_out = run + 1 == runs ? arg_s(a, "--labels-out", "") : "";
      if (!run_replay(graph, stream, cmd, ablate, threads, run, check_every, timing_only,
                      batch_csv.is_open() ? &batch_csv : nullptr,
                      phase_csv.is_open() ? &phase_csv : nullptr,
                      peel_scan_csv.is_open() ? &peel_scan_csv : nullptr,
                      route_csv.is_open() ? &route_csv : nullptr,
                      online_route_csv.is_open() ? &online_route_csv : nullptr,
                      online_probe_interval, labels_out))
        return 2;
    }
    if (phase_csv.is_open()) {
      phase_csv.close();
      if (!phase_csv) throw std::runtime_error("failed writing phase CSV");
    }
    if (peel_scan_csv.is_open()) {
      peel_scan_csv.close();
      if (!peel_scan_csv) throw std::runtime_error("failed writing peel scan CSV");
    }
    if (route_csv.is_open()) {
      route_csv.close();
      if (!route_csv) throw std::runtime_error("failed writing route CSV");
    }
    if (online_route_csv.is_open()) {
      online_route_csv.close();
      if (!online_route_csv) throw std::runtime_error("failed writing online route CSV");
    }
    return 0;
  }
  fprintf(stderr, "unknown subcommand %s\n", cmd.c_str());
  return 1;
} catch (const std::exception& error) {
  fprintf(stderr, "%s\n", error.what());
  return 1;
}
