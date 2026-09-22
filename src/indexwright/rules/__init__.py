from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.model import SEVERITY_ORDER
from indexwright.rules import collscan, docs_examined_ratio, low_selectivity_index, sort_in_memory

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, Finding, IndexInfo, ShapeStats
    from indexwright.rules.base import Rule

RULES: list[tuple[str, Rule]] = [
    ("collscan", collscan.check),
    ("sort_in_memory", sort_in_memory.check),
    ("docs_examined_ratio", docs_examined_ratio.check),
    ("low_selectivity_index", low_selectivity_index.check),
]


def run_rules(
    stats: ShapeStats, explain: ExplainResult | None, indexes: list[IndexInfo]
) -> list[Finding]:
    return [finding for _, rule in RULES for finding in rule(stats, explain, indexes)]


def sort_findings(findings: list[Finding], weight: dict[str, int]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -weight.get(f.shape_id, 0)))
