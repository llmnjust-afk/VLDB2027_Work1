#pragma once
#include "fixpoint.h"
#include <atomic>
#include <mutex>
#include <condition_variable>
#include <functional>
#include <thread>
#include <unordered_set>
#include <cstdio>
#include <chrono>
#include <cstdlib>
inline uint64_t now_ns() { return (uint64_t)std::chrono::duration_cast<std::chrono::nanoseconds>(std::chrono::steady_clock::now().time_since_epoch()).count(); }
inline int64_t bt_probe() { static int64_t v = getenv("BT_PROBE") ? 1 : -1; return v; }

struct BatchStats {
  uint64_t evals = 0;
  uint64_t seeds = 0;
  uint64_t rounds = 0;
  uint64_t region_max = 0;
  uint64_t region_sum = 0;
  uint64_t deltas = 0;
  uint64_t del_deltas = 0;
  uint64_t ins_deltas = 0;
};

class BatchMaintainer {
public:
  DynGraph& g;
  std::vector<uint16_t> tau;
  std::unique_ptr<std::atomic<uint32_t>[]> sup;
  std::unique_ptr<std::atomic<uint32_t>[]> dcount;
  uint32_t supcap = 0;
  uint32_t T;
  bool ablate_allseeds = false;
  int64_t watch_eid = -1;
  BatchStats stats;

  std::vector<StampSet> stamps;
  std::vector<std::vector<uint16_t>> bufs;
  std::vector<uint8_t> in_next;
  std::vector<uint8_t> in_reg;
  std::vector<uint8_t> ub_in;
  std::unique_ptr<std::atomic<uint8_t>[]> dead_mark;
  std::vector<uint32_t> psup;
  std::vector<uint8_t> palive;
  std::vector<std::vector<uint32_t>> evt_t;
  std::vector<uint32_t> ubc;
  std::vector<uint32_t> frontier, next, reg_list;
  std::vector<std::vector<uint32_t>> chg_e, nxt_t, seeds_t, dl;
  std::vector<uint64_t> cnt_t;
  std::vector<std::vector<uint16_t>> chg_v;
  std::vector<std::vector<std::pair<uint32_t, uint32_t>>> mate_t;

  std::vector<std::thread> pool_ths;
  std::mutex pool_mx;
  std::condition_variable pool_cv;
  struct {
    alignas(64) std::atomic<uint64_t> seq{0};
    alignas(64) std::atomic<uint64_t> job{0};
    alignas(64) std::atomic<uint64_t> done{0};
    alignas(64) std::atomic<bool> stop{false};
    alignas(64) std::atomic<uint64_t> count{0};
  } P;
  std::atomic<uint64_t>& pool_seq = P.seq;
  std::atomic<uint64_t>& pool_job = P.job;
  std::atomic<uint64_t>& pool_done = P.done;
  std::atomic<bool>& pool_stop = P.stop;
  std::atomic<uint64_t>& pool_count = P.count;
  std::function<void(uint32_t, uint64_t)> pool_body = nullptr;

  void pool_worker(uint32_t tid) {
    uint64_t seen = 0;
    for (;;) {
      {
        std::unique_lock<std::mutex> lk(pool_mx);
        while (pool_seq.load(std::memory_order_acquire) == seen && !pool_stop.load())
          pool_cv.wait(lk);
      }
      if (pool_stop.load()) return;
      seen = pool_seq.load(std::memory_order_acquire);
      uint64_t n = pool_count.load(std::memory_order_acquire);
      for (;;) {
        uint64_t i0 = pool_job.fetch_add(64);
        if (i0 >= n) break;
        uint64_t iend = i0 + 64;
        if (iend > n) iend = n;
        for (uint64_t i = i0; i < iend; ++i) pool_body(tid, i);
      }
      pool_done.fetch_add(1, std::memory_order_release);
    }
  }

  void pool_start() {
    for (uint32_t t = 1; t < T; ++t)
      pool_ths.emplace_back([this, t] { pool_worker(t); });
  }

