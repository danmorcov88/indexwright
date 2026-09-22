from __future__ import annotations

from itertools import pairwise
from typing import TYPE_CHECKING, Any

from indexwright.model import Finding, Recommendation
from indexwright.rules.base import is_collscan, severity, usable, with_advice

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, ShapeStats
    from indexwright.rules.base import Indexes


def sorts_in_memory(stats: ShapeStats, explain: ExplainResult | None) -> bool:
    plan = usable(explain)
    if plan is None:
        return stats.has_sort_stage
    # SORT_MERGE merges index-ordered streams; every other SORT* stage sorts in memory.
    return any(s.startswith("SORT") and s != "SORT_MERGE" for s in plan.stages)


def check(stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes) -> list[Finding]:
    if not stats.shape.sort or is_collscan(stats, explain) or not sorts_in_memory(stats, explain):
        return []
    shape = stats.shape
    fields = ", ".join(f"{field}:{direction}" for field, direction in shape.sort)
    message = f"sort on {{{fields}}} is done in memory, no index supplies that order"
    plan = usable(explain)
    evidence = {
        "sort": [list(pair) for pair in shape.sort],
        "indexes_used": list(plan.index_names) if plan else [],
    }
    if sort_feeds_group(shape.pipeline):
        # An index-provided order before $group only trades the sort for random fetches.
        message += "; the sorted documents go straight into $group"
        advice = (
            "remove the $sort unless a $first or $last accumulator depends on that order; "
            "if it does, sort after the $group on the smaller result"
        )
        rewrite = Recommendation("rewrite", shape.ns, advice)
        return [
            Finding(
                severity(stats),
                "sort_in_memory",
                shape.ns,
                shape.fingerprint,
                message,
                rewrite,
                evidence,
            )
        ]
    return with_advice("sort_in_memory", stats, severity(stats), message, catalog, evidence)


def sort_feeds_group(pipeline: list[dict[str, Any]] | None) -> bool:
    names = [next(iter(stage)) for stage in pipeline or []]
    return any(a == "$sort" and b == "$group" for a, b in pairwise(names))
