from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.rules.base import is_collscan, severity, usable, with_advice
from indexwright.rules.sort_in_memory import sorts_in_memory

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, Finding, IndexInfo, ShapeStats

THRESHOLD = 10


def check(
    stats: ShapeStats, explain: ExplainResult | None, indexes: list[IndexInfo]
) -> list[Finding]:
    plan = usable(explain)
    if plan is None or not stats.returned_known or not plan.fetch_filter_fields:
        return []
    if is_collscan(stats, explain) or sorts_in_memory(stats, explain):
        return []
    if stats.docs_per_returned <= THRESHOLD:
        return []
    fields = ", ".join(sorted(plan.fetch_filter_fields))
    used = ", ".join(plan.index_names) or "an index"
    message = (
        f"{used} is used but {stats.docs_per_returned:.0f} documents are examined per document "
        f"returned, the filter on {fields} runs after fetching each document"
    )
    evidence = {
        "docs_per_returned": round(stats.docs_per_returned, 1),
        "indexes_used": list(plan.index_names),
        "filter_fields_not_in_index": sorted(plan.fetch_filter_fields),
    }
    return with_advice("docs_examined_ratio", stats, severity(stats), message, indexes, evidence)
