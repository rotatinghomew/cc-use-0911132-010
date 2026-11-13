"""forecast_value 分析的方法正确性测试。

覆盖业务反例：
* 迟到的“事后订正很准”版本不能在名义提前量上赢得比较；
* 缺少共同覆盖时段的产品被整体排除；
* 不同累积时段不与同一实况值比较；
* 共同样本、聚类 bootstrap 与样本不足不排名；
* 两种实况口径（最初 / 最终）可解释且结论可不同。
"""
from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from progressive_forecast_chain.forecast_value.engine import (
    analyze,
    brier_score,
    brier_skill_score,
    build_targets,
    cluster_bootstrap,
    load_input,
    match_products,
    parse_ts,
    pinball_loss,
    reliability_bins,
    roc_auc,
)
from progressive_forecast_chain.forecast_value.storage import AnalysisStore

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "analysis" / "input.json"


class ForecastValueTests(unittest.TestCase):
    def setUp(self):
        self.payload = load_input(FIXTURE)
        self.result = analyze(copy.deepcopy(self.payload))

    # ------------------------------------------------------------------ #
    # 结构与口径
    # ------------------------------------------------------------------ #
    def test_required_top_level_fields(self):
        for field in (
            "analysis_id",
            "matched_products",
            "scores",
            "calibration",
            "revision_gains",
            "sample_support",
        ):
            self.assertIn(field, self.result)
        self.assertTrue(self.result["analysis_id"].startswith("forecast-value-"))

    def test_analysis_id_is_deterministic(self):
        again = analyze(copy.deepcopy(self.payload))
        self.assertEqual(again["analysis_id"], self.result["analysis_id"])

    def test_two_truth_modes_present(self):
        self.assertEqual(set(self.result["scores"]), {"first_available", "final"})
        targets = build_targets(self.payload["truth"], parse_ts(self.payload["as_of"]))
        revised = next(item for item in targets if "2026-08-21" in item["target_key"])
        self.assertNotEqual(revised["first"]["amount"], revised["final"]["amount"])
        self.assertEqual(revised["first"]["amount"], 4)
        self.assertEqual(revised["final"]["amount"], 17)
        self.assertTrue(revised["final"]["revised"])

    # ------------------------------------------------------------------ #
    # 反例一：缺少共同覆盖时段
    # ------------------------------------------------------------------ #
    def test_partial_target_excluded_for_no_common_window(self):
        excluded = self.result["matched_products"]["excluded_products"]
        partial = [item for item in excluded if item["product_id"] == "partial-target"]
        self.assertEqual(len(partial), 1)
        self.assertEqual(partial[0]["reason"], "no_common_coverage_window")
        # 不允许出现在任何评分或校准产品里
        eligible_ids = {item["product_id"] for item in self.result["matched_products"]["eligible"]}
        self.assertNotIn("partial-target", eligible_ids)
        for mode_block in self.result["calibration"].values():
            self.assertNotIn("partial", mode_block["models"])

    def test_different_accumulation_windows_never_pair(self):
        as_of = parse_ts(self.payload["as_of"])
        targets = build_targets(self.payload["truth"], as_of)
        matches, exclusions = match_products(
            self.payload["products"], targets, self.payload["study"]["leads_hours"], as_of
        )
        partial_matches = [m for m in matches if m.model == "partial"]
        self.assertEqual(partial_matches, [])
        self.assertTrue(exclusions)

    # ------------------------------------------------------------------ #
    # 反例二：迟到但事后很准的订正
    # ------------------------------------------------------------------ #
    def test_late_vintage_unavailable_at_nominal_24h_lead(self):
        excluded = self.result["matched_products"]["excluded_at_lead"]
        late_24 = [
            item
            for item in excluded
            if item["product_id"].endswith("-1-1") and item["decision_lead_hours"] == 24
        ]
        self.assertEqual(len(late_24), 19)
        for item in late_24:
            self.assertEqual(item["effective_lead_hours"], 6.0)
            self.assertIn("not_available_at_decision_lead", item["reason"])

    def test_late_72_unavailable_at_72h_decision_lead(self):
        excluded = self.result["matched_products"]["excluded_at_lead"]
        late_72 = [
            item
            for item in excluded
            if item["product_id"].endswith("-3-1") and item["decision_lead_hours"] == 72
        ]
        self.assertEqual(len(late_72), 19)

    def test_late_only_wins_where_actually_available(self):
        # 24h 决策提前量：late-72 vintage（有效提前量 54h）确实已到，可以赢
        for mode in ("first_available", "final"):
            block = self.result["scores"][mode]["by_decision_lead"]["24"]
            ranking = block["common_sample_ranking"]
            self.assertTrue(ranking["ranked"])
            self.assertEqual(ranking["preferred_model"], "late")
            self.assertEqual(ranking["common_sample_count"], 10)
        # 72h 决策提前量：late 完全不可得 -> 不排名，而不是宣布 late 更好
        block72 = self.result["scores"]["first_available"]["by_decision_lead"]["72"]
        self.assertIsNone(block72["models"]["late"])
        ranking72 = block72["common_sample_ranking"]
        self.assertFalse(ranking72["ranked"])
        self.assertEqual(ranking72["decision"], "withhold_no_common_sample")

    def test_no_post_hoc_substitution_in_eligible_set(self):
        # 目标 2026-08-21 在 24h 决策点只能拿到 fast 的 product-20-1-0
        eligible = self.result["matched_products"]["eligible"]
        target20 = [item for item in eligible if "2026-08-21" in item["target_key"]]
        lead24 = [item for item in target20 if item["decision_lead_hours"] == 24]
        ids = {item["product_id"] for item in lead24}
        self.assertIn("product-20-1-0", ids)
        self.assertNotIn("product-20-1-1", ids)

    # ------------------------------------------------------------------ #
    # 评分数值正确性
    # ------------------------------------------------------------------ #
    def test_brier_and_skill_score_math(self):
        self.assertAlmostEqual(brier_score([0.6] * 3, [1, 0, 0]), (0.16 + 0.36 + 0.36) / 3)
        self.assertAlmostEqual(brier_skill_score(0.1, 0.2), 0.5)
        self.assertIsNone(brier_skill_score(0.1, 0.0))

    def test_roc_auc_degenerate_and_perfect(self):
        self.assertIsNone(roc_auc([0.1, 0.2], [0, 0]))
        self.assertEqual(roc_auc([0.9, 0.8, 0.1], [1, 1, 0]), 1.0)
        self.assertAlmostEqual(roc_auc([0.5, 0.5], [1, 0]), 0.5)

    def test_pinball_loss(self):
        # 观测高于分位数：tau * (y - q)
        self.assertAlmostEqual(pinball_loss(0.5, 10, 12), 1.0)
        self.assertAlmostEqual(pinball_loss(0.1, 10, 12), 0.2)
        # 观测低于分位数：(tau - 1) * (y - q)
        self.assertAlmostEqual(pinball_loss(0.1, 10, 8), 1.8)

    def test_evaluation_scores_match_hand_calculation(self):
        block = self.result["scores"]["first_available"]["by_decision_lead"]["24"]
        fast = block["models"]["fast"]
        late = block["models"]["late"]
        # 评估期 10 个目标，fast 恒为 0.6，事件 5 个：BS = 0.4*0.16 + 0.6*0.36
        self.assertAlmostEqual(fast["brier_score"], 0.26, places=6)
        # late 完全命中（0.05/0.95）：BS = 0.0025
        self.assertAlmostEqual(late["brier_score"], 0.0025, places=6)
        self.assertAlmostEqual(late["roc_auc"], 1.0, places=6)
        # 训练期气候率 4/9，基线 BS = p(1-p)
        p = 4 / 9
        self.assertAlmostEqual(fast["baseline_brier_score"], p * (1 - p), places=6)
        self.assertAlmostEqual(
            fast["brier_skill_score"], 1 - fast["brier_score"] / (p * (1 - p)), places=6
        )

    def test_baseline_trained_only_on_training_window(self):
        baseline = self.result["scores"]["first_available"]["baseline"]
        self.assertEqual(baseline["training_targets"], 9)
        self.assertEqual(baseline["training_events"], 4)
        # 评估期 10 个目标不能回流进基线
        self.assertEqual(self.result["sample_support"]["evaluation_targets"], 10)

    def test_final_truth_flips_revision_gain_for_revised_target(self):
        def gain(mode):
            detail = self.result["revision_gains"][mode]["by_horizon"][0]["detail"]
            return next(item for item in detail if "2026-08-21" in item["target_key"])

        first = gain("first_available")
        final = gain("final")
        # 最初实况（4，无事件）下 0.05 的订正看似很准，增益为正
        self.assertGreater(first["brier_reduction"], 0)
        # 最终实况（17，事件发生）下同一订正是错的，增益翻负
        self.assertLess(final["brier_reduction"], 0)
        self.assertEqual(final["truth_id"], "truth-20-r2")

    def test_all_revision_pairs_are_late_at_nominal_lead(self):
        for mode in ("first_available", "final"):
            for block in self.result["revision_gains"][mode]["by_horizon"]:
                self.assertEqual(block["pairs_timely_at_nominal_lead"], 0)
                self.assertEqual(block["pairs_late_at_nominal_lead"], block["pair_count"])

    # ------------------------------------------------------------------ #
    # 校准诊断
    # ------------------------------------------------------------------ #
    def test_reliability_bins_and_support_flag(self):
        bins = reliability_bins([0.05] * 10 + [0.95] * 9, [0] * 10 + [1] * 9)
        populated = [b for b in bins if b["sample_count"] > 0]
        self.assertEqual(len(populated), 2)
        self.assertTrue(all(b["support_sufficient"] for b in populated))
        empty = [b for b in bins if b["sample_count"] == 0]
        self.assertTrue(all(not b["support_sufficient"] for b in empty))

    def test_quantile_coverage_diagnoses_miscalibration(self):
        fast_cal = self.result["calibration"]["first_available"]["models"]["fast"]
        q01 = next(item for item in fast_cal["quantile_coverage"] if item["quantile"] == "0.1")
        q09 = next(item for item in fast_cal["quantile_coverage"] if item["quantile"] == "0.9")
        # 固定分位数（q0.1=2，最小实况为4）永远覆盖不到 -> 覆盖率 0
        self.assertEqual(q01["observed_coverage"], 0.0)
        # q0.9=20，最大实况 17 -> 全覆盖
        self.assertEqual(q09["observed_coverage"], 1.0)

    # ------------------------------------------------------------------ #
    # 聚类 bootstrap 与样本支持
    # ------------------------------------------------------------------ #
    def test_cluster_bootstrap_respects_clusters(self):
        rows = [{"event_id": f"e{i % 3}", "v": 1.0} for i in range(30)]
        ci = cluster_bootstrap(rows, lambda sampled: sum(r["v"] for r in sampled) / len(sampled))
        self.assertEqual(ci["clusters"], 3)
        self.assertEqual(ci["replicates"], 2000)
        self.assertIsNotNone(ci["ci_low"])
        self.assertLessEqual(ci["ci_low"], ci["ci_high"])

    def test_bootstrap_is_deterministic(self):
        rows = [{"event_id": f"e{i % 4}", "v": float(i)} for i in range(40)]
        ci1 = cluster_bootstrap(rows, lambda sampled: sum(r["v"] for r in sampled) / len(sampled))
        ci2 = cluster_bootstrap(rows, lambda sampled: sum(r["v"] for r in sampled) / len(sampled))
        self.assertEqual(ci1, ci2)

    def test_withheld_cells_recorded(self):
        withheld = self.result["sample_support"]["withheld_cells"]
        leads = {(item["truth_mode"], item["decision_lead_hours"]) for item in withheld}
        self.assertIn(("first_available", 72), leads)
        self.assertIn(("final", 72), leads)

    def test_insufficient_support_withholds_ranking(self):
        tiny = copy.deepcopy(self.payload)
        # 只保留两个目标过程，低于 MIN_CLUSTERS
        keep_starts = {"2026-09-17T00:00:00+00:00", "2026-09-26T00:00:00"}
        tiny["products"] = [
            p for p in tiny["products"] if p["target_start"] in keep_starts
        ]
        tiny["truth"] = [t for t in tiny["truth"] if t["target_start"] in keep_starts]
        tiny["study"] = dict(tiny["study"], training_end="2026-09-01T00:00:00+00:00")
        result = analyze(tiny)
        for mode in ("first_available", "final"):
            ranking = result["scores"][mode]["by_decision_lead"]["24"]["common_sample_ranking"]
            self.assertFalse(ranking["ranked"])
            self.assertEqual(ranking["decision"], "withhold_insufficient_support")

    # ------------------------------------------------------------------ #
    # 持久化与重放
    # ------------------------------------------------------------------ #
    def test_storage_roundtrip_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "a.sqlite3"
            store = AnalysisStore(db)
            saved = store.save(copy.deepcopy(self.result), "fp-1")
            recalled = store.recall(self.result["analysis_id"])
            self.assertEqual(recalled, saved)
            # 再次保存不会产生重复分析
            store.save(copy.deepcopy(self.result), "fp-1")
            import sqlite3

            con = sqlite3.connect(db)
            self.assertEqual(
                con.execute("SELECT count(*) FROM forecast_value_analyses").fetchone()[0], 1
            )
            self.assertGreaterEqual(
                con.execute("SELECT count(*) FROM forecast_value_matches").fetchone()[0], 76
            )

    def test_lineage_link_to_existing_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "b.sqlite3"
            # 先跑既有 release 流程建立 runs 记录
            from progressive_forecast_chain.service import ApplicationService

            service = ApplicationService(db)
            demo = json.loads(
                (Path(__file__).resolve().parents[2] / "fixtures" / "demo.json").read_text()
            )
            run = service.execute(demo)
            store = AnalysisStore(db)
            saved = store.save(copy.deepcopy(self.result), "fp-2")
            self.assertEqual(saved["lineage_link"]["linked_run_id"], run["run_id"])


if __name__ == "__main__":
    unittest.main()
