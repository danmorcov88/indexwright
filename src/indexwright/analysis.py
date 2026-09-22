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
        grouped = self._grouped("create_index")
        kept: list[tuple[Recommendation, list[str]]] = []
        for rec, ids in grouped:
            wider = next((w for w, _ in grouped if w is not rec and _is_prefix(rec, w)), None)
            if wider is None:
                kept.append((rec, ids))
                continue
            target = next(entry for entry in grouped if entry[0] is wider)
            target[1].extend(i for i in ids if i not in target[1])
        return [(rec, ids) for rec, ids in grouped if any(k is rec for k, _ in kept)]

    def drop_index_statements(self) -> list[Recommendation]:
        return [rec for rec, _ in self._grouped("drop_index")]

    def rewrite_statements(self) -> list[tuple[Recommendation, list[str]]]:
        return self._grouped("rewrite")

    def _grouped(self, kind: str) -> list[tuple[Recommendation, list[str]]]:
        grouped: dict[tuple[str, str], tuple[Recommendation, list[str]]] = {}
        for finding in self.findings:
            rec = finding.recommendation
            if rec is None or rec.kind != kind:
                continue
            key = (rec.ns, rec.statement)
            grouped.setdefault(key, (rec, []))[1].append(finding.shape_id)
        return list(grouped.values())


def _is_prefix(short: Recommendation, long: Recommendation) -> bool:
    if short.ns != long.ns or len(long.keys) <= len(short.keys):
        return False
    prefix = long.keys[: len(short.keys)]
    if prefix == short.keys:
        return True
    return len(short.keys) == 1 and prefix[0][0] == short.keys[0][0]


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
