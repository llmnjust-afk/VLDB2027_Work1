#include "luo_edge.h"
#include "replay.h"
#include "stream.h"

#include <filesystem>
#include <iomanip>
#include <iostream>
#include <memory>

namespace {

struct Options {
  std::string graph, stream, batch_csv, labels_dir, dataset = "NA";
  uint32_t threads = 1;
  bool timing_only = false;
};

Options parse(int argc, char** argv) {
  Options options;
  for (int i = 1; i < argc; ++i) {
    const std::string flag = argv[i];
    if (flag == "--timing-only") {
      options.timing_only = true;
      continue;
    }
    if (++i == argc) throw std::runtime_error("missing value for " + flag);
    const std::string value = argv[i];
    if (flag == "--graph") options.graph = value;
    else if (flag == "--stream") options.stream = value;
    else if (flag == "--dataset") options.dataset = value;
    else if (flag == "--batch-csv") options.batch_csv = value;
    else if (flag == "--labels-dir") options.labels_dir = value;
    else if (flag == "--threads") {
      size_t used = 0;
      const auto threads = std::stoull(value, &used);
      if (!threads || threads > UINT32_MAX || used != value.size())
        throw std::runtime_error("--threads requires a positive integer");
      options.threads = static_cast<uint32_t>(threads);
    } else if (!((flag == "--runs" || flag == "--check-every") && value == "1")) {
      throw std::runtime_error("unsupported option: " + flag + " " + value);
    }
  }
  if (options.graph.empty() || options.stream.empty() || options.batch_csv.empty())
    throw std::runtime_error("provide --graph, --stream and --batch-csv");
  if (options.timing_only && !options.labels_dir.empty())
    throw std::runtime_error("--labels-dir is available in verification replays");
  if (options.dataset.find_first_of(",\r\n") != std::string::npos)
    throw std::runtime_error("--dataset must be a single CSV field without commas");
  return options;
}

void write_labels(const DynGraph& graph, const std::vector<uint32_t>& labels,
                  const std::filesystem::path& directory, size_t batch) {
  const auto path = directory / ("batch-" + std::to_string(batch) + ".labels");
  std::ofstream file(path);
  for (uint32_t edge = 0; edge < graph.cap_edges; ++edge) {
    if (graph.alive(edge))
      file << graph.eu[edge] << ' ' << graph.ev[edge] << ' ' << labels[edge] << '\n';
  }
  if (!file) throw std::runtime_error("cannot write " + path.string());
}

}  // namespace

int main(int argc, char** argv) try {
  if (argc == 2 && std::string(argv[1]) == "--help") {
    std::cout << "Independent edge-update reproduction of Luo et al. (IEEE TC 2023).\n"
                 "--graph PATH --stream PATH --batch-csv PATH [--dataset NAME] [--threads N]\n"
                 "[--check-every 1 or --timing-only] [--labels-dir DIR for verification]\n";
    return 0;
  }
  const auto options = parse(argc, argv);
  auto graph = DynGraph::load(options.graph);
  const auto stream = load_stream(options.stream, graph.n);
  if (stream.empty()) throw std::runtime_error("stream contains no checkpoints");
  std::ofstream batches(options.batch_csv);
  if (!batches) throw std::runtime_error("cannot write " + options.batch_csv);
  batches << "run,batch,raw_ops,deleted,inserted,deletion_groups,insertion_groups,"
             "elapsed_us,verify_ok,final_m\n" << std::setprecision(12);

  auto oracle = options.timing_only ? nullptr : std::make_unique<ReplayOracle>(graph);
  const auto start_build = now_s();
  luo::EdgeMaintainer maintainer(graph, options.threads);
  const double build_s = now_s() - start_build;
  const char* verified = oracle ? "1" : "";
  double maintenance_s = 0;
  if (oracle && !oracle->check(graph, maintainer.labels(), 0, {})) return 2;
  if (!options.labels_dir.empty()) {
    std::filesystem::create_directories(options.labels_dir);
    write_labels(graph, maintainer.labels(), options.labels_dir, 0);
  }

  for (size_t i = 0; i < stream.size(); ++i) {
    const auto& operations = stream[i];
    const auto start = now_s();
    const auto counts = maintainer.apply(operations);
    const double elapsed = now_s() - start;
    maintenance_s += elapsed;
    if (oracle) {
      oracle->advance(operations);
      if (!oracle->check(graph, maintainer.labels(), i + 1, operations)) return 2;
      if (!options.labels_dir.empty())
        write_labels(graph, maintainer.labels(), options.labels_dir, i + 1);
    }
    batches << "0," << i + 1 << ',' << operations.size() << ','
            << counts.deleted << ',' << counts.inserted << ',' << counts.deletion_groups << ','
            << counts.insertion_groups << ',' << elapsed * 1e6 << ',' << verified << ','
            << graph.m << '\n';
  }
  if (!batches) throw std::runtime_error("cannot write " + options.batch_csv);
  std::cout << "dataset,method,threads,nbatches,run,build_s,total_maint_s,rss_mb,verify_ok,final_m\n"
            << std::setprecision(12) << options.dataset << ",luo-edge," << options.threads << ','
            << stream.size() << ",0," << build_s << ',' << maintenance_s << ',' << peak_rss_mb()
            << ',' << verified << ',' << graph.m << '\n';
} catch (const std::exception& error) {
  std::cerr << "luo-edge: " << error.what() << '\n';
  return 1;
}
