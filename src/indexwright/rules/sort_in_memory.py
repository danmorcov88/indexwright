from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.rules.base import is_collscan, severity, usable, with_advice

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, Finding, IndexInfo, ShapeStats


def sorts_in_memory(stats: ShapeStats, explain: ExplainResult | None) -> bool:
    plan = usable(explain)
    if plan is None:
        return stats.has_sort_stage
    # SORT_MERGE merges index-ordered streams; every other SORT* stage sorts in memory.
    return any(s.startswith("SORT") and s != "SORT_MERGE" for s in plan.stages)


def check(
    stats: ShapeStats, explain: ExplainResult | None, indexes: list[IndexInfo]
) -> list[Finding]:
    if not stats.shape.sort or is_collscan(stats, explain) or not sorts_in_memory(stats, explain):
        return []
    fields = ", ".join(f"{field}:{direction}" for field, direction in stats.shape.sort)
    message = f"sort on {{{fields}}} is done in memory, no index supplies that order"
    plan = usable(explain)
    evidence = {
        "sort": [list(pair) for pair in stats.shape.sort],
        "indexes_used": list(plan.index_names) if plan else [],
    }
    return with_advice("sort_in_memory", stats, severity(stats), message, indexes, evidence)
