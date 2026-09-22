#pragma once
#include <algorithm>
#include <atomic>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <thread>
#include <vector>

inline int64_t BT_WATCH = -1;

inline double now_s() {
  return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

inline uint64_t splitmix64(uint64_t x) {
  x += 0x9e3779b97f4a7c15ULL;
  x = (x ^ (x >> 30)) * 0xbf58476d1ce4e5b9ULL;
  x = (x ^ (x >> 27)) * 0x94d049bb133111ebULL;
  return x ^ (x >> 31);
}

struct Rng {
  uint64_t s;
  explicit Rng(uint64_t seed) : s(splitmix64(seed + 0x1234567)) {}
  inline uint64_t next() { s = splitmix64(s); return s; }
  inline uint32_t below(uint32_t bound) { return bound ? uint32_t(next() % bound) : 0; }
};

struct StampSet {
  std::vector<uint32_t> s;
  std::vector<uint32_t> mid;
  uint32_t e = 1;
  void init(size_t n) {
    s.assign(n, 0);
    mid.assign(n, 0);
    e = 1;
  }
  inline void next_epoch() {
    if (++e == 0) {
      std::fill(s.begin(), s.end(), 0u);
      std::fill(mid.begin(), mid.end(), 0u);
      e = 1;
    }
  }
  inline void mark(uint32_t v, uint32_t eid) {
    s[v] = e;
    mid[v] = eid;
  }
  inline bool test(uint32_t v) const { return s[v] == e; }
  inline uint32_t marked_eid(uint32_t v) const { return mid[v]; }
};

template <class F>
inline void parallel_for(uint64_t lo, uint64_t hi, uint32_t nthreads, F body) {
  if (nthreads <= 1 || hi - lo < (uint64_t)2 * nthreads) {
    for (uint64_t i = lo; i < hi; ++i) body(i);
    return;
  }
  std::vector<std::thread> ths;
  std::atomic<uint64_t> idx(lo);
  auto worker = [&]() {
    for (;;) {
      uint64_t i = idx.fetch_add(1);
      if (i >= hi) break;
      body(i);
    }
  };
  for (uint32_t t = 1; t < nthreads; ++t) ths.emplace_back(worker);
  worker();
  for (auto& t : ths) t.join();
}

inline uint64_t peak_rss_mb() {
  FILE* f = fopen("/proc/self/status", "r");
  if (!f) return 0;
  char line[256];
  uint64_t kb = 0;
  while (fgets(line, sizeof(line), f)) {
    if (strncmp(line, "VmHWM:", 6) == 0) {
      sscanf(line + 6, "%lu", &kb);
      break;
    }
  }
  fclose(f);
  return kb / 1024;
}

struct LatStats {
  double mean = 0, p50 = 0, p99 = 0, mx = 0;
  void compute(std::vector<double>& v) {
    if (v.empty()) return;
    std::sort(v.begin(), v.end());
    double sum = 0;
    for (double x : v) sum += x;
    mean = sum / v.size();
    p50 = v[v.size() / 2];
    p99 = v[std::min(v.size() - 1, (size_t)(v.size() * 99) / 100)];
    mx = v.back();
  }
};

struct Op {
  uint8_t del;
  uint32_t u, v;
};
