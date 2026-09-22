from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from indexwright.aggregate import group
from indexwright.doctor import run_checks
from indexwright.explain import Explainer
from indexwright.indexes import IndexCatalog, inventory
from indexwright.rules import run_index_rules, run_rules, sort_findings
from indexwright.rules.base import NoIndexes

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from indexwright.doctor import Check
    from indexwright.model import Entry, Finding, Recommendation, ShapeStats
    from indexwright.mongo import Connection


@dataclass
class Analysis:
    checks: list[Check]
    shapes: list[ShapeStats] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    entries_read: int = 0
    explains: int = 0
    members_total: int = 1
    members_reached: int = 1
    unreachable: list[str] = field(default_factory=list)
    offline: bool = False

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def actionable(self) -> bool:
        return any(f.severity in ("critical", "high") for f in self.findings)

    def create_index_statements(self) -> list[tuple[Recommendation, list[str]]]:
        grouped: dict[str, tuple[Recommendation, list[str]]] = {}
        for finding in self.findings:
            rec = finding.recommendation
            if rec is None or rec.kind != "create_index":
                continue
            grouped.setdefault(rec.statement, (rec, []))[1].append(finding.shape_id)
        return list(grouped.values())

    def drop_index_statements(self) -> list[Recommendation]:
        seen: dict[str, Recommendation] = {}
        for finding in self.findings:
            rec = finding.recommendation
            if rec is not None and rec.kind == "drop_index":
                seen.setdefault(rec.statement, rec)
        return list(seen.values())


def analyze(
    conn: Connection | None, entries: Iterable[Entry], max_explains: int, unused_days: int = 30
) -> Analysis:
    analysis = Analysis(checks=run_checks(conn) if conn else [], offline=conn is None)
    if analysis.failed_checks:
        return analysis
    analysis.shapes = group(_count(entries, analysis))
    findings: list[Finding] = []
    if conn is None:
        for stats in analysis.shapes:
            findings.extend(run_rules(stats, None, NoIndexes()))
    else:
        explainer = Explainer(conn, max_total=max_explains)
        catalog = IndexCatalog(conn)
        for stats in analysis.shapes:
            findings.extend(run_rules(stats, explainer.explain(stats), catalog))
        analysis.explains = explainer.executed
        indexes = inventory(conn, unused_days)
        for coll in indexes.collections:
            findings.extend(run_index_rules(coll))
        analysis.members_total = indexes.report.members_total
        analysis.members_reached = indexes.report.members_reached
        analysis.unreachable = indexes.report.unreachable
    weight = {s.shape.fingerprint: s.total_ms for s in analysis.shapes}
    analysis.findings = sort_findings(findings, weight)
    return analysis


def _count(entries: Iterable[Entry], analysis: Analysis) -> Iterator[Entry]:
    for entry in entries:
        analysis.entries_read += 1
        yield entry
