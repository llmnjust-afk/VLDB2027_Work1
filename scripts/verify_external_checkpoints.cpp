#include "replay.h"
#include "stream.h"

#include <filesystem>
#include <iostream>

namespace fs = std::filesystem;

static void check_snapshot(const DynGraph& graph, const fs::path& path) {
  std::ifstream input(path);
  if (!input) throw std::runtime_error("cannot open " + path.string());

  const auto truth = static_truss(graph);
  std::vector<uint8_t> seen(graph.cap_edges, 0);
  std::string line;
  size_t row = 0;
  while (std::getline(input, line)) {
    ++row;
    if (line.find_first_not_of(" \t\r") == std::string::npos) continue;

    std::istringstream fields(line);
    uint64_t u, v, label;
    std::string extra;
    if (!(fields >> u >> v >> label) || u >= graph.n || v >= graph.n ||
        u == v || label < 2 || label > UINT32_MAX || fields >> extra) {
      throw std::runtime_error("invalid u v trussness record on line " + std::to_string(row));
    }

    const auto key = DynGraph::ekey(static_cast<uint32_t>(u), static_cast<uint32_t>(v));
    const auto edge_id = graph.find(key);
    const auto edge = [u, v] { return "(" + std::to_string(u) + "," + std::to_string(v) + ")"; };
    if (edge_id < 0) throw std::runtime_error("extra edge " + edge());
    if (seen[edge_id]) throw std::runtime_error("duplicate edge " + edge());
    if (label != truth.tau[edge_id]) {
      throw std::runtime_error("trussness mismatch for " + edge() +
                               ": expected " + std::to_string(truth.tau[edge_id]) +
                               ", got " + std::to_string(label));
    }
    seen[edge_id] = 1;
  }
  if (input.bad()) throw std::runtime_error("failed reading " + path.string());

  for (uint32_t edge_id = 0; edge_id < graph.cap_edges; ++edge_id) {
    if (graph.alive(edge_id) && !seen[edge_id]) {
      throw std::runtime_error("missing edge (" + std::to_string(graph.eu[edge_id]) +
                               "," + std::to_string(graph.ev[edge_id]) + ")");
    }
  }
}

int main(int argc, char** argv) {
  const bool help = argc == 2 && std::string(argv[1]) == "--help";
  if (argc != 4) {
    std::cerr << "Usage: " << argv[0] << " INITIAL_GRAPH ORDERED_STREAM SNAPSHOT_DIR\n"
              << "For each 1-based batch, export batch-N.labels with one 'u v trussness'\n"
              << "row per live edge, using the vertex IDs in ORDERED_STREAM. An empty\n"
              << "checkpoint still requires its file. Run this outside timed replays.\n";
    return help ? 0 : 2;
  }

  size_t batch = 0;
  try {
    auto expected = DynGraph::load(argv[1]);
    const auto stream = load_stream(argv[2], expected.n);
    if (stream.empty()) throw std::runtime_error("stream contains no checkpoints");
    const fs::path snapshots = argv[3];
    uint64_t updates = 0;
    for (const auto& operations : stream) {
      ++batch;
      for (const auto& op : operations) replay_edge(expected, op);
      updates += operations.size();
      check_snapshot(expected, snapshots / ("batch-" + std::to_string(batch) + ".labels"));
    }
    std::cout << "VERIFIED " << batch << " checkpoints, " << updates << " ordered updates\n";
  } catch (const std::exception& error) {
    std::cerr << "checkpoint " << batch << ": " << error.what() << '\n';
    return 1;
  }
  return 0;
}
