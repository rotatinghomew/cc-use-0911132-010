from __future__ import annotations
from typing import Any
from progressive_forecast_chain.vintages.runtime import RunState, SQLiteLedger, fingerprint, number, sequence

class ReleaseChain:
    artifact_key = 'release_lineage'

    def execute(self, state: RunState, stages: list[Any], ledger: SQLiteLedger) -> None:
        lineage = []
        previous = "received"
        for stage in stages:
            stage.apply(state)
            artifact = state.artifacts[stage.name]
            lineage.append({"from": previous, "to": stage.name, "score": artifact["score"], "revision": state.revision})
            previous = stage.name
            ledger.save(state)
        regressions = [item for left, item in zip(lineage, lineage[1:]) if item["score"] + 5 < left["score"]]
        state.artifacts[self.artifact_key] = {"lineage": lineage, "regressions": regressions, "publishable": not regressions, "signature": fingerprint(lineage)}
        state.emit(self.artifact_key, "release_lineage_completed", revisions=len(lineage))
