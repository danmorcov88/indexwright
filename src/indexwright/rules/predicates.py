from __future__ import annotations

from typing import TYPE_CHECKING, Any

from indexwright.model import Finding, Recommendation
from indexwright.rules.base import severity

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, ShapeStats
    from indexwright.rules.base import Indexes

NEGATIONS = frozenset({"$ne", "$nin", "$not"})
LARGE_IN = 200


def negation_only(filter_: dict[str, Any]) -> bool:
    if not filter_ or any(key.startswith("$") for key in filter_):
        return False
    return all(_is_negation(value) for value in filter_.values())


def _is_negation(value: Any) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    operators = set(value)
    if operators == {"$exists"}:
        return value["$exists"] is False
    return operators <= NEGATIONS


def unanchored_regex(
    stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes
) -> list[Finding]:
    fields = sorted(stats.meta.unanchored_regex)
    if not fields:
        return []
    shape = stats.shape
    names = ", ".join(fields)
    message = (
        f"$regex on {names} is not anchored with ^, no index can narrow it: every key or "
        "every document is scanned"
    )
    advice = f"anchor the pattern on {names} with ^, or use a text index for free-text search"
    recommendation = Recommendation("rewrite", shape.ns, advice)
    evidence = {"fields": fields, "docs_per_returned": round(stats.docs_per_returned, 1)}
    return [
        Finding(
            severity(stats),
            "unanchored_regex",
            shape.ns,
            shape.fingerprint,
            message,
            recommendation,
            evidence,
        )
    ]


def negation_predicate(
    stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes
) -> list[Finding]:
    shape = stats.shape
    if not negation_only(shape.filter):
        return []
    fields = ", ".join(sorted(shape.filter))
    message = f"the filter on {fields} only excludes values, an index cannot narrow a negation"
    advice = (
        "add a positive predicate (equality or range), or model the state so the common "
        "case is an equality"
    )
    recommendation = Recommendation("rewrite", shape.ns, advice)
    evidence = {
        "fields": sorted(shape.filter),
        "docs_per_returned": round(stats.docs_per_returned, 1),
    }
    return [
        Finding(
            severity(stats),
            "negation_predicate",
            shape.ns,
            shape.fingerprint,
            message,
            recommendation,
            evidence,
        )
    ]


def large_in(stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes) -> list[Finding]:
    size = stats.meta.in_size
    if size <= LARGE_IN:
        return []
    shape = stats.shape
    message = f"$in with up to {size} values, one index bound per value"
    advice = "split the list into batches, or store the relationship on the other side"
    recommendation = Recommendation("rewrite", shape.ns, advice)
    level = "medium" if severity(stats) in ("critical", "high") else severity(stats)
    return [
        Finding(
            level,
            "large_in",
            shape.ns,
            shape.fingerprint,
            message,
            recommendation,
            {"in_size": size},
        )
    ]
