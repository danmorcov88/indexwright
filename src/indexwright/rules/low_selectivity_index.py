from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.rules.base import is_collscan, severity, usable, with_advice
from indexwright.rules.predicates import negation_only
from indexwright.rules.sort_in_memory import sorts_in_memory

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, Finding, ShapeStats
    from indexwright.rules.base import Indexes

THRESHOLD = 10


def check(stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes) -> list[Finding]:
    plan = usable(explain)
    if (
        plan is None
        or not stats.returned_known
        or stats.meta.output_reduced
        or plan.fetch_filter_fields
    ):
        return []
    if is_collscan(stats, explain) or sorts_in_memory(stats, explain):
        return []
    # Unanchored regexes and negation-only filters have their own rules.
    if stats.meta.unanchored_regex or negation_only(stats.shape.filter):
        return []
    if not plan.has("IXSCAN") or stats.keys_per_returned <= THRESHOLD:
        return []
    used = ", ".join(plan.index_names) or "an index"
    message = (
        f"{used} examines {stats.keys_per_returned:.0f} index keys per document returned, "
        "its key order does not match the query"
    )
    evidence = {
        "keys_per_returned": round(stats.keys_per_returned, 1),
        "indexes_used": list(plan.index_names),
    }
    return with_advice("low_selectivity_index", stats, severity(stats), message, catalog, evidence)
