#pragma once
#include "common.h"
#include <unordered_map>

struct EdgeRef {
  uint32_t nbr;
  uint32_t eid;
};

class DynGraph {
public:
  uint32_t n = 0;
  uint64_t m = 0;
  uint32_t cap_edges = 0;
  std::vector<std::vector<EdgeRef>> adj;
  std::vector<uint32_t> eu, ev;
  std::vector<uint32_t> slot0, slot1;
  std::vector<uint32_t> free_ids;

  std::vector<uint64_t> hkey;
  std::vector<uint32_t> hval;
  uint64_t hmask = 0;
  uint64_t hused = 0;
  uint64_t hcount = 0;

  static inline uint64_t ekey(uint32_t u, uint32_t v) {
    return u < v ? ((uint64_t)u << 32) | v : ((uint64_t)v << 32) | u;
  }
  static inline uint64_t hash_key(uint64_t x) { return splitmix64(x); }

  void set_vertices(uint32_t nv) {
    n = nv;
    adj.assign(n, std::vector<EdgeRef>());
  }

  void reserve_edges(uint32_t cap) {
    if (cap <= cap_edges) return;
    cap_edges = cap;
    eu.resize(cap, UINT32_MAX);
    ev.resize(cap, UINT32_MAX);
    slot0.resize(cap, 0);
    slot1.resize(cap, 0);
  }

  void init_hash(uint64_t cap) {
    uint64_t sz = 16;
    while (sz < cap * 2) sz <<= 1;
    hkey.assign(sz, 0);
    hval.assign(sz, 0);
    hmask = sz - 1;
    hused = 0;
    hcount = 0;
  }

  void rehash(uint64_t sz) {
    std::vector<uint64_t> ok;
    std::vector<uint32_t> ov;
    for (uint64_t i = 0; i <= hmask; ++i) {
      if (hval[i] != 0 && hval[i] != UINT32_MAX) {
        ok.push_back(hkey[i]);
        ov.push_back(hval[i]);
      }
    }
    hkey.assign(sz, 0);
    hval.assign(sz, 0);
    hmask = sz - 1;
    hused = ok.size();
    hcount = ok.size();
    for (size_t i = 0; i < ok.size(); ++i) raw_insert(ok[i], ov[i]);
  }

  void raw_insert(uint64_t k, uint32_t v) {
    uint64_t i = hash_key(k) & hmask;
    while (hval[i] != 0 && hval[i] != UINT32_MAX) i = (i + 1) & hmask;
    hkey[i] = k;
    hval[i] = v;
  }

  void hash_insert(uint64_t k, uint32_t eid) {
    if ((hused + 1) * 2 > hmask + 1) rehash((hmask + 1) * 2);
    raw_insert(k, eid);
    hused++;
    hcount++;
  }

  inline int64_t find(uint64_t k) const {
    if (hmask == 0) return -1;
    uint64_t i = hash_key(k) & hmask;
    for (;;) {
      uint32_t v = hval[i];
      if (v == 0) return -1;
      if (v != UINT32_MAX && hkey[i] == k) return (int64_t)(v - 1);
      i = (i + 1) & hmask;
    }
  }

  void hash_delete(uint64_t k) {
    uint64_t i = hash_key(k) & hmask;
    for (;;) {
      uint32_t v = hval[i];
      if (v == 0) return;
      if (v != UINT32_MAX && hkey[i] == k) {
        hval[i] = UINT32_MAX;
        hcount--;
        return;
      }
      i = (i + 1) & hmask;
    }
  }

  inline bool has(uint32_t u, uint32_t v) const { return find(ekey(u, v)) >= 0; }
  inline uint32_t get_eid(uint32_t u, uint32_t v) const { return (uint32_t)find(ekey(u, v)); }

  uint32_t add_edge(uint32_t u, uint32_t v) {
    if (u > v) std::swap(u, v);
    uint32_t eid;
    if (!free_ids.empty()) {
      eid = free_ids.back();
      free_ids.pop_back();
    } else {
      eid = cap_edges++;
      if (cap_edges > eu.size()) {
        size_t ns = eu.size() * 2 + 1024;
        eu.resize(ns, UINT32_MAX);
        ev.resize(ns, UINT32_MAX);
        slot0.resize(ns, 0);
        slot1.resize(ns, 0);
      }
    }
    eu[eid] = u;
    ev[eid] = v;
    slot0[eid] = (uint32_t)adj[u].size();
    slot1[eid] = (uint32_t)adj[v].size();
    adj[u].push_back({v, eid});
    adj[v].push_back({u, eid});
    hash_insert(ekey(u, v), eid + 1);
    m++;
    return eid;
  }

