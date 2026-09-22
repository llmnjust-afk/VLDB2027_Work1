#include "batch.h"
#include "incremental.h"
#include "stream.h"
#include <array>
#include <cstring>

static std::string g_dataset = "NA";

static void csv_header() {
  printf("dataset,method,threads,ablate,batch_size,nbatches,run,build_s,total_maint_s,"
         "lat_mean_ms,lat_p50_ms,lat_p99_ms,lat_max_ms,throughput_eps,evals,seeds,rounds,"
         "region_max,rss_mb,verify_ok,verify_s,final_m,final_n\n");
}

static void csv_row(const std::string& method, uint32_t threads, const std::string& ablate,
                    uint32_t bsize, uint32_t nb, uint32_t run, double build_s, double maint_s,
                    std::vector<double>& lats, uint64_t evals, uint64_t seeds, uint64_t rounds,
                    uint64_t region_max, bool verify_ok, double verify_s, uint64_t fm,
                    uint32_t fn) {
  LatStats L;
  L.compute(lats);
  double eps = maint_s > 0 ? (double)((uint64_t)nb * bsize) / maint_s : 0;
  printf("%s,%s,%u,%s,%u,%u,%u,%.3f,%.3f,%.4f,%.4f,%.4f,%.4f,%.1f,%llu,%llu,%llu,%llu,%.0f,"
         "%d,%.3f,%llu,%u\n",
         g_dataset.c_str(), method.c_str(), threads, ablate.c_str(), bsize, nb, run, build_s,
         maint_s, L.mean * 1e3, L.p50 * 1e3, L.p99 * 1e3, L.mx * 1e3, eps,
         (unsigned long long)evals, (unsigned long long)seeds, (unsigned long long)rounds,
         (unsigned long long)region_max, (double)peak_rss_mb(), verify_ok ? 1 : 0, verify_s,
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

static bool verify_now(DynGraph& g, const std::vector<uint16_t>& tau, double& verify_s) {
  double t0 = now_s();
  StaticTruss s = static_truss(g, 1);
  bool ok = true;
  for (uint32_t e = 0; e < g.cap_edges; ++e) {
    if (!g.alive(e)) continue;
    if (tau[e] != s.tau[e]) {
      ok = false;
      fprintf(stderr, "MISMATCH eid=%u (%u,%u) maintained=%u truth=%u\n", e, g.eu[e], g.ev[e],
              tau[e], s.tau[e]);
      break;
    }
  }
  verify_s = now_s() - t0;
  return ok;
}

static void run_batch(DynGraph& g, const std::vector<std::vector<Op>>& stream, uint32_t threads,
                      const std::string& ablate, uint32_t run_id, uint32_t check_every) {
  double t0 = now_s();
  uint64_t reserve = 0;
  for (auto& b : stream)
    for (auto& op : b)
      if (!op.del) reserve++;
  BatchMaintainer bm(g, threads, reserve);
  double build_s = now_s() - t0;
  std::vector<double> lats;
  bool ok = true;
  double vs = 0;
  double t1 = now_s();
  for (size_t bi = 0; bi < stream.size(); ++bi) {
    double tb = now_s();
    if (ablate == "nomerge")
      bm.apply_batch_nomerge(stream[bi]);
    else
      bm.apply_batch(stream[bi]);
    lats.push_back(now_s() - tb);
    if (check_every && (bi + 1) % check_every == 0) ok = verify_now(g, bm.tau, vs) && ok;
  }
  double maint = now_s() - t1;
  ok = verify_now(g, bm.tau, vs) && ok;
  BatchStats* st = &bm.stats;
  csv_row("batch", threads, ablate, 0, (uint32_t)stream.size(), run_id, build_s, maint, lats,
          st->evals, st->seeds, st->rounds, st->region_max, ok, vs, g.m, g.n);
}

static void run_peredge(DynGraph& g, const std::vector<std::vector<Op>>& stream, uint32_t run_id,
                        uint32_t check_every) {
  double t0 = now_s();
  PerEdgeMaintainer pm(g);
  double build_s = now_s() - t0;
  std::vector<double> lats;
  bool ok = true;
  double vs = 0;
  double t1 = now_s();
  for (size_t bi = 0; bi < stream.size(); ++bi) {
    double tb = now_s();
    for (auto& op : stream[bi]) {
      if (op.del)
        pm.remove(op.u, op.v);
      else
        pm.insert(op.u, op.v);
    }
    lats.push_back(now_s() - tb);
    if (check_every && (bi + 1) % check_every == 0) ok = verify_now(g, pm.tau, vs) && ok;
  }
  double maint = now_s() - t1;
  ok = verify_now(g, pm.tau, vs) && ok;
  csv_row("peredge", 1, "-", 0, (uint32_t)stream.size(), run_id, build_s, maint, lats,
          pm.stats.evals, 0, 0, pm.stats.region_max, ok, vs, g.m, g.n);
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
    save_graph_edgelist(g, arg_s(a, "--graph-out", "initial.txt"));
  }
  save_stream(batches, arg_s(a, "--stream-out", "stream.txt"));
  fprintf(stderr, "snap: initial edges=%zu stream batches=%zu ops=%zu\n", initial.size(),
          batches.size(), (size_t)(rows.size() - cut));
}

int main(int argc, char** argv) {
  if (argc < 2) {
    fprintf(stderr,
            "subcommands:\n"
            "  synth --n N --p P --out G | synth2 --clusters C --size S --bridge B --out G\n"
            "  gen --graph G --out S --batches K --batch-size B --p-insert P --seed R\n"
            "  snap --temporal T --fraction F --batch-size B --mode append|rewind "
            "--graph-out GO --stream-out SO\n"
            "  static --graph G --threads T\n"
            "  peredge --graph G --stream S [--check-every K] [--dataset NAME]\n"
            "  batch --graph G --stream S --threads T [--check-every K] [--ablate m] [--runs R]"
            " [--dataset NAME]\n");
    return 1;
  }
  std::vector<std::string> a(argv + 1, argv + argc);
  std::string cmd = a[0];
  g_dataset = arg_s(a, "--dataset", "NA");

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
  if (cmd == "static") {
    DynGraph g = DynGraph::load(arg_s(a, "--graph", ""));
    double t0 = now_s();
    StaticTruss s = static_truss(g, arg_u32(a, "--threads", 1));
    double el = now_s() - t0;
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
  if (cmd == "peredge" || cmd == "batch") {
    csv_header();
    uint32_t runs = arg_u32(a, "--runs", 1);
    uint32_t check_every = arg_u32(a, "--check-every", 0);
    for (uint32_t r = 0; r < runs; ++r) {
      DynGraph g = DynGraph::load(arg_s(a, "--graph", ""));
      auto stream = load_stream(arg_s(a, "--stream", ""));
      if (cmd == "peredge")
        run_peredge(g, stream, r, check_every);
      else
        run_batch(g, stream, arg_u32(a, "--threads", 1), arg_s(a, "--ablate", "full"), r,
                  check_every);
    }
    return 0;
  }
  fprintf(stderr, "unknown subcommand %s\n", cmd.c_str());
  return 1;
}
