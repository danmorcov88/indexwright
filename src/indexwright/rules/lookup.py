from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.model import Finding
from indexwright.rules.base import severity
from indexwright.rules.esr import recommendation

if TYPE_CHECKING:
    from indexwright.model import ExplainResult, ShapeStats
    from indexwright.rules.base import Indexes


def lookup_no_index(
    stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes
) -> list[Finding]:
    shape = stats.shape
    if not shape.pipeline or not catalog.known:
        return []
    db = shape.ns.split(".", 1)[0]
    findings = []
    for stage in shape.pipeline:
        body = stage.get("$lookup")
        if not isinstance(body, dict):
            continue
        source, field = body.get("from"), body.get("foreignField")
        if not isinstance(source, str) or not isinstance(field, str) or field == "_id":
            continue
        foreign_ns = f"{db}.{source}"
        indexed = any(
            index.keys and index.keys[0][0] == field and not index.partial and not index.hidden
            for index in catalog.for_ns(foreign_ns)
        )
        if indexed:
            continue
        message = (
            f"$lookup joins {source} on {field}, which has no index, so every input document "
            f"scans {source}"
        )
        findings.append(
            Finding(
                severity(stats),
                "lookup_no_index",
                shape.ns,
                shape.fingerprint,
                message,
                recommendation(foreign_ns, ((field, 1),)),
                {"from": source, "foreignField": field},
            )
        )
    return findings
