#pragma once
#include "batch.h"
#include <array>

struct OnlineRoute {
  BatchRoute path;
  const char* reason;
  bool switched;
  uint64_t pre_edges;
  uint64_t raw_ops;
  uint64_t prior_fused_region_edges;
  uint64_t prior_fused_degree_sum;
};

// Keep a small history for each path. Only a path actually executed in this
// replay can add an observation. A periodic probe refreshes the other path.
class OnlineBatchRouter {
public:
  OnlineBatchRouter(bool use_work, uint32_t probe_interval)
      : use_work_(use_work), probe_interval_(probe_interval) {}

  OnlineRoute choose(uint64_t pre_edges, uint64_t raw_ops) const {
    BatchRoute path;
    const char* reason;
    if (batches_ < 2) {
      path = batches_ == 0 ? BatchRoute::Fused : BatchRoute::Static;
      reason = "warmup";
    } else {
      double fused = fused_.mean();
      double full = static_.mean();
      if (use_work_) {
        // Scale the last measured closure by raw batch size. The current
        // closure is unavailable before dispatch, so it is never read here.
        const double ratio = double(raw_ops) / std::max<uint64_t>(1, prior_fused_raw_ops_);
        const double region = std::min(double(pre_edges) + raw_ops,
                                       prior_fused_region_edges_ * ratio);
        const double degrees = region * prior_fused_degree_sum_ /
                               std::max<uint64_t>(1, prior_fused_region_edges_);
        fused *= fused_work(raw_ops, region, degrees);
        full *= static_work(pre_edges, raw_ops);
      }
      path = fused <= full ? BatchRoute::Fused : BatchRoute::Static;
      reason = "predicted";
      if (batches_ % probe_interval_ == 0) {
        path = path == BatchRoute::Fused ? BatchRoute::Static : BatchRoute::Fused;
        reason = "probe";
      }
    }
    return {path, reason, batches_ != 0 && path != previous_, pre_edges, raw_ops,
            prior_fused_region_edges_, prior_fused_degree_sum_};
  }

  void observe(const OnlineRoute& route, const BatchStats& stats, double path_s) {
    if (route.path == BatchRoute::Fused) {
      fused_.add(use_work_ ? path_s / fused_work(route.raw_ops, stats.region_max,
                                                stats.region_degree_sum) : path_s);
      prior_fused_raw_ops_ = route.raw_ops;
      prior_fused_region_edges_ = stats.region_max;
      prior_fused_degree_sum_ = stats.region_degree_sum;
    } else {
      static_.add(use_work_ ? path_s / static_work(route.pre_edges, route.raw_ops) : path_s);
    }
    ++batches_;
    previous_ = route.path;
  }

private:
  struct History {
    std::array<double, 3> samples{};
    size_t used = 0;
    size_t next = 0;

    void add(double value) {
      samples[next] = value;
      used = std::min(used + 1, samples.size());
      next = (next + 1) % samples.size();
    }

    double mean() const { return (samples[0] + samples[1] + samples[2]) / used; }
  };

  static double fused_work(double raw, double region, double degrees) {
    return std::max(1.0, raw + region + degrees);
  }

  static double static_work(double edges, double raw) {
    return std::max(1.0, edges + raw);
  }

  History fused_, static_;
  bool use_work_;
  uint32_t probe_interval_;
  size_t batches_ = 0;
  BatchRoute previous_ = BatchRoute::Fused;
  uint64_t prior_fused_raw_ops_ = 0;
  uint64_t prior_fused_region_edges_ = 0;
  uint64_t prior_fused_degree_sum_ = 0;
};
