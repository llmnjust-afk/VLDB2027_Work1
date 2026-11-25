"""Summarize audited replays and compare online routes with paired fixed runs."""

from math import ceil, isfinite
from statistics import mean, stdev


ROUTE_METHOD = {"static": "static", "fused": "full-fused", "fused-pruned": "full-fused-pruned"}
FIXED = tuple(ROUTE_METHOD.values())
ROUTE_FEATURES = (
    "pre_edges", "raw_ops", "prior_fused_region_edges", "prior_fused_degree_sum",
    "region_edges", "region_degree_sum",
)


def percentile(values, quantile):
    return sorted(values)[ceil(quantile * len(values)) - 1]


def optional_float(value):
    return float(value) if value not in (None, "") else ""


def relative_percent(observed, reference):
    return 100 * (observed / reference - 1) if reference else ""


def route_observations(common, method, repetition, elapsed, routes, fixed, regional_method):
    rows = []
    previous = None
    for batch, (latency, route_row) in enumerate(zip(elapsed, routes, strict=True), start=1):
        route = route_row["route"]
        path = ROUTE_METHOD[route]
        alternative = regional_method if path == "static" else "static"
        switched = previous is not None and route != previous
        reason = route_row.get("reason")
        if reason is None:
            if previous is None:
                reason = "warmup"
            elif switched and route == "fused":
                reason = "probe"
            else:
                reason = "coverage"
        chosen_ms = fixed[path][batch - 1]
        alternative_ms = fixed[alternative][batch - 1]
        decision_us = optional_float(route_row.get("decision_us"))
        path_us = optional_float(route_row.get("path_us"))
        rows.append(dict(
            common, method=method, repetition=repetition, batch=batch,
            route=route, previous_route=previous or "", reason=reason,
            switched=switched, probe=reason == "probe", elapsed_ms=latency,
            chosen_fixed_ms=chosen_ms, alternative_fixed_ms=alternative_ms,
            excess_vs_chosen_fixed_ms=latency - chosen_ms,
            excess_vs_alternative_fixed_ms=latency - alternative_ms,
            excess_vs_paired_route_oracle_ms=latency - min(chosen_ms, alternative_ms),
            decision_us=decision_us, path_us=path_us,
            other_measured_us=(latency * 1000 - decision_us - path_us
                               if decision_us != "" and path_us != "" else ""),
            **{name: route_row.get(name, "") for name in ROUTE_FEATURES},
        ))
        previous = route
    return rows


def route_metrics(rows, pruned_mode=False):
    switched = [row for row in rows if row["switched"]]
    probes = [row for row in rows if row["probe"]]
    switch_deltas = [row["excess_vs_chosen_fixed_ms"] for row in switched]
    probe_deltas = [row["excess_vs_alternative_fixed_ms"] for row in probes]
    decisions = [row["decision_us"] for row in rows if row["decision_us"] != ""]
    other_times = [row["other_measured_us"] for row in rows if row["other_measured_us"] != ""]
    metrics = dict(
        static_batches=sum(row["route"] == "static" for row in rows),
        fused_batches=sum(row["route"] == "fused" for row in rows),
        switches=len(switched), probes=len(probes),
        decision_us_per_batch=mean(decisions) if decisions else "",
        decision_time_ms=sum(decisions) / 1000 if decisions else "",
        other_measured_us_per_batch=mean(other_times) if other_times else "",
        switch_excess_same_path_ms=sum(switch_deltas),
        switch_positive_excess_same_path_ms=sum(max(0, value) for value in switch_deltas),
        probe_excess_alternative_path_ms=sum(probe_deltas),
        probe_positive_excess_alternative_path_ms=sum(max(0, value) for value in probe_deltas),
    )
    if pruned_mode:
        metrics["fused_pruned_batches"] = sum(row["route"] == "fused-pruned" for row in rows)
    return metrics


