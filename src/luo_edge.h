#pragma once

#include "batch_updates.h"
#include "luo_edge_groups.h"
#include "luo_edge_traversal.h"
#include "truss_static.h"

#include <condition_variable>
#include <exception>
#include <functional>
#include <map>
#include <mutex>

namespace luo {

// Assign independent trussness levels to a fixed pool, as in Section 4.3.
// The caller uses one slot in the requested thread budget.
class LevelPool {
 public:
  explicit LevelPool(uint32_t threads) {
    workers_.reserve(threads - 1);
    try {
      for (uint32_t t = 1; t < threads; ++t)
        workers_.emplace_back([this] { worker(); });
    } catch (...) {
      stop();
      throw;
    }
  }

  ~LevelPool() { stop(); }

  void run(size_t jobs, const std::function<void(size_t)>& function) {
    if (jobs < 2 || workers_.empty()) {
      for (size_t i = 0; i < jobs; ++i) function(i);
      return;
    }

    {
      std::lock_guard<std::mutex> lock(mutex_);
      next_ = 0;
      jobs_ = jobs;
      function_ = &function;
      pending_ = workers_.size();
      failure_ = nullptr;
      ++generation_;
    }
    start_.notify_all();
    consume();

    std::unique_lock<std::mutex> lock(mutex_);
    done_.wait(lock, [this] { return pending_ == 0; });
    if (failure_) std::rethrow_exception(failure_);
  }

 private:
  std::vector<std::thread> workers_;
  std::atomic<size_t> next_{0};
  size_t jobs_ = 0, generation_ = 0, pending_ = 0;
  const std::function<void(size_t)>* function_ = nullptr;
  std::exception_ptr failure_;
  std::mutex mutex_;
  std::condition_variable start_, done_;
  bool stopping_ = false;

  void consume() {
    try {
      for (size_t i = next_++; i < jobs_; i = next_++) (*function_)(i);
    } catch (...) {
      std::lock_guard<std::mutex> lock(mutex_);
      if (!failure_) failure_ = std::current_exception();
      next_ = jobs_;
    }
  }

  void worker() {
    size_t seen = 0;
    std::unique_lock<std::mutex> lock(mutex_);
    for (;;) {
      start_.wait(lock, [this, &seen] { return stopping_ || generation_ != seen; });
      if (stopping_) return;
      seen = generation_;
      lock.unlock();
      consume();
      lock.lock();
      if (--pending_ == 0) done_.notify_one();
    }
  }

  void stop() {
    {
      std::lock_guard<std::mutex> lock(mutex_);
      stopping_ = true;
    }
    start_.notify_all();
    for (auto& worker : workers_) worker.join();
  }
};

struct UpdateCounts {
  size_t deleted, inserted, deletion_groups, insertion_groups;
};

// Edge-update restriction of Luo et al., IEEE TC 2023, DOI
// 10.1109/TC.2022.3174594. All input vertices exist from the initial graph.
class EdgeMaintainer {
 public:
  EdgeMaintainer(DynGraph& graph, uint32_t threads)
      : graph_(graph), labels_(static_truss(graph, threads).tau), pool_(threads) {
    stamp_.init(graph.n);
  }

  const std::vector<uint32_t>& labels() const { return labels_; }

  UpdateCounts apply(const std::vector<Op>& operations) {
    // The outer adapter gives ordered mixed batches a single exact checkpoint.
    const auto net = coalesce_batch_updates(graph_, operations);
    const auto deletes = group_deletions(graph_, net.deletes);
    for (const auto& group : deletes) delete_group(group);

    std::vector<std::pair<uint32_t, uint32_t>> insertions;
    insertions.reserve(net.inserts.size());
    for (uint64_t key : net.inserts)
      insertions.emplace_back(static_cast<uint32_t>(key >> 32), static_cast<uint32_t>(key));
    const auto inserts = group_insertions(graph_, insertions);
    for (const auto& group : inserts) insert_group(group);
    return {net.deletes.size(), net.inserts.size(), deletes.size(), inserts.size()};
  }

 private:
  using SeedsByLevel = std::map<uint32_t, std::vector<uint32_t>>;

  DynGraph& graph_;
  std::vector<uint32_t> labels_;
  LevelPool pool_;
  StampSet stamp_;
  std::vector<uint32_t> minima_;

  uint32_t pre_truss(uint32_t u, uint32_t v) {
    minima_.clear();
    graph_.common_neighbors(u, v, stamp_, [&](uint32_t, uint32_t left, uint32_t right) {
      minima_.push_back(std::min(labels_[left], labels_[right]));
    });
    std::sort(minima_.rbegin(), minima_.rend());
    uint32_t best = 2;
    for (size_t i = 0; i < minima_.size(); ++i) {
      // Equation (3) requires k - 2 supporting triangles at level k.
      best = std::max(best, std::min(minima_[i], static_cast<uint32_t>(i + 3)));
    }
    return best;
  }

  void seed_triangle_mates(uint32_t edge, bool inserting, SeedsByLevel& seeds) {
    const uint32_t level = labels_[edge];
    graph_.common_neighbors(graph_.eu[edge], graph_.ev[edge], stamp_,
                            [&](uint32_t, uint32_t left, uint32_t right) {
      const uint32_t k = std::min(labels_[left], labels_[right]);
      if (k > level || (!inserting && k == 2)) return;
      if (labels_[left] == k) seeds[k].push_back(left);
      if (labels_[right] == k) seeds[k].push_back(right);
    });
  }

  void traverse(const SeedsByLevel& seeds, bool inserting) {
    std::vector<SeedsByLevel::const_iterator> levels;
    levels.reserve(seeds.size());
    for (auto it = seeds.begin(); it != seeds.end(); ++it) levels.push_back(it);
    std::vector<std::vector<uint32_t>> changed(levels.size());
    pool_.run(levels.size(), [&](size_t i) {
      const auto& level = *levels[i];
      changed[i] = changed_at_level(graph_, labels_, level.first, level.second, inserting);
    });
    // Members of a level can move by only one. Reading all old levels
    // throughout traversal and committing at the barrier keeps levels independent.
    for (size_t i = 0; i < levels.size(); ++i) {
      const uint32_t new_level = inserting ? levels[i]->first + 1 : levels[i]->first - 1;
      for (uint32_t edge : changed[i]) labels_[edge] = new_level;
    }
  }

  void delete_group(const std::vector<uint32_t>& group) {
    SeedsByLevel seeds;
    for (uint32_t edge : group) seed_triangle_mates(edge, false, seeds);
    for (uint32_t edge : group) {
      graph_.remove_edge(edge);
      labels_[edge] = 0;
    }
    traverse(seeds, false);
  }

  void insert_group(const std::vector<std::pair<uint32_t, uint32_t>>& group) {
    std::vector<uint32_t> pre_labels;
    pre_labels.reserve(group.size());
    for (const auto& edge : group) pre_labels.push_back(pre_truss(edge.first, edge.second));

    std::vector<uint32_t> inserted;
    inserted.reserve(group.size());
    for (size_t i = 0; i < group.size(); ++i) {
      const uint32_t edge = graph_.add_edge(group[i].first, group[i].second);
      if (labels_.size() < graph_.cap_edges) labels_.resize(graph_.eu.size());
      labels_[edge] = pre_labels[i];
      inserted.push_back(edge);
    }

    SeedsByLevel seeds;
    for (uint32_t edge : inserted) {
      seeds[labels_[edge]].push_back(edge);
      seed_triangle_mates(edge, true, seeds);
    }
    traverse(seeds, true);
  }
};

}  // namespace luo
