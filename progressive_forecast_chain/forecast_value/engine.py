"""概率预报价值分析核心引擎。

设计原则（对应业务口径）：

* 配对只使用用户在指定决策提前量真正能收到的版本（按 ``available_at`` 判定），
  迟到产品不会被事后最优版本替换，而是显式记录排除原因。
* 只有目标区间与实况区间完全一致的产品才能与该实况值配对；不同累积时段不比较。
* 概率产品以 Brier 分数 / Brier 技巧评分为主，ROC AUC 作区分度辅助，
  pinball 损失评估分位数，校准以可靠性分组与分位数覆盖率诊断。
* 历史基线（气候概率）仅由 ``training_end`` 之前的目标估计，评估期冻结，
  避免用评估期信息反算基线。
* 同一过程（event_id）下多时效、相邻区间高度相关，不确定性使用按 event_id
  聚类的 cluster bootstrap；样本不足时只报不可排名，不强行排序。
* 实况保留两种口径：最初实况（first_available）与截止 as_of 的最终实况（final）。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from progressive_forecast_chain.vintages.runtime import stable_json

# ---- 样本支持阈值：低于阈值只描述、不排名 ----
MIN_CLUSTERS = 4          # 排名所需的独立过程（event_id）聚类数
MIN_PAIRED_SAMPLES = 8    # 排名所需的共同样本数
MIN_BIN_SAMPLES = 3       # 可靠性分组可解释所需的最小样本
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_SEED = 20261030
CONFIDENCE_LEVEL = 0.90
ROUND_DIGITS = 6

# study.truth_modes 口径名 -> 目标记录内部键
MODE_KEY = {"first_available": "first", "final": "final"}


def r6(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), ROUND_DIGITS)


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def hours_between(later: datetime, earlier: datetime) -> float:
    return (later - earlier).total_seconds() / 3600.0


# --------------------------------------------------------------------------- #
# 数据装载与实况口径
# --------------------------------------------------------------------------- #

def load_input(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def truth_windows(truth_rows: Sequence[dict[str, Any]], as_of: datetime) -> dict[str, list[dict[str, Any]]]:
    """按目标区间归集实况版本，只保留 as_of 之前已经可用的事实。"""
    windows: dict[str, list[dict[str, Any]]] = {}
    for row in truth_rows:
        available = parse_ts(row["available_at"])
        if available > as_of:
            # 晚于分析截止时刻的事实不能采用
            continue
        key = (row["target_start"], row["target_end"])
        windows.setdefault(key, []).append(dict(row))
    for rows in windows.values():
        rows.sort(key=lambda item: (item["revision"], item["available_at"], item["truth_id"]))
    return windows


def build_targets(
    truth_rows: Sequence[dict[str, Any]],
    as_of: datetime,
) -> list[dict[str, Any]]:
    """构造两种实况口径下的目标事件序列。"""
    windows = truth_windows(truth_rows, as_of)
    targets: list[dict[str, Any]] = []
    for (start, end), versions in sorted(windows.items()):
        first = versions[0]
        final = versions[-1]
        targets.append(
            {
                "target_key": f"{start}|{end}",
                "target_start": start,
                "target_end": end,
                "event_id": final.get("event_id") or first.get("event_id"),
                "first": {
                    "truth_id": first["truth_id"],
                    "amount": float(first["amount"]),
                    "revision": int(first["revision"]),
                    "available_at": first["available_at"],
                },
                "final": {
                    "truth_id": final["truth_id"],
                    "amount": float(final["amount"]),
                    "revision": int(final["revision"]),
                    "available_at": final["available_at"],
                    "supersedes": final.get("supersedes"),
                    "revised": final["truth_id"] != first["truth_id"],
                },
            }
        )
    return targets


def event_outcome(amount: float, threshold: float) -> int:
    return 1 if amount > threshold else 0


# --------------------------------------------------------------------------- #
# 版本配对：按实际收到时点
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Match:
    product_id: str
    model: str
    target_key: str
    event_id: str | None
    decision_lead_hours: int
    nominal_horizon_hours: int
    effective_lead_hours: float
    issued_at: str
    available_at: str
    probability: float
    threshold: float
    quantiles: dict[str, float]
    revision_of: str | None
    eligible: bool
    exclusion_reason: str | None


def _same_window(product: dict[str, Any], target: dict[str, Any]) -> bool:
    return (
        product["target_start"] == target["target_start"]
        and product["target_end"] == target["target_end"]
    )


def match_products(
    products: Sequence[dict[str, Any]],
    targets: Sequence[dict[str, Any]],
    leads_hours: Sequence[int],
    as_of: datetime,
) -> tuple[list[Match], list[dict[str, Any]]]:
    """把产品与目标、决策提前量配对。

    排除规则：
      * ``no_common_coverage_window``：产品覆盖区间与任何实况区间不一致，
        不同累积时段不能与同一个实况值比较；
      * ``not_available_at_decision_lead``：在该决策提前量对应时刻用户尚未收到。
    """
    target_by_key = {item["target_key"]: item for item in targets}
    matches: list[Match] = []
    exclusions: list[dict[str, Any]] = []

    for product in sorted(products, key=lambda item: item["product_id"]):
        target_key = f"{product['target_start']}|{product['target_end']}"
        target = target_by_key.get(target_key)
        available = parse_ts(product["available_at"])
        target_start = parse_ts(product["target_start"])
        effective_lead = hours_between(target_start, available)

        if target is None:
            exclusions.append(
                {
                    "product_id": product["product_id"],
                    "model": product["model"],
                    "target_start": product["target_start"],
                    "target_end": product["target_end"],
                    "reason": "no_common_coverage_window",
                    "detail": "产品累积时段与任何实况区间不完全重合，无法与同一实况值配对",
                    "decision_lead_hours": None,
                }
            )
            continue

        if available > as_of:
            exclusions.append(
                {
                    "product_id": product["product_id"],
                    "model": product["model"],
                    "target_key": target_key,
                    "reason": "not_known_as_of",
                    "detail": "available_at 晚于分析截止时刻 as_of",
                    "decision_lead_hours": None,
                }
            )
            continue

        for lead in sorted(leads_hours):
            decision_at = datetime.fromtimestamp(target_start.timestamp() - lead * 3600.0, tz=target_start.tzinfo)
            eligible = available <= decision_at
            reason = None
            if not eligible:
                lag = hours_between(available, decision_at)
                reason = (
                    f"not_available_at_decision_lead: 决策时刻 {decision_at.isoformat()} "
                    f"之后 {lag:g} 小时才收到"
                )
            matches.append(
                Match(
                    product_id=product["product_id"],
                    model=product["model"],
                    target_key=target_key,
                    event_id=target["event_id"],
                    decision_lead_hours=lead,
                    nominal_horizon_hours=int(product["horizon_hours"]),
                    effective_lead_hours=effective_lead,
                    issued_at=product["issued_at"],
                    available_at=product["available_at"],
                    probability=float(product["probability"]),
                    threshold=float(product["threshold"]),
                    quantiles={str(k): float(v) for k, v in product.get("quantiles", {}).items()},
                    revision_of=product.get("revision_of"),
                    eligible=eligible,
                    exclusion_reason=reason,
                )
            )
    return matches, exclusions


def as_received_series(matches: Sequence[Match]) -> list[dict[str, Any]]:
    """每个产品按其真实收到时点的有效提前量序列（不做决策提前量筛选）。"""
    seen: dict[str, Match] = {}
    for match in matches:
        # 同一产品会在多个决策提前量行出现，取一行代表其有效提前量
        seen.setdefault(match.product_id, match)
    series = []
    for match in sorted(seen.values(), key=lambda item: (item.effective_lead_hours, item.product_id)):
        series.append(
            {
                "product_id": match.product_id,
                "model": match.model,
                "target_key": match.target_key,
                "event_id": match.event_id,
                "nominal_horizon_hours": match.nominal_horizon_hours,
                "effective_lead_hours": r6(match.effective_lead_hours),
                "available_at": match.available_at,
                "revision_of": match.revision_of,
                "probability": match.probability,
            }
        )
    return series


# --------------------------------------------------------------------------- #
# 评分
# --------------------------------------------------------------------------- #

def brier_score(probabilities: Sequence[float], outcomes: Sequence[int]) -> float:
    return sum((p - y) ** 2 for p, y in zip(probabilities, outcomes)) / len(probabilities)


def brier_skill_score(forecast_bs: float, baseline_bs: float) -> float | None:
    if baseline_bs <= 0.0:
        return None
    return 1.0 - forecast_bs / baseline_bs


def roc_auc(probabilities: Sequence[float], outcomes: Sequence[int]) -> float | None:
    """trapezoid ROC AUC；只有一类事件时不可定义。"""
    positives = [p for p, y in zip(probabilities, outcomes) if y == 1]
    negatives = [p for p, y in zip(probabilities, outcomes) if y == 0]
    if not positives or not negatives:
        return None
    # Mann-Whitney U 形式，平局各计 0.5
    wins = 0.0
    for pos in positives:
        for neg in negatives:
            if pos > neg:
                wins += 1.0
            elif pos == neg:
                wins += 0.5
    return wins / (len(positives) * len(negatives))


def pinball_loss(quantile: float, quantile_value: float, observed: float) -> float:
    error = observed - quantile_value
    return error * quantile if error >= 0 else error * (quantile - 1.0)


def climatology_baseline(
    train_targets: Sequence[dict[str, Any]],
    mode: str,
    threshold: float,
) -> dict[str, Any]:
    """训练期气候概率与气候分位数，评估期冻结。"""
    amounts = [item[MODE_KEY[mode]]["amount"] for item in train_targets]
    events = [event_outcome(amount, threshold) for amount in amounts]
    rate = sum(events) / len(events) if events else None
    ordered = sorted(amounts)
    quantiles = {}
    if ordered:
        for level in (0.1, 0.5, 0.9):
            position = level * (len(ordered) - 1)
            lower = int(position)
            upper = min(lower + 1, len(ordered) - 1)
            weight = position - lower
            quantiles[str(level)] = r6(ordered[lower] * (1 - weight) + ordered[upper] * weight)
    return {
        "event_rate": r6(rate),
        "quantiles": quantiles,
        "training_targets": len(train_targets),
        "training_events": sum(events),
    }


def reliability_bins(probabilities: Sequence[float], outcomes: Sequence[int]) -> list[dict[str, Any]]:
    """固定 5 档可靠性分组；样本不足的分组标记 support_sufficient=False。"""
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.000001]
    bins = []
    for index in range(5):
        low, high = edges[index], edges[index + 1]
        members = [(p, y) for p, y in zip(probabilities, outcomes) if low <= p < high]
        n = len(members)
        if n == 0:
            bins.append(
                {
                    "bin": index + 1,
                    "range": [r6(low), r6(min(high, 1.0))],
                    "sample_count": 0,
                    "mean_forecast": None,
                    "observed_frequency": None,
                    "support_sufficient": False,
                }
            )
            continue
        mean_forecast = sum(p for p, _ in members) / n
        observed = sum(y for _, y in members) / n
        bins.append(
            {
                "bin": index + 1,
                "range": [r6(low), r6(min(high, 1.0))],
                "sample_count": n,
                "mean_forecast": r6(mean_forecast),
                "observed_frequency": r6(observed),
                "support_sufficient": n >= MIN_BIN_SAMPLES,
            }
        )
    return bins


def murphy_decomposition(probabilities: Sequence[float], outcomes: Sequence[int]) -> dict[str, Any]:
    """Brier 分数的可靠性-解析度-不确定性分解（按唯一预报值分组）。"""
    n = len(probabilities)
    base_rate = sum(outcomes) / n
    uncertainty = base_rate * (1.0 - base_rate)
    groups: dict[float, list[int]] = {}
    for p, y in zip(probabilities, outcomes):
        groups.setdefault(p, []).append(y)
    reliability = 0.0
    resolution = 0.0
    for p, ys in groups.items():
        nk = len(ys)
        freq = sum(ys) / nk
        reliability += (nk / n) * (p - freq) ** 2
        resolution += (nk / n) * (freq - base_rate) ** 2
    return {
        "reliability": r6(reliability),
        "resolution": r6(resolution),
        "uncertainty": r6(uncertainty),
        "check_bs": r6(reliability - resolution + uncertainty),
    }


def quantile_diagnostics(
    rows: Sequence[dict[str, Any]],
    levels: Sequence[float],
) -> list[dict[str, Any]]:
    """分位数覆盖率与 pinball 损失；每个 (model, target) 只取一条，避免多时效重复计数。"""
    chosen: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        key = (row["model"], row["target_key"])
        current = chosen.get(key)
        if current is None or row["nominal_horizon_hours"] < current["nominal_horizon_hours"]:
            chosen[key] = row
    diagnostics = []
    for level in levels:
        key = str(level)
        covered = 0
        pinballs = []
        used = 0
        for row in chosen.values():
            if key not in row["quantiles"]:
                continue
            used += 1
            value = row["quantiles"][key]
            observed = row["amount"]
            if observed <= value:
                covered += 1
            pinballs.append(pinball_loss(level, value, observed))
        diagnostics.append(
            {
                "quantile": key,
                "sample_count": used,
                "nominal_level": level,
                "observed_coverage": r6(covered / used) if used else None,
                "coverage_gap": r6(covered / used - level) if used else None,
                "mean_pinball_loss": r6(sum(pinballs) / len(pinballs)) if pinballs else None,
                "support_sufficient": used >= MIN_PAIRED_SAMPLES,
            }
        )
    return diagnostics


# --------------------------------------------------------------------------- #
# 聚类 bootstrap 不确定性（同一 event_id 的多个时效/相邻区间视为相关）
# --------------------------------------------------------------------------- #

def cluster_bootstrap(
    rows: Sequence[dict[str, Any]],
    statistic,
    cluster_key: str = "event_id",
    replicates: int = BOOTSTRAP_REPLICATES,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    clusters: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        clusters.setdefault(row[cluster_key], []).append(row)
    cluster_ids = sorted(clusters, key=lambda item: str(item))
    rng = random.Random(seed)
    estimates = []
    for _ in range(replicates):
        sampled_rows: list[dict[str, Any]] = []
        for _ in range(len(cluster_ids)):
            cid = cluster_ids[rng.randrange(len(cluster_ids))]
            sampled_rows.extend(clusters[cid])
        estimates.append(statistic(sampled_rows))
    estimates = [value for value in estimates if value is not None and math.isfinite(value)]
    if not estimates:
        return {"ci_low": None, "ci_high": None, "replicates": 0, "clusters": len(cluster_ids)}
    ordered = sorted(estimates)
    alpha = (1.0 - CONFIDENCE_LEVEL) / 2.0

    def quantile(fraction: float) -> float:
        position = fraction * (len(ordered) - 1)
        lower = int(position)
        upper = min(lower + 1, len(ordered) - 1)
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "ci_low": r6(quantile(alpha)),
        "ci_high": r6(quantile(1.0 - alpha)),
        "replicates": len(estimates),
        "clusters": len(cluster_ids),
        "confidence_level": CONFIDENCE_LEVEL,
        "method": "cluster_bootstrap_resampled_by_event_id",
    }


def paired_bs_difference(
    rows_a: Sequence[dict[str, Any]],
    rows_b: Sequence[dict[str, Any]],
    outcome_key: str,
) -> tuple[list[dict[str, Any]], float]:
    """共同样本上的逐目标 Brier 差（A - B），负值表示 A 更准。"""
    by_key_b = {row["target_key"]: row for row in rows_b}
    paired = []
    for row in rows_a:
        other = by_key_b.get(row["target_key"])
        if other is None:
            continue
        diff = (row["probability"] - row[outcome_key]) ** 2 - (
            other["probability"] - other[outcome_key]
        ) ** 2
        paired.append(
            {
                "target_key": row["target_key"],
                "event_id": row["event_id"],
                "bs_a": r6((row["probability"] - row[outcome_key]) ** 2),
                "bs_b": r6((other["probability"] - other[outcome_key]) ** 2),
                "difference_a_minus_b": r6(diff),
            }
        )
    mean_diff = sum(item["difference_a_minus_b"] for item in paired) / len(paired) if paired else float("nan")
    return paired, mean_diff


# --------------------------------------------------------------------------- #
# 主分析
# --------------------------------------------------------------------------- #

def _select_latest_vintage(rows: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """每个目标只保留决策时刻可得的最新 vintage（按 available_at 最晚）。

    同一模型对同一目标可能有多个名义时效产品同时满足提前量（如迟到的长时效产品
    与短时效产品），用户实际持有的是最新版本，不能把两条都当作独立样本计分。
    """
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        current = latest.get(row["target_key"])
        if current is None or row["effective_lead_hours"] < current["effective_lead_hours"]:
            # effective_lead 越小意味着 available_at 越晚（越临近目标）
            latest[row["target_key"]] = row
    return sorted(latest.values(), key=lambda item: item["target_key"])


def _scored_rows(
    matches: Sequence[Match],
    targets: Sequence[dict[str, Any]],
    mode: str,
    lead: int,
    models: Sequence[str],
) -> dict[str, list[dict[str, Any]]]:
    target_by_key = {item["target_key"]: item for item in targets}
    rows_by_model: dict[str, list[dict[str, Any]]] = {model: [] for model in models}
    for match in matches:
        if match.decision_lead_hours != lead or not match.eligible:
            continue
        if match.model not in rows_by_model:
            continue
        target = target_by_key[match.target_key]
        rows_by_model[match.model].append(
            {
                "product_id": match.product_id,
                "model": match.model,
                "target_key": match.target_key,
                "event_id": match.event_id,
                "nominal_horizon_hours": match.nominal_horizon_hours,
                "effective_lead_hours": match.effective_lead_hours,
                "probability": match.probability,
                "quantiles": match.quantiles,
                "threshold": match.threshold,
                "amount": target[MODE_KEY[mode]]["amount"],
                "truth_id": target[MODE_KEY[mode]]["truth_id"],
                "outcome": event_outcome(target[MODE_KEY[mode]]["amount"], match.threshold),
            }
        )
    for rows in rows_by_model.values():
        rows.sort(key=lambda item: item["target_key"])
    return rows_by_model


def _score_model(
    rows: Sequence[dict[str, Any]],
    baseline_rate: float | None,
    baseline_quantiles: dict[str, float],
) -> dict[str, Any] | None:
    if not rows:
        return None
    probabilities = [row["probability"] for row in rows]
    outcomes = [row["outcome"] for row in rows]
    bs = brier_score(probabilities, outcomes)
    baseline_bs = baseline_rate * (1.0 - baseline_rate) if baseline_rate is not None else None
    auc = roc_auc(probabilities, outcomes)

    # 气候基线 pinball：训练期气候分位数对同一评估样本
    baseline_pinballs = []
    for row in rows:
        for key, value in baseline_quantiles.items():
            baseline_pinballs.append(pinball_loss(float(key), value, row["amount"]))

    bs_ci = cluster_bootstrap(
        [dict(row, _stat=((row["probability"] - row["outcome"]) ** 2)) for row in rows],
        lambda sampled: sum(item["_stat"] for item in sampled) / len(sampled),
    )
    return {
        "sample_count": len(rows),
        "cluster_count": len({row["event_id"] for row in rows}),
        "brier_score": r6(bs),
        "brier_score_ci": bs_ci,
        "brier_skill_score": r6(brier_skill_score(bs, baseline_bs)) if baseline_bs is not None else None,
        "baseline_brier_score": r6(baseline_bs),
        "roc_auc": r6(auc),
        "mean_pinball_loss": r6(
            sum(pinball_loss(float(q), qv, row["amount"]) for row in rows for q, qv in row["quantiles"].items())
            / max(1, sum(len(row["quantiles"]) for row in rows))
        ),
        "baseline_mean_pinball_loss": r6(
            sum(baseline_pinballs) / len(baseline_pinballs) if baseline_pinballs else None
        ),
        "product_ids": sorted({row["product_id"] for row in rows}),
    }


def _ranking(
    rows_fast: Sequence[dict[str, Any]],
    rows_late: Sequence[dict[str, Any]],
    outcome_key: str,
) -> dict[str, Any]:
    paired, mean_diff = paired_bs_difference(rows_fast, rows_late, outcome_key)
    clusters = len({item["event_id"] for item in paired})
    support_ok = len(paired) >= MIN_PAIRED_SAMPLES and clusters >= MIN_CLUSTERS
    result: dict[str, Any] = {
        "common_sample_count": len(paired),
        "cluster_count": clusters,
        "support_sufficient": support_ok,
        "paired_differences": paired,
    }
    if not paired:
        result.update(
            {
                "mean_brier_difference_fast_minus_late": None,
                "difference_ci": None,
                "ranked": False,
                "decision": "withhold_no_common_sample",
            }
        )
        return result
    by_key = {item["target_key"]: item for item in paired}
    ci = cluster_bootstrap(
        [
            {"event_id": item["event_id"], "target_key": item["target_key"], "_diff": item["difference_a_minus_b"]}
            for item in paired
        ],
        lambda sampled: sum(item["_diff"] for item in sampled) / len(sampled),
    )
    result["mean_brier_difference_fast_minus_late"] = r6(mean_diff)
    result["difference_ci"] = ci
    if not support_ok:
        result.update(ranked=False, decision="withhold_insufficient_support")
    elif ci["ci_low"] is None:
        result.update(ranked=False, decision="withhold_bootstrap_unavailable")
    elif ci["ci_low"] > 0:
        result.update(ranked=True, preferred_model="late", decision="ranked_late_more_accurate_on_common_samples")
    elif ci["ci_high"] < 0:
        result.update(ranked=True, preferred_model="fast", decision="ranked_fast_more_accurate_on_common_samples")
    else:
        result.update(ranked=False, decision="withhold_confidence_interval_includes_zero")
    return result


def _revision_gains(
    matches: Sequence[Match],
    targets: Sequence[dict[str, Any]],
    mode: str,
) -> list[dict[str, Any]]:
    target_by_key = {item["target_key"]: item for item in targets}
    # 每个产品在不同决策提前量行重复出现，先按 product_id 去重为产品级记录
    unique_products: dict[str, Match] = {}
    for match in matches:
        unique_products.setdefault(match.product_id, match)

    pair_groups: dict[int, list[tuple[Match, Match]]] = {}
    for revised in unique_products.values():
        if not revised.revision_of:
            continue
        base = unique_products.get(revised.revision_of)
        if base is None or base.target_key != revised.target_key:
            continue
        pair_groups.setdefault(base.nominal_horizon_hours, []).append((base, revised))

    gains = []
    for horizon in sorted(pair_groups):
        pairs = sorted(pair_groups[horizon], key=lambda item: item[0].product_id)
        detail = []
        diffs = []
        timely = 0
        for base, revised in pairs:
            target = target_by_key[base.target_key]
            target_start = parse_ts(target["target_start"])
            outcome = event_outcome(target[MODE_KEY[mode]]["amount"], base.threshold)
            bs_base = (base.probability - outcome) ** 2
            bs_revised = (revised.probability - outcome) ** 2
            diff = bs_base - bs_revised  # 正：订正降低 Brier
            decision_at = datetime.fromtimestamp(
                target_start.timestamp() - horizon * 3600.0,
                tz=target_start.tzinfo,
            )
            revised_timely = parse_ts(revised.available_at) <= decision_at
            if revised_timely:
                timely += 1
            diffs.append({"event_id": base.event_id, "_diff": diff})
            detail.append(
                {
                    "target_key": base.target_key,
                    "event_id": base.event_id,
                    "base_product_id": base.product_id,
                    "revised_product_id": revised.product_id,
                    "base_probability": base.probability,
                    "revised_probability": revised.probability,
                    "bs_base": r6(bs_base),
                    "bs_revised": r6(bs_revised),
                    "brier_reduction": r6(diff),
                    "revised_available_at": revised.available_at,
                    "revised_effective_lead_hours": r6(revised.effective_lead_hours),
                    "nominal_lead_hours": horizon,
                    "revised_timely_at_nominal_lead": revised_timely,
                    "truth_id": target[MODE_KEY[mode]]["truth_id"],
                }
            )
        ci = cluster_bootstrap(diffs, lambda sampled: sum(item["_diff"] for item in sampled) / len(sampled))
        gains.append(
            {
                "nominal_horizon_hours": horizon,
                "pair_count": len(detail),
                "cluster_count": len({item["event_id"] for item in detail}),
                "mean_brier_reduction": r6(sum(item["brier_reduction"] for item in detail) / len(detail)),
                "reduction_ci": ci,
                "pairs_timely_at_nominal_lead": timely,
                "pairs_late_at_nominal_lead": len(detail) - timely,
                "support_sufficient": len(detail) >= MIN_PAIRED_SAMPLES
                and len({item["event_id"] for item in detail}) >= MIN_CLUSTERS,
                "detail": detail,
            }
        )
    return gains


def analyze(input_payload: dict[str, Any]) -> dict[str, Any]:
    as_of = parse_ts(input_payload["as_of"])
    study = input_payload["study"]
    leads = sorted(int(value) for value in study["leads_hours"])
    training_end = parse_ts(study["training_end"])
    products = input_payload["products"]
    if not products:
        raise ValueError("输入缺少 products，无法进行预报价值分析")
    targets = build_targets(input_payload["truth"], as_of)
    threshold = float(products[0]["threshold"])

    matches, exclusions = match_products(products, targets, leads, as_of)
    models = sorted({match.model for match in matches})

    train_targets = [item for item in targets if parse_ts(item["target_start"]) < training_end]
    eval_targets = [item for item in targets if parse_ts(item["target_start"]) >= training_end]

    # ---- 两种实况口径分别评分 ----
    scores: dict[str, Any] = {}
    calibration: dict[str, Any] = {}
    revision_gains: dict[str, Any] = {}
    rankings: dict[str, Any] = {}

    quantile_levels = sorted({float(level) for match in matches for level in match.quantiles})

    for mode in study["truth_modes"]:
        baseline = climatology_baseline(train_targets, mode, threshold)
        mode_block: dict[str, Any] = {
            "evaluation_window": {"training_end": study["training_end"], "targets_total": len(targets)},
            "baseline": {
                "kind": "historical_climatology_frozen_at_training_end",
                **baseline,
            },
            "by_decision_lead": {},
        }
        eval_keys = {item["target_key"] for item in eval_targets}

        for lead in leads:
            rows_by_model = _scored_rows(matches, targets, mode, lead, models)
            lead_block: dict[str, Any] = {"models": {}}
            for model, rows in rows_by_model.items():
                eval_rows = _select_latest_vintage(
                    [row for row in rows if row["target_key"] in eval_keys]
                )
                scored = _score_model(eval_rows, baseline["event_rate"], baseline["quantiles"])
                if scored is not None:
                    scored["training_window_sample_count"] = len(
                        _select_latest_vintage(rows)
                    )
                lead_block["models"][model] = scored
            # 共同样本排名（仅评估期，每个目标每模型只取决策时刻最新 vintage）
            fast_eval = _select_latest_vintage(
                [row for row in rows_by_model.get("fast", []) if row["target_key"] in eval_keys]
            )
            late_eval = _select_latest_vintage(
                [row for row in rows_by_model.get("late", []) if row["target_key"] in eval_keys]
            )
            ranking = _ranking(fast_eval, late_eval, "outcome")
            lead_block["common_sample_ranking"] = ranking
            lead_block["vintage_selection"] = (
                "每个目标取决策时刻 available_at 最晚的可得版本，迟到产品不被事后最优替换"
            )
            rankings.setdefault(mode, {})[str(lead)] = ranking
            mode_block["by_decision_lead"][str(lead)] = lead_block

        scores[mode] = mode_block

        # ---- 校准诊断（全窗口，保证样本量；明确区分于评估期评分）----
        cal_block: dict[str, Any] = {"window": "all_matched_targets", "models": {}}
        for model in models:
            all_rows = []
            for lead in leads:
                all_rows.extend(_scored_rows(matches, targets, mode, lead, [model]).get(model, []))
            # 每个目标只保留一条（最短名义时效），避免相邻时效重复计数
            dedup: dict[str, dict[str, Any]] = {}
            for row in all_rows:
                current = dedup.get(row["target_key"])
                if current is None or row["nominal_horizon_hours"] < current["nominal_horizon_hours"]:
                    dedup[row["target_key"]] = row
            model_rows = sorted(dedup.values(), key=lambda item: item["target_key"])
            if not model_rows:
                cal_block["models"][model] = None
                continue
            probabilities = [row["probability"] for row in model_rows]
            outcomes = [row["outcome"] for row in model_rows]
            cal_block["models"][model] = {
                "sample_count": len(model_rows),
                "cluster_count": len({row["event_id"] for row in model_rows}),
                "mean_forecast": r6(sum(probabilities) / len(probabilities)),
                "observed_event_frequency": r6(sum(outcomes) / len(outcomes)),
                "frequency_bias": r6(sum(probabilities) / len(probabilities) - sum(outcomes) / len(outcomes)),
                "reliability_bins": reliability_bins(probabilities, outcomes),
                "murphy_decomposition": murphy_decomposition(probabilities, outcomes),
                "quantile_coverage": quantile_diagnostics(model_rows, quantile_levels),
            }
        calibration[mode] = cal_block

        # ---- 临近更新增益（版本链 revision_of）----
        revision_gains[mode] = {
            "interpretation": "正值表示订正版本在共同样本上降低 Brier 分数；迟到订正的增益以其真实有效提前量为条件，不能归功于名义提前量",
            "by_horizon": _revision_gains(matches, targets, mode),
        }

    # ---- 配对明细与排除说明 ----
    matched_products = {
        "matching_rule": "按 available_at 与决策提前量配对；仅目标区间与实况区间完全一致的产品参与评分",
        "eligible": [
            {
                "product_id": m.product_id,
                "model": m.model,
                "target_key": m.target_key,
                "event_id": m.event_id,
                "decision_lead_hours": m.decision_lead_hours,
                "nominal_horizon_hours": m.nominal_horizon_hours,
                "effective_lead_hours": r6(m.effective_lead_hours),
                "issued_at": m.issued_at,
                "available_at": m.available_at,
                "revision_of": m.revision_of,
                "probability": m.probability,
            }
            for m in matches
            if m.eligible
        ],
        "excluded_at_lead": [
            {
                "product_id": m.product_id,
                "model": m.model,
                "target_key": m.target_key,
                "decision_lead_hours": m.decision_lead_hours,
                "nominal_horizon_hours": m.nominal_horizon_hours,
                "effective_lead_hours": r6(m.effective_lead_hours),
                "available_at": m.available_at,
                "reason": m.exclusion_reason,
            }
            for m in matches
            if not m.eligible
        ],
        "excluded_products": exclusions,
        "as_received_series": as_received_series(matches),
    }

    # ---- 样本支持总览 ----
    sample_support = {
        "targets_total": len(targets),
        "training_targets": len(train_targets),
        "evaluation_targets": len(eval_targets),
        "evaluation_clusters": len({item["event_id"] for item in eval_targets}),
        "thresholds": {
            "min_clusters_for_ranking": MIN_CLUSTERS,
            "min_paired_samples_for_ranking": MIN_PAIRED_SAMPLES,
            "min_samples_per_reliability_bin": MIN_BIN_SAMPLES,
            "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            "confidence_level": CONFIDENCE_LEVEL,
        },
        "clustering": "按 event_id 聚类 bootstrap，同一过程的多时效与相邻区间不视为独立观测",
        "by_lead_and_mode": {},
        "withheld_cells": [],
    }
    for mode in study["truth_modes"]:
        sample_support["by_lead_and_mode"][mode] = {}
        for lead in leads:
            ranking = rankings[mode][str(lead)]
            cell = {
                "common_sample_count": ranking["common_sample_count"],
                "cluster_count": ranking["cluster_count"],
                "support_sufficient": ranking["support_sufficient"],
                "ranked": ranking["ranked"],
                "decision": ranking["decision"],
            }
            sample_support["by_lead_and_mode"][mode][str(lead)] = cell
            if not ranking["ranked"]:
                sample_support["withheld_cells"].append(
                    {"truth_mode": mode, "decision_lead_hours": lead, "decision": ranking["decision"]}
                )

    analysis_id = "forecast-value-" + hashlib.sha256(
        stable_json(
            {
                "products": products,
                "truth": input_payload["truth"],
                "study": study,
                "as_of": input_payload["as_of"],
            }
        ).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "analysis_id": analysis_id,
        "schema_version": 1,
        "as_of": input_payload["as_of"],
        "source_versions": input_payload.get("source_versions", {}),
        "baseline_case_id": input_payload.get("baseline_case_id"),
        "lineage_link": {
            "baseline_case_id": input_payload.get("baseline_case_id"),
            "vintage_key": "product_id + revision_of",
            "note": "linked_run_id 在持久化阶段连接既有 runs 表后补全",
        },
        "matched_products": matched_products,
        "scores": scores,
        "calibration": calibration,
        "revision_gains": revision_gains,
        "sample_support": sample_support,
    }