  void remove_edge(uint32_t eid) {
    uint32_t u = eu[eid], v = ev[eid];
    uint32_t p0 = slot0[eid], p1 = slot1[eid];
    {
      auto& au = adj[u];
      EdgeRef l0 = au.back();
      au[p0] = l0;
      if (eu[l0.eid] == u)
        slot0[l0.eid] = p0;
      else
        slot1[l0.eid] = p0;
      au.pop_back();
    }
    {
      auto& av = adj[v];
      EdgeRef l1 = av.back();
      av[p1] = l1;
      if (eu[l1.eid] == v)
        slot0[l1.eid] = p1;
      else
        slot1[l1.eid] = p1;
      av.pop_back();
    }
    hash_delete(ekey(u, v));
    eu[eid] = UINT32_MAX;
    ev[eid] = UINT32_MAX;
    free_ids.push_back(eid);
    m--;
  }

  inline bool alive(uint32_t eid) const { return eu[eid] != UINT32_MAX; }

  template <class CB>
  inline uint32_t common_neighbors(uint32_t u, uint32_t v, StampSet& st, CB cb) const {
    st.next_epoch();
    for (auto& r : adj[u]) st.mark(r.nbr, r.eid);
    uint32_t cnt = 0;
    for (auto& r : adj[v])
      if (st.test(r.nbr)) {
        cb(r.nbr, st.marked_eid(r.nbr), r.eid);
        ++cnt;
      }
    return cnt;
  }

  static DynGraph load(const std::string& path, bool temporal = false) {
    DynGraph g;
    std::unordered_map<uint32_t, uint32_t> idmap;
    std::vector<std::pair<uint32_t, uint32_t>> edges;
    std::vector<std::vector<uint32_t>> trows;
    FILE* f = fopen(path.c_str(), "r");
    if (!f) { fprintf(stderr, "cannot open %s\n", path.c_str()); exit(1); }
    char line[1 << 12];
    while (fgets(line, sizeof(line), f)) {
      if (line[0] == '#' || line[0] == '%') continue;
      if (temporal) {
        uint64_t a, b, t;
        if (sscanf(line, "%lu %lu %lu", &a, &b, &t) != 3) continue;
        trows.push_back({(uint32_t)a, (uint32_t)b, (uint32_t)t});
      } else {
        uint64_t a, b;
        if (sscanf(line, "%lu %lu", &a, &b) != 2) continue;
        edges.push_back({(uint32_t)a, (uint32_t)b});
      }
    }
    fclose(f);
    std::vector<std::pair<uint32_t, uint32_t>> elist;
    if (temporal)
      for (auto& r : trows) elist.push_back({r[0], r[1]});
    else
      elist = edges;
    auto map_id = [&](uint32_t x) {
      auto it = idmap.find(x);
      if (it == idmap.end()) {
        uint32_t id = (uint32_t)idmap.size();
        idmap[x] = id;
        return id;
      }
      return it->second;
    };
    for (auto& e : elist) {
      map_id(e.first);
      map_id(e.second);
    }
    uint64_t cap = elist.size();
    g.set_vertices((uint32_t)idmap.size());
    g.reserve_edges((uint32_t)std::min<uint64_t>(cap + 16, UINT32_MAX - 1));
    g.init_hash(cap * 2 + 64);
    for (auto& e : elist) {
      uint32_t u = idmap[e.first], v = idmap[e.second];
      if (u == v) continue;
      if (g.has(u, v)) continue;
      g.add_edge(u, v);
    }
    return g;
  }

  DynGraph clone() const {
    DynGraph c;
    c.n = n;
    c.m = m;
    c.cap_edges = cap_edges;
    c.adj.resize(n);
    for (uint32_t v = 0; v < n; ++v) c.adj[v] = adj[v];
    c.eu = eu;
    c.ev = ev;
    c.slot0 = slot0;
    c.slot1 = slot1;
    c.free_ids = free_ids;
    c.hkey = hkey;
    c.hval = hval;
    c.hmask = hmask;
    c.hused = hused;
    c.hcount = hcount;
    return c;
  }
};
