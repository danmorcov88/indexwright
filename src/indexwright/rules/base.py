from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from indexwright.model import Finding
from indexwright.rules import esr

if TYPE_CHECKING:
    from collections.abc import Callable

    from indexwright.model import ExplainResult, IndexInfo, Severity, ShapeStats

    Rule = Callable[[ShapeStats, ExplainResult | None, "Indexes"], list[Finding]]


class Indexes(Protocol):
    known: bool

    def for_ns(self, ns: str) -> list[IndexInfo]: ...


class NoIndexes:
    known = False

    def for_ns(self, ns: str) -> list[IndexInfo]:
        return []


def severity(stats: ShapeStats) -> Severity:
    if stats.p99_ms > 1000 and stats.count > 100:
        return "critical"
    if stats.p99_ms > 100 or stats.count > 1000:
        return "high"
    return "medium"


def usable(explain: ExplainResult | None) -> ExplainResult | None:
    return explain if explain is not None and explain.error is None else None


def is_collscan(stats: ShapeStats, explain: ExplainResult | None) -> bool:
    plan = usable(explain)
    if plan is not None:
        return plan.has("COLLSCAN")
    return any("COLLSCAN" in summary for summary in stats.plan_summaries)


def with_advice(
    rule: str,
    stats: ShapeStats,
    level: Severity,
    message: str,
    catalog: Indexes,
    evidence: dict[str, Any],
) -> list[Finding]:
    shape = stats.shape
    advice = esr.advise(shape, catalog.for_ns(shape.ns), stats.meta.unanchored_regex)
    evidence = {**evidence, "count": stats.count, "p99_ms": stats.p99_ms}
    if advice.recommendations:
        return [
            Finding(level, rule, shape.ns, shape.fingerprint, message, rec, evidence)
            for rec in advice.recommendations
        ]
    note = "; ".join(advice.covered) or "no indexable predicate to build an index from"
    return [Finding(level, rule, shape.ns, shape.fingerprint, f"{message}; {note}", None, evidence)]