  void pool_shutdown() {
    {
      std::lock_guard<std::mutex> lk(pool_mx);
      pool_stop.store(true, std::memory_order_release);
      pool_cv.notify_all();
    }
    for (auto& t : pool_ths) if (t.joinable()) t.join();
    pool_ths.clear();
  }

  template <class F>
  void run_jobs(uint64_t count, F body) {
    if (T <= 1 || count < 2 || count < 32768) {
      for (uint64_t i = 0; i < count; ++i) body(0, i);
      return;
    }
    pool_body = body;
    {
      std::lock_guard<std::mutex> lk(pool_mx);
      pool_count.store(count, std::memory_order_release);
      pool_job.store(0, std::memory_order_release);
      pool_done.store(0, std::memory_order_relaxed);
      pool_seq.fetch_add(1, std::memory_order_release);
      pool_cv.notify_all();
    }
    for (;;) {
      uint64_t i0 = pool_job.fetch_add(64);
      if (i0 >= count) break;
      uint64_t iend = i0 + 64;
      if (iend > count) iend = count;
      for (uint64_t i = i0; i < iend; ++i) body(0, i);
    }
    while (pool_done.load(std::memory_order_acquire) != T - 1)
      std::this_thread::yield();
  }

  BatchMaintainer(DynGraph& graph, uint32_t threads, uint64_t reserve_edges = 0)
      : g(graph), T(threads ? threads : 1) {
    uint32_t cap = g.cap_edges;
    if (reserve_edges) {
      uint64_t want = (uint64_t)g.cap_edges + reserve_edges + 16;
      if (want > UINT32_MAX - 1) want = UINT32_MAX - 1;
      cap = (uint32_t)want;
      g.reserve_edges(cap);
    }
    StaticTruss s = static_truss(g, T, &stamps);
    tau.assign(cap, 0);
    for (uint32_t e = 0; e < g.cap_edges; ++e) tau[e] = s.tau[e];
    sup = std::make_unique<std::atomic<uint32_t>[]>(cap);
    dcount = std::make_unique<std::atomic<uint32_t>[]>(cap);
    supcap = cap;
    for (uint32_t e = 0; e < cap; ++e) {
      sup[e].store(e < g.cap_edges ? s.sup[e] : 0);
      dcount[e].store(0);
    }
    bufs.resize(T);
    chg_e.resize(T);
    chg_v.resize(T);
    nxt_t.resize(T);
    seeds_t.resize(T);
    mate_t.resize(T);
    dl.resize(T);
    in_next.assign(cap, 0);
    in_reg.assign(cap, 0);
    ub_in.assign(cap, 0);
    ubc.assign(cap, 0);
    dead_mark = std::make_unique<std::atomic<uint8_t>[]>(cap);
    for (uint32_t i = 0; i < cap; ++i) dead_mark[i].store(0, std::memory_order_relaxed);
    psup.assign(cap, 0);
    palive.assign(cap, 0);
    evt_t.resize(T);
    cnt_t.resize(T);
    pool_start();
  }

  ~BatchMaintainer() { pool_shutdown(); }

  inline void expand_local(uint32_t tid, uint32_t eid, std::vector<uint8_t>& flags,
                           std::vector<uint32_t>& out) {
    uint32_t a = g.eu[eid], b = g.ev[eid];
    for (auto& r : g.adj[a])
      if (r.eid != eid && !flags[r.eid]) {
        flags[r.eid] = 1;
        out.push_back(r.eid);
      }
    for (auto& r : g.adj[b])
      if (r.eid != eid && !flags[r.eid]) {
        flags[r.eid] = 1;
        out.push_back(r.eid);
      }
  }

