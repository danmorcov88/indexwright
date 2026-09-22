from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rich.table import Table
from rich.text import Text

from indexwright import __version__
from indexwright.shape import compact

if TYPE_CHECKING:
    from rich.console import RenderableType

    from indexwright.analysis import Analysis
    from indexwright.model import Finding, Recommendation, Shape, ShapeStats

SCHEMA_VERSION = 1
MARKDOWN_ROWS = 30
SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "dim",
}


@dataclass(frozen=True)
class Source:
    kind: str
    since: datetime
    databases: list[str] | None = None
    files: list[str] | None = None


def render_json(analysis: Analysis, source: Source, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    document = {
        "tool_version": __version__,
        "schema_version": SCHEMA_VERSION,
        "generated_at": now.isoformat(),
        "source": {
            "kind": source.kind,
            "since": source.since.isoformat(),
            "databases": source.databases,
            "files": source.files,
        },
        "checks": [
            {"name": c.name, "status": c.status, "detail": c.detail} for c in analysis.checks
        ],
        "summary": {
            "entries_read": analysis.entries_read,
            "shapes": len(analysis.shapes),
            "explains": analysis.explains,
            "findings": len(analysis.findings),
            "offline": analysis.offline,
            "members": {
                "total": analysis.members_total,
                "reached": analysis.members_reached,
                "unreachable": analysis.unreachable,
            },
        },
        "shapes": [_shape_document(s) for s in analysis.shapes],
        "findings": [_finding_document(f) for f in analysis.findings],
        "recommendations": {
            "create_index": [
                {"statement": r.statement, "ns": r.ns, "keys": _keys(r), "shape_ids": ids}
                for r, ids in analysis.create_index_statements()
            ],
            "drop_index": [
                {"statement": r.statement, "ns": r.ns} for r in analysis.drop_index_statements()
            ],
            "rewrite": [
                {"statement": r.statement, "ns": r.ns, "shape_ids": ids}
                for r, ids in analysis.rewrite_statements()
            ],
        },
    }
    return json.dumps(document, indent=2) + "\n"


def _shape_document(stats: ShapeStats) -> dict[str, Any]:
    shape = stats.shape
    return {
        "fingerprint": shape.fingerprint,
        "ns": shape.ns,
        "op": shape.op,
        "shape": compact(shape),
        "count": stats.count,
        "p50_ms": stats.p50_ms,
        "p99_ms": stats.p99_ms,
        "max_ms": stats.max_ms,
        "total_ms": stats.total_ms,
        "docs_examined": stats.docs_examined,
        "keys_examined": stats.keys_examined,
        "nreturned": stats.nreturned,
        "returned_known": stats.returned_known,
        "has_sort_stage": stats.has_sort_stage,
        "plan_summaries": sorted(stats.plan_summaries),
        "meta": {
            "in_size": stats.meta.in_size,
            "unanchored_regex": sorted(stats.meta.unanchored_regex),
            "projection_present": stats.meta.projection_present,
            "output_reduced": stats.meta.output_reduced,
        },
        "first_seen": stats.first_seen.isoformat(),
        "last_seen": stats.last_seen.isoformat(),
    }


def _finding_document(finding: Finding) -> dict[str, Any]:
    rec = finding.recommendation
    return {
        "severity": finding.severity,
        "rule": finding.rule,
        "ns": finding.ns,
        "shape_id": finding.shape_id,
        "message": finding.message,
        "recommendation": None
        if rec is None
        else {"kind": rec.kind, "ns": rec.ns, "statement": rec.statement, "keys": _keys(rec)},
        "evidence": finding.evidence,
    }


def _keys(rec: Recommendation) -> list[list[Any]]:
    return [[field, direction] for field, direction in rec.keys]


def render_markdown(analysis: Analysis, source: Source, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    shapes = {s.shape.fingerprint: s.shape for s in analysis.shapes}
    lines = [
        "# indexwright report",
        "",
        f"Generated {now.strftime('%Y-%m-%d %H:%M UTC')} by indexwright {__version__}, "
        f"from the {source.kind} since {source.since.strftime('%Y-%m-%d %H:%M UTC')}.",
        "",
        f"{analysis.entries_read} entries, {len(analysis.shapes)} shapes, "
        f"{len(analysis.findings)} findings"
        + (", offline (no explain, existing indexes unknown)" if analysis.offline else "")
        + ".",
        "",
    ]
    warnings = [c for c in analysis.checks if c.status != "ok"]
    if warnings:
        lines.append("## Doctor")
        lines.append("")
        lines.extend(f"- {c.status.upper()} {c.name}: {c.detail}" for c in warnings)
        lines.append("")
    lines.append("## Findings")
    lines.append("")
    if not analysis.findings:
        lines.append("No findings.")
    else:
        lines.append("| severity | rule | ns | shape | message |")
        lines.append("|---|---|---|---|---|")
        for finding in analysis.findings[:MARKDOWN_ROWS]:
            lines.append(
                f"| {finding.severity} | {finding.rule} | {finding.ns} | "
                f"{_escape(shape_label(finding, shapes))} | {_escape(finding.message)} |"
            )
        if len(analysis.findings) > MARKDOWN_ROWS:
            lines.append("")
            lines.append(f"and {len(analysis.findings) - MARKDOWN_ROWS} more.")
    lines.append("")
    creates = analysis.create_index_statements()
    if creates:
        lines += ["## Recommended indexes", "", "```js"]
        lines += [rec.statement for rec, _ in creates]
        lines += ["```", ""]
    drops = analysis.drop_index_statements()
    if drops:
        lines += [
            "## Indexes to drop",
            "",
            "Verify usage on every replica set member first.",
            "",
            "```js",
        ]
        lines += [rec.statement for rec in drops]
        lines += ["```", ""]
    rewrites = analysis.rewrite_statements()
    if rewrites:
        lines += ["## Query rewrites", ""]
        for rec, ids in rewrites:
            labels = ", ".join(_label(shapes.get(i)) for i in ids)
            lines.append(f"- {rec.ns} `{labels}`: {rec.statement}")
        lines.append("")
    return "\n".join(lines)


def shape_label(finding: Finding, shapes: dict[str, Shape]) -> str:
    shape = shapes.get(finding.shape_id)
    if shape is not None:
        return _label(shape)
    return f"index {finding.evidence.get('index', '')}".strip()


def _label(shape: Shape | None) -> str:
    return f"{shape.op} {compact(shape)}" if shape else "?"


def _escape(text: str) -> str:
    return text.replace("|", "\\|")


def render_table(analysis: Analysis) -> list[RenderableType]:
    shapes = {s.shape.fingerprint: s.shape for s in analysis.shapes}
    table = Table(title="findings", expand=True)
    table.add_column("severity", no_wrap=True)
    table.add_column("rule", no_wrap=True)
    table.add_column("ns", no_wrap=True)
    table.add_column("shape", ratio=1, overflow="fold")
    table.add_column("message", ratio=2, overflow="fold")
    for f in analysis.findings:
        style = SEVERITY_STYLE[f.severity]
        label = shape_label(f, shapes)
        table.add_row(f"[{style}]{f.severity}[/{style}]", f.rule, f.ns, label, f.message)
    if not analysis.findings:
        table.add_row("", "", "", "", "no findings")
    parts: list[RenderableType] = [table]
    creates = analysis.create_index_statements()
    if creates:
        parts.append(Text("\nRecommended indexes", style="bold"))
        for rec, ids in creates:
            served = ", ".join(i[:10] for i in ids)
            parts.append(Text(f"  {rec.statement}   # shapes {served}"))
    drops = analysis.drop_index_statements()
    if drops:
        parts.append(Text("\nIndexes to drop (verify usage on every member first)", style="bold"))
        parts.extend(Text(f"  {rec.statement}") for rec in drops)
    rewrites = analysis.rewrite_statements()
    if rewrites:
        parts.append(Text("\nQuery rewrites", style="bold"))
        for rec, ids in rewrites:
            labels = ", ".join(_label(shapes.get(i)) for i in ids)
            parts.append(Text(f"  {labels}: {rec.statement}"))
    return parts
