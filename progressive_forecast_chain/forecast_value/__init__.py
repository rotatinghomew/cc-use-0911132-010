"""按实际收到时点评估的概率预报价值分析。

入口模块 :mod:`progressive_forecast_chain.forecast_value.cli` 提供
``python3 -m progressive_forecast_chain.forecast_value`` 业务命令。
"""
from __future__ import annotations
from progressive_forecast_chain.forecast_value.engine import analyze, load_input

__all__ = ["analyze", "load_input"]
