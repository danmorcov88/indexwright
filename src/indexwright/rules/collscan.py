from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.rules.base import is_collscan, severity, usable, with_advice
from indexwright.rules.predicates import negation_only

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, Finding, ShapeStats
    from indexwright.rules.base import Indexes

SMALL_COLLECTION = 100


def check(stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes) -> list[Finding]:
    if not is_collscan(stats, explain) or negation_only(stats.shape.filter):
        return []
    per_execution = stats.docs_examined / max(stats.count, 1)
    level = "low" if per_execution < SMALL_COLLECTION else severity(stats)
    message = (
        f"collection scan, {per_execution:.0f} documents examined per execution, "
        f"{stats.docs_per_returned:.0f} per document returned"
    )
    plan = usable(explain)
    evidence = {
        "docs_examined_per_execution": round(per_execution),
        "docs_per_returned": round(stats.docs_per_returned, 1),
        "stages": list(plan.stages) if plan else sorted(stats.plan_summaries),
    }
    return with_advice("collscan", stats, level, message, catalog, evidence)
