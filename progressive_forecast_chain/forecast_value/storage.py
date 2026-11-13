"""分析结果持久化，复用既有 SQLite 账本并连接 vintage / lineage。

新增两张表，不改动既有 runs/events/idempotency/failures 表结构：

* ``forecast_value_analyses``：分析主记录（确定性 analysis_id，支持同输入重放）；
* ``forecast_value_matches``：配对与排除明细，保留每个产品在每个决策提前量上的
  可用状态与排除原因，可回溯到 vintage（product_id / revision_of）。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Mapping

from progressive_forecast_chain.vintages.runtime import stable_json, utc_now

ANALYSIS_TABLE = "forecast_value_analyses"
MATCH_TABLE = "forecast_value_matches"


class AnalysisStore:
    def __init__(self, database: str | Path):
        self.database = str(database)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=20)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        connection = self._connect()
        try:
            connection.executescript(
                f"""
                CREATE TABLE IF NOT EXISTS {ANALYSIS_TABLE}(
                    analysis_id TEXT PRIMARY KEY,
                    baseline_case_id TEXT,
                    input_fingerprint TEXT NOT NULL,
                    result TEXT NOT NULL,
                    linked_run_id TEXT,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS {MATCH_TABLE}(
                    match_id TEXT PRIMARY KEY,
                    analysis_id TEXT NOT NULL,
                    product_id TEXT NOT NULL,
                    model TEXT NOT NULL,
                    target_key TEXT,
                    event_id TEXT,
                    decision_lead_hours INTEGER,
                    nominal_horizon_hours INTEGER,
                    effective_lead_hours REAL,
                    eligible INTEGER NOT NULL,
                    exclusion_reason TEXT,
                    revision_of TEXT,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_match_analysis ON {MATCH_TABLE}(analysis_id);
                CREATE INDEX IF NOT EXISTS idx_match_product ON {MATCH_TABLE}(product_id);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def linked_run(self, baseline_case_id: str | None) -> str | None:
        """连接既有 vintage 运行记录（release.cli 写入的 runs 表）。"""
        if not baseline_case_id:
            return None
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT run_id FROM runs WHERE case_id=? ORDER BY updated_at DESC LIMIT 1",
                (baseline_case_id,),
            ).fetchone()
            return row["run_id"] if row else None
        except sqlite3.OperationalError:
            # 全新数据库且尚未运行过 release 流程时 runs 表可能不存在
            return None
        finally:
            connection.close()

    def recall(self, analysis_id: str) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                f"SELECT result FROM {ANALYSIS_TABLE} WHERE analysis_id=?", (analysis_id,)
            ).fetchone()
            if not row:
                return None
            import json

            return json.loads(row["result"])
        finally:
            connection.close()

    def save(self, result: Mapping[str, Any], input_fingerprint: str) -> dict[str, Any]:
        import hashlib

        analysis_id = result["analysis_id"]
        linked_run = self.linked_run(result.get("baseline_case_id"))
        persisted = dict(result)
        lineage = dict(persisted.get("lineage_link", {}))
        lineage["linked_run_id"] = linked_run
        persisted["lineage_link"] = lineage
        connection = self._connect()
        try:
            connection.execute(
                f"INSERT OR IGNORE INTO {ANALYSIS_TABLE}"
                "(analysis_id, baseline_case_id, input_fingerprint, result, linked_run_id, created_at)"
                " VALUES(?,?,?,?,?,?)",
                (
                    analysis_id,
                    persisted.get("baseline_case_id"),
                    input_fingerprint,
                    stable_json(persisted),
                    linked_run,
                    utc_now(),
                ),
            )
            matched = persisted["matched_products"]
            for item in matched.get("eligible", []):
                match_id = hashlib.sha256(
                    stable_json(["eligible", analysis_id, item]).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    f"INSERT OR IGNORE INTO {MATCH_TABLE}"
                    "(match_id, analysis_id, product_id, model, target_key, event_id, "
                    "decision_lead_hours, nominal_horizon_hours, effective_lead_hours, eligible, "
                    "exclusion_reason, revision_of, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        match_id,
                        analysis_id,
                        item["product_id"],
                        item["model"],
                        item.get("target_key"),
                        item.get("event_id"),
                        item.get("decision_lead_hours"),
                        item.get("nominal_horizon_hours"),
                        item.get("effective_lead_hours"),
                        1,
                        None,
                        item.get("revision_of"),
                        stable_json(item),
                        utc_now(),
                    ),
                )
            for item in matched.get("excluded_at_lead", []):
                match_id = hashlib.sha256(
                    stable_json(["excluded", analysis_id, item]).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    f"INSERT OR IGNORE INTO {MATCH_TABLE}"
                    "(match_id, analysis_id, product_id, model, target_key, event_id, "
                    "decision_lead_hours, nominal_horizon_hours, effective_lead_hours, eligible, "
                    "exclusion_reason, revision_of, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        match_id,
                        analysis_id,
                        item["product_id"],
                        item["model"],
                        item.get("target_key"),
                        None,
                        item.get("decision_lead_hours"),
                        item.get("nominal_horizon_hours"),
                        item.get("effective_lead_hours"),
                        0,
                        item.get("reason"),
                        None,
                        stable_json(item),
                        utc_now(),
                    ),
                )
            for item in matched.get("excluded_products", []):
                match_id = hashlib.sha256(
                    stable_json(["no_window", analysis_id, item]).encode("utf-8")
                ).hexdigest()
                connection.execute(
                    f"INSERT OR IGNORE INTO {MATCH_TABLE}"
                    "(match_id, analysis_id, product_id, model, target_key, event_id, "
                    "decision_lead_hours, nominal_horizon_hours, effective_lead_hours, eligible, "
                    "exclusion_reason, revision_of, detail, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        match_id,
                        analysis_id,
                        item["product_id"],
                        item["model"],
                        None,
                        None,
                        None,
                        None,
                        None,
                        0,
                        item.get("reason"),
                        None,
                        stable_json(item),
                        utc_now(),
                    ),
                )
            connection.commit()
        finally:
            connection.close()
        return persisted