  inline uint32_t eval_wmax_pot(uint32_t u, uint32_t v, StampSet& st) {
    st.next_epoch();
    for (auto& r : g.adj[u]) st.mark(r.nbr, r.eid);
    uint32_t best = 0;
    for (auto& r : g.adj[v])
      if (st.test(r.nbr)) {
        uint32_t e1 = st.marked_eid(r.nbr), e2 = r.eid;
        uint32_t p1 = in_reg[e1] ? (uint32_t)tau[e1] : sup[e1].load() + 2;
        uint32_t p2 = in_reg[e2] ? (uint32_t)tau[e2] : sup[e2].load() + 2;
        uint32_t m = p1 < p2 ? p1 : p2;
        if (m > best) best = m;
      }
    return best;
  }

  void region_closure(std::vector<uint32_t>& seeds, BatchStats& st) {
    reg_list.clear();
    for (uint32_t e : seeds) {
      if (!g.alive(e) || in_reg[e]) continue;
      in_reg[e] = 1;
      reg_list.push_back(e);
    }
    if (reg_list.size() > st.region_max) st.region_max = reg_list.size();
    seeds.clear();
    for (uint32_t e : reg_list) seeds.push_back(e);
    while (!seeds.empty()) {
      st.evals += seeds.size();
      for (uint32_t t = 0; t < T; ++t) nxt_t[t].clear();
      run_jobs(seeds.size(), [&](uint32_t tid, uint64_t i) {
        uint32_t eid = seeds[i];
        uint32_t a = g.eu[eid], b = g.ev[eid];
        g.common_neighbors(a, b, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
          if (!ub_in[e1]) {
            ub_in[e1] = 1;
            nxt_t[tid].push_back(e1);
          }
          if (!ub_in[e2]) {
            ub_in[e2] = 1;
            nxt_t[tid].push_back(e2);
          }
        });
      });
      seeds.clear();
      for (uint32_t t = 0; t < T; ++t)
        for (uint32_t e : nxt_t[t]) {
          if (in_reg[e]) continue;
          in_reg[e] = 1;
          reg_list.push_back(e);
          seeds.push_back(e);
        }
    }
    for (uint32_t e : reg_list) ub_in[e] = 0;
  }

  void region_peel(BatchStats& st) {
    if (BT_WATCH >= 0) fprintf(stderr, "[peel-in] n=%zu\n", reg_list.size());
    if (BT_WATCH >= 0) fprintf(stderr, "[peel-call]\n");
    if (BT_WATCH >= 0) {
      std::vector<uint32_t> tmp(reg_list);
      std::sort(tmp.begin(), tmp.end());
      uint32_t dup = 0;
      for (size_t i = 1; i < tmp.size(); ++i) if (tmp[i] == tmp[i-1]) dup++;
      fprintf(stderr, "[peel-in] region=%zu dup=%zu\n", reg_list.size(), (size_t)dup);
    }
    for (uint32_t t = 0; t < T; ++t) evt_t[t].clear();
    uint64_t p0 = bt_probe() > 0 ? now_ns() : 0;
    run_jobs(reg_list.size(), [&](uint32_t tid, uint64_t i) {
      uint32_t eid = reg_list[i];
      uint32_t a = g.eu[eid], b = g.ev[eid];
      cnt_t[tid]++;
      uint32_t c = 0;
      g.common_neighbors(a, b, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        ++c;
        if (!in_reg[e1] && !dead_mark[e1].load(std::memory_order_relaxed)) {
          uint8_t exp = 0;
          if (dead_mark[e1].compare_exchange_strong(exp, 1, std::memory_order_relaxed))
            evt_t[tid].push_back(e1);
        }
        if (!in_reg[e2] && !dead_mark[e2].load(std::memory_order_relaxed)) {
          uint8_t exp = 0;
          if (dead_mark[e2].compare_exchange_strong(exp, 1, std::memory_order_relaxed))
            evt_t[tid].push_back(e2);
        }
      });
      psup[eid] = c;
    });
    uint64_t p1 = bt_probe() > 0 ? now_ns() : 0;
    for (uint32_t t = 0; t < T; ++t) {
      st.evals += cnt_t[t];
      cnt_t[t] = 0;
    }
    run_jobs(reg_list.size(), [&](uint32_t, uint64_t i) {
      uint32_t e = reg_list[i];
      sup[e].store(psup[e]);
      if (e == 3724 && BT_WATCH >= 0)
        fprintf(stderr, "[init3724] psup=%u tau=%u\n", psup[e], tau[e]);
    });
    uint64_t p2 = bt_probe() > 0 ? now_ns() : 0;
    std::vector<std::pair<uint32_t, uint32_t>> evs;
    for (uint32_t t = 0; t < T; ++t)
      for (uint32_t e : evt_t[t]) evs.push_back({tau[e] >= 2 ? tau[e] - 2 : 0, e});
    evs.shrink_to_fit();
    std::sort(evs.begin(), evs.end());
    size_t evw = 0;
    for (size_t i = 0; i < evs.size(); ++i) {
      if (i > 0 && evs[i].second == evs[evw - 1].second) continue;
      evs[evw++] = evs[i];
    }
    evs.resize(evw);
    for (auto& ev : evs) ub_in[ev.second] = 1;
    for (uint32_t e : reg_list) palive[e] = 1;
    for (auto& ev : evs) palive[ev.second] = 1;
    uint32_t maxsup = 0;
    for (uint32_t e : reg_list)
      if (psup[e] > maxsup) maxsup = psup[e];
    uint64_t p3 = bt_probe() > 0 ? now_ns() : 0;
    std::vector<std::vector<uint32_t>> buckets(maxsup + 1);
    for (uint32_t e : reg_list) buckets[psup[e]].push_back(e);
    uint32_t popped = 0;
    size_t evptr = 0;
    StampSet& st0 = stamps[0];
    for (uint32_t s = 0; s <= maxsup; ++s) {
      while (evptr < evs.size() && evs[evptr].first == s) {
        uint32_t X = evs[evptr++].second;
        if (BT_WATCH >= 0) fprintf(stderr, "[vev] s=%u eid=%u\n", s, X);
        palive[X] = 0;
        st.evals++;
        uint32_t a = g.eu[X], b = g.ev[X];
        st0.next_epoch();
        for (auto& r : g.adj[a]) st0.mark(r.nbr, r.eid);
        for (auto& r : g.adj[b]) {
          if (!st0.test(r.nbr)) continue;
          uint32_t e1 = st0.marked_eid(r.nbr), e2 = r.eid;
          bool d1 = in_reg[e1] ? !palive[e1] : (ub_in[e1] ? !palive[e1] : (tau[e1] < (uint16_t)(s + 2)));
          bool d2 = in_reg[e2] ? !palive[e2] : (ub_in[e2] ? !palive[e2] : (tau[e2] < (uint16_t)(s + 2)));
          if (d1 || d2) continue;
          uint32_t mm[2] = {e1, e2};
          for (uint32_t mi = 0; mi < 2; ++mi) {
            uint32_t m = mm[mi];
            if (!in_reg[m] || psup[m] <= s) continue;
            psup[m]--;
            if (m == 3724 && BT_WATCH >= 0)
              fprintf(stderr, "[vdec3724] s=%u src=%u psup->%u\n", s, X, psup[m]);
            buckets[psup[m]].push_back(m);
          }
        }
      }
      auto& bkt = buckets[s];
      for (size_t i = 0; i < bkt.size(); ++i) {
        uint32_t eid = bkt[i];
        if (!palive[eid] || psup[eid] != s) continue;
        tau[eid] = (uint16_t)(s + 2);
        palive[eid] = 0;
        popped++;
        st.evals++;
        if (BT_WATCH >= 0) fprintf(stderr, "[rpop] s=%u eid=%u psup=%u\n", s, eid, psup[eid]);
        uint32_t a = g.eu[eid], b = g.ev[eid];
        st0.next_epoch();
        for (auto& r : g.adj[a]) st0.mark(r.nbr, r.eid);
        for (auto& r : g.adj[b]) {
          if (!st0.test(r.nbr)) continue;
          uint32_t e1 = st0.marked_eid(r.nbr), e2 = r.eid;
          bool d1 = in_reg[e1] ? !palive[e1] : (ub_in[e1] ? !palive[e1] : (tau[e1] < (uint16_t)(s + 2)));
          bool d2 = in_reg[e2] ? !palive[e2] : (ub_in[e2] ? !palive[e2] : (tau[e2] < (uint16_t)(s + 2)));
          if (d1 || d2) continue;
          uint32_t mm[2] = {e1, e2};
          for (uint32_t mi = 0; mi < 2; ++mi) {
            uint32_t m = mm[mi];
            if (!in_reg[m] || psup[m] <= s) continue;
            psup[m]--;
            if (m == 3724 && BT_WATCH >= 0)
              fprintf(stderr, "[pdec3724] s=%u src=%u psup->%u\n", s, eid, psup[m]);
            buckets[psup[m]].push_back(m);
          }
        }
      }
    }
    if (popped != reg_list.size()) {
      fprintf(stderr, "region peel incomplete: popped=%u region=%zu\n", popped, reg_list.size());
      exit(1);
    }
    if (BT_WATCH >= 0) fprintf(stderr, "[peel-out] popped=%u\n", popped);
    uint64_t p4 = bt_probe() > 0 ? now_ns() : 0;
    if (bt_probe() > 0)
      fprintf(stderr, "[probe] rec=%lu sup=%lu evs=%lu sweep=%lu total=%lu nreg=%zu T=%u\n",
              (unsigned long)(p1-p0), (unsigned long)(p2-p1), (unsigned long)(p3-p2),
              (unsigned long)(p4-p3), (unsigned long)(p4-p0), reg_list.size(), T);
    for (uint32_t e : reg_list) palive[e] = 0;
    for (auto& ev : evs) {
      palive[ev.second] = 0;
      ub_in[ev.second] = 0;
      dead_mark[ev.second].store(0, std::memory_order_relaxed);
    }
  }

  bool apply_batch(const std::vector<Op>& raw) {
    std::unordered_set<uint64_t> delset, insset;
    for (auto& op : raw) {
      if (op.u == op.v) continue;
      uint64_t k = DynGraph::ekey(op.u, op.v);
      if (op.del) {
        if (g.has(op.u, op.v)) delset.insert(k);
      } else {
        if (!g.has(op.u, op.v)) insset.insert(k);
      }
    }
    std::vector<uint32_t> dels;
    for (uint64_t k : delset) {
      if (insset.count(k)) continue;
      int64_t eid = g.find(k);
      if (eid >= 0) dels.push_back((uint32_t)eid);
    }
    std::vector<std::pair<uint32_t, uint32_t>> ins;
    for (uint64_t k : insset) {
      if (delset.count(k)) continue;
      ins.push_back({(uint32_t)(k >> 32), (uint32_t)(k & 0xffffffffULL)});
    }
    stats.deltas += dels.size() + ins.size();
    stats.del_deltas += dels.size();
    stats.ins_deltas += ins.size();
    if (!dels.empty()) { if (BT_WATCH >= 0) fprintf(stderr, "[pd-in] %zu\n", dels.size()); phase_deletions(dels); if (BT_WATCH >= 0) fprintf(stderr, "[pd-out]\n"); }
    if (!ins.empty()) { if (BT_WATCH >= 0) fprintf(stderr, "[pi-in] %zu\n", ins.size()); phase_insertions(ins); if (BT_WATCH >= 0) fprintf(stderr, "[pi-out]\n"); }
    return true;
  }

  bool apply_batch_nomerge(const std::vector<Op>& raw) {
    bool ok = true;
    for (auto& op : raw) {
      std::vector<Op> one = {op};
      ok = apply_batch(one) && ok;
    }
    return ok;
  }

  void phase_deletions(std::vector<uint32_t>& dels) {
    for (uint32_t t = 0; t < T; ++t) seeds_t[t].clear();
    for (uint32_t e : dels) dead_mark[e].store(1, std::memory_order_relaxed);
    uint64_t w0 = bt_probe() > 0 ? now_ns() : 0;
    run_jobs(dels.size(), [&](uint32_t tid, uint64_t i) {
      uint32_t e = dels[i];
      if (!g.alive(e)) return;
      uint32_t u = g.eu[e], v = g.ev[e];
      g.common_neighbors(u, v, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        if ((dead_mark[e1].load(std::memory_order_relaxed) && e1 < e) || (dead_mark[e2].load(std::memory_order_relaxed) && e2 < e)) return;
        uint32_t o1 = sup[e1].fetch_sub(1);
        uint32_t o2 = sup[e2].fetch_sub(1);
        if (o1 == 0 || o2 == 0) {
          fprintf(stderr, "support underflow\n");
          exit(1);
        }
        seeds_t[tid].push_back(e1);
        seeds_t[tid].push_back(e2);
      });
      if (ablate_allseeds) {
        for (auto& r : g.adj[u]) seeds_t[tid].push_back(r.eid);
        for (auto& r : g.adj[v]) seeds_t[tid].push_back(r.eid);
      }
    });
    if (BT_WATCH >= 0) fprintf(stderr, "[pd-enum-done]\n");
    uint64_t w1 = bt_probe() > 0 ? now_ns() : 0;
    for (uint32_t e : dels) {
      if (!g.alive(e)) continue;
      g.remove_edge(e);
      tau[e] = 0;
      sup[e].store(0);
      dead_mark[e].store(0, std::memory_order_relaxed);
    }
    std::vector<uint32_t> seeds;
    for (uint32_t t = 0; t < T; ++t)
      for (uint32_t e : seeds_t[t]) seeds.push_back(e);
    reg_list.clear();
    region_closure(seeds, stats);
    uint64_t w2 = bt_probe() > 0 ? now_ns() : 0;
    region_peel(stats);
    uint64_t w3 = bt_probe() > 0 ? now_ns() : 0;
    if (bt_probe() > 0)
      fprintf(stderr, "[probeD] wit=%lu clos=%lu peel=%lu ndels=%zu\n",
              (unsigned long)(w1-w0), (unsigned long)(w2-w1), (unsigned long)(w3-w2), dels.size());
    for (uint32_t e : reg_list) in_reg[e] = 0;
    reg_list.clear();
  }

  void phase_insertions(std::vector<std::pair<uint32_t, uint32_t>>& ins) {
    uint64_t B = ins.size();
    std::vector<uint32_t> neweids(B);
    for (uint64_t i = 0; i < B; ++i) {
      uint32_t u = ins[i].first, v = ins[i].second;
      if (u > v) std::swap(u, v);
      uint32_t eid;
      if (!g.free_ids.empty()) {
        eid = g.free_ids.back();
        g.free_ids.pop_back();
      } else {
        eid = g.cap_edges++;
        if (eid >= supcap || eid >= g.eu.size()) grow_structures(eid);
      }
      g.eu[eid] = u;
      g.ev[eid] = v;
      g.slot0[eid] = (uint32_t)g.adj[u].size();
      g.slot1[eid] = (uint32_t)g.adj[v].size();
      g.adj[u].push_back({v, eid});
      g.adj[v].push_back({u, eid});
      g.hash_insert(DynGraph::ekey(u, v), eid + 1);
      g.m++;
      tau[eid] = 0;
      sup[eid].store(0);
      in_next[eid] = 0;
      in_reg[eid] = 0;
      ub_in[eid] = 0;
      neweids[i] = eid;
    }
    std::vector<uint32_t> wsize(B, 0);
    for (uint32_t t = 0; t < T; ++t) {
      mate_t[t].clear();
      dl[t].clear();
    }
    uint64_t wi0 = bt_probe() > 0 ? now_ns() : 0;
    run_jobs(B, [&](uint32_t tid, uint64_t i) {
      uint32_t eid = neweids[i];
      uint32_t u = g.eu[eid], v = g.ev[eid];
      uint32_t cnt = g.common_neighbors(u, v, stamps[tid], [&](uint32_t, uint32_t e1, uint32_t e2) {
        mate_t[tid].push_back({e1, e2});
      });
      wsize[i] = cnt;
    });
    run_jobs(B, [&](uint32_t, uint64_t i) { sup[neweids[i]].store(wsize[i]); });
    std::vector<std::pair<uint32_t, uint32_t>> allmates;
    for (uint32_t t = 0; t < T; ++t)
      for (auto& p : mate_t[t]) allmates.push_back(p);
    uint64_t M = allmates.size();
    run_jobs(M, [&](uint32_t, uint64_t i) {
      dcount[allmates[i].first].store(0);
      dcount[allmates[i].second].store(0);
    });
    run_jobs(M, [&](uint32_t, uint64_t i) {
      sup[allmates[i].first].fetch_add(1);
      sup[allmates[i].second].fetch_add(1);
      dcount[allmates[i].first].fetch_add(1);
      dcount[allmates[i].second].fetch_add(1);
    });
    reg_list.clear();
    std::vector<uint32_t> seeds;
    for (uint64_t i = 0; i < B; ++i) seeds.push_back(neweids[i]);
    uint64_t wi1 = bt_probe() > 0 ? now_ns() : 0;
    run_jobs(M, [&](uint32_t tid, uint64_t i) {
      uint32_t f = allmates[i].first;
      if (dcount[f].load() > 0) dl[tid].push_back(f);
      f = allmates[i].second;
      if (dcount[f].load() > 0) dl[tid].push_back(f);
    });
    for (uint32_t t = 0; t < T; ++t)
      for (uint32_t f : dl[t]) seeds.push_back(f);
    uint64_t wi2 = bt_probe() > 0 ? now_ns() : 0;
    region_closure(seeds, stats);
    region_peel(stats);
    uint64_t wi3 = bt_probe() > 0 ? now_ns() : 0;
    if (bt_probe() > 0)
      fprintf(stderr, "[probeI] clos=%lu peel=%lu wit=%lu nins=%zu\n",
              (unsigned long)(wi2-wi1), (unsigned long)(wi3-wi2), (unsigned long)(wi1-wi0), ins.size());
    for (uint32_t e : reg_list) in_reg[e] = 0;
    reg_list.clear();
  }  void grow_structures(uint32_t eid) {
    size_t nsz = (size_t)std::max<uint64_t>(eid + 1, (g.eu.size() ? g.eu.size() * 2 : 1024));
    g.eu.resize(nsz, UINT32_MAX);
    g.ev.resize(nsz, UINT32_MAX);
    g.slot0.resize(nsz, 0);
    g.slot1.resize(nsz, 0);
    in_next.resize(nsz, 0);
    in_reg.resize(nsz, 0);
    ub_in.resize(nsz, 0);
    ubc.resize(nsz, 0);
    dead_mark = std::make_unique<std::atomic<uint8_t>[]>(nsz);
    for (uint32_t i = 0; i < nsz; ++i) dead_mark[i].store(0, std::memory_order_relaxed);
    psup.resize(nsz, 0);
    palive.resize(nsz, 0);
    tau.resize(nsz, 0);
    auto nsup = std::make_unique<std::atomic<uint32_t>[]>(nsz);
    auto ndc = std::make_unique<std::atomic<uint32_t>[]>(nsz);
    for (size_t e = 0; e < nsz; ++e) {
      if (e < supcap) {
        nsup[e].store(sup[e].load());
        ndc[e].store(dcount[e].load());
      } else {
        nsup[e].store(0);
        ndc[e].store(0);
      }
    }
    sup = std::move(nsup);
    dcount = std::move(ndc);
    supcap = (uint32_t)nsz;
  }
};
