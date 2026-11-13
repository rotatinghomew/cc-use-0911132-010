"""``python3 -m progressive_forecast_chain.forecast_value`` 业务入口。"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from progressive_forecast_chain.forecast_value.engine import analyze, load_input
from progressive_forecast_chain.forecast_value.storage import AnalysisStore
from progressive_forecast_chain.vintages.runtime import stable_json


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按实际收到时点计算概率预报价值（Brier/校准/订正增益/共同样本排名）"
    )
    parser.add_argument("--input", required=True, help="分析输入 JSON（forecast_json）")
    parser.add_argument("--database", required=True, help="既有 vintage/lineage SQLite 账本路径")
    parser.add_argument("--output", required=True, help="分析结果输出 JSON 路径")
    return parser


def run(input_path: str | Path, database: str | Path, output_path: str | Path) -> dict:
    payload = load_input(input_path)
    store = AnalysisStore(database)
    result = analyze(payload)
    input_fingerprint = hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest()

    recalled = store.recall(result["analysis_id"])
    if recalled is not None:
        result = recalled
    else:
        result = store.save(result, input_fingerprint)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    return result


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = run(args.input, args.database, args.output)
    print(
        json.dumps(
            {
                "analysis_id": result["analysis_id"],
                "output": str(args.output),
                "database": str(args.database),
                "targets": result["sample_support"]["targets_total"],
                "withheld_cells": len(result["sample_support"]["withheld_cells"]),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