def summarize_case(case, observations, regional_by_method=None):
    """Reduce equal-length, verified batches; pair fixed replays by trial and batch."""
    batch_count = len(case["batch_operations"])
    runs = len(observations[FIXED[0]])
    if not (runs and batch_count) or any(len(replays) != runs for replays in observations.values()):
        raise ValueError("each method needs the same positive replay count on a nonempty workload")
    elapsed = {method: [[float(row["elapsed_us"]) / 1000 for row in replay["batches"]]
                        for replay in replays] for method, replays in observations.items()}
    if any(len(trial) != batch_count or not all(isfinite(ms) and ms >= 0 for ms in trial)
           for replays in elapsed.values() for trial in replays):
        raise ValueError("incomplete or invalid batch latencies")

    totals = {method: [sum(trial) for trial in elapsed[method]] for method in observations}
    best_deployment = min(FIXED, key=lambda method: mean(totals[method]))
    common = dict(dataset=case["dataset"], scope=case.get("scope", ""),
                  period=case.get("role", case.get("period", "")),
                  start_utc=case.get("start_utc", ""), end_utc=case.get("end_utc", ""),
                  checkpoint_seconds=case.get("checkpoint_seconds", ""),
                  window_days=case.get("window_days", ""),
                  probe_interval=case.get("probe_interval", ""))

    def scores(observed, best_method, path, deployment, oracle, deployment_oracle):
        return dict(best_route_fixed_method=best_method, best_route_fixed_total_ms=path,
                    paired_route_oracle_total_ms=oracle,
                    best_deployment_fixed_method=best_deployment,
                    best_deployment_fixed_total_ms=deployment,
                    paired_deployment_oracle_total_ms=deployment_oracle,
                    regret_best_route_fixed_pct=relative_percent(observed, path),
                    regret_paired_route_oracle_pct=relative_percent(observed, oracle),
                    regret_best_deployment_fixed_pct=relative_percent(observed, deployment),
                    regret_paired_deployment_oracle_pct=relative_percent(observed, deployment_oracle))

    summaries, replay_rows, route_rows = [], [], []
    for method, replays in observations.items():
        regional_method = (regional_by_method or {}).get(method, "full-fused")
        fixed_paths = ("static", regional_method)
        best_path = min(fixed_paths, key=lambda path: mean(totals[path]))
        method_common = common
        if regional_by_method:
            method_common = common | {"routing_regional_method": regional_method}
        replay_details = []
        for index, record in enumerate(replays):
            samples = elapsed[method][index]
            total = totals[method][index]
            fixed = {name: elapsed[name][index] for name in FIXED}
            path_oracle = sum(map(min, zip(*(fixed[name] for name in fixed_paths), strict=True)))
            deployment_oracle = sum(map(min, zip(*fixed.values(), strict=True)))
            routes = route_observations(method_common, method, index, samples, record["routes"],
                                        fixed, regional_method) if record["routes"] else []
            route_rows.extend(routes)
            build = float(record["run"]["build_s"])
            rss = int(record["run"]["rss_mb"])
            if not isfinite(build) or build < 0 or rss <= 0:
                raise ValueError(f"invalid build time or RSS for {method}, replay {index}")
            replay_details.append(dict(
                method_common, method=method, repetition=index,
                mean_ms_per_batch=mean(samples), p95_ms=percentile(samples, .95),
                p99_ms=percentile(samples, .99), maintenance_ms=total,
                build_s=build, build_plus_maintenance_s=build + total / 1000,
                peak_process_rss_mib=rss,
                **scores(total, best_path, totals[best_path][index], totals[best_deployment][index],
                         path_oracle, deployment_oracle),
                **{key: value if routes else "" for key, value in
                   route_metrics(routes, bool(regional_by_method)).items()},
            ))
        replay_rows.extend(replay_details)
        means = [row["mean_ms_per_batch"] for row in replay_details]
        pooled = [ms for trial in elapsed[method] for ms in trial]

        def average(key):
            values = [row[key] for row in replay_details if row[key] != ""]
            return mean(values) if values else ""

        maintenance_ms = average("maintenance_ms")
        summaries.append(dict(
            method_common, method=method, threads=1, batches=batch_count, verified_batches=batch_count,
            operations=sum(case["batch_operations"]), timing_replays=runs, status="verified",
            mean_ms_per_batch=mean(means), replay_mean_sd_ms=stdev(means) if runs > 1 else "",
            pooled_p95_ms=percentile(pooled, .95), pooled_p99_ms=percentile(pooled, .99),
            mean_maintenance_s=maintenance_ms / 1000,
            mean_build_s=average("build_s"),
            mean_build_plus_maintenance_s=average("build_plus_maintenance_s"),
            peak_process_rss_mib=max(row["peak_process_rss_mib"] for row in replay_details),
            **scores(maintenance_ms, best_path, average("best_route_fixed_total_ms"),
                     average("best_deployment_fixed_total_ms"), average("paired_route_oracle_total_ms"),
                     average("paired_deployment_oracle_total_ms")),
            **{f"mean_{key}": average(key) for key in route_metrics([], bool(regional_by_method))},
        ))
    return summaries, replay_rows, route_rows
