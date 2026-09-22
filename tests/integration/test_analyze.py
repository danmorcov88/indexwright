from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.analysis import analyze
from indexwright.cli import EXIT_FINDINGS, EXIT_OK, app
from indexwright.mongo import Settings, connect
from indexwright.rules.esr import format_keys
from indexwright.shape import compact
from indexwright.source import read_all
from tests.integration.workload import EXPECTED_FINDINGS, EXPECTED_SHAPES

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Mongo

runner = CliRunner()


def test_workload_findings_match_exactly(mongo: Mongo, workload: int) -> None:
    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        entries = read_all(conn, datetime.now(UTC) - timedelta(hours=1), 50_000)
        result = analyze(conn, entries, 200)
    finally:
        conn.close()
    assert result.failed_checks == []
    assert result.entries_read >= workload
    assert {(s.shape.op, compact(s.shape)) for s in result.shapes} == EXPECTED_SHAPES
    assert result.explains == len(EXPECTED_SHAPES)

    labels = {s.shape.fingerprint: (s.shape.op, compact(s.shape)) for s in result.shapes}
    found: dict[tuple[str, str], tuple[str, str]] = {}
    shape_findings = [f for f in result.findings if f.shape_id]
    for finding in shape_findings:
        label = labels[finding.shape_id]
        assert label not in found, f"{label} has more than one finding"
        assert finding.recommendation is not None, f"{label}: {finding.message}"
        rec = finding.recommendation
        found[label] = (finding.rule, format_keys(rec.keys) if rec.keys else rec.kind)
    assert found == EXPECTED_FINDINGS

    for finding in shape_findings:
        assert finding.severity in ("medium", "high"), finding
        assert "example.com" not in str(finding.evidence)

    statements = result.create_index_statements()
    assert len(statements) == 6
    assert sum(rec.ns == "app.customers" for rec, _ in statements) == 1


def test_analyze_command(mongo: Mongo, workload: int) -> None:
    before = mongo.opcounters()
    result = runner.invoke(app, ["analyze", "--uri", mongo.ro_uri, "--db", "app"])
    actionable = " high " in result.output or " critical " in result.output
    assert result.exit_code == (EXIT_FINDINGS if actionable else EXIT_OK), result.output
    assert "Recommended indexes" in result.output
    assert "db.orders.createIndex({email: 1}" in result.output
    assert "Indexes to drop" in result.output
    assert 'db.orders.dropIndex("created_1")' in result.output
    assert "analyze:" in result.output and "20 shapes, 20 explains" in result.output
    assert mongo.opcounters() == before


def test_analyze_json_report_to_file(mongo: Mongo, workload: int, tmp_path: Path) -> None:
    before = mongo.opcounters()
    out = tmp_path / "report.json"
    args = ["analyze", "--uri", mongo.ro_uri, "--db", "app", "--format", "json", "--out", str(out)]
    result = runner.invoke(app, args)
    assert result.exit_code in (EXIT_OK, EXIT_FINDINGS), result.output
    assert "report written to" in result.output and "createIndex" not in result.output
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert doc["schema_version"] == 1
    assert doc["summary"]["shapes"] == len(EXPECTED_SHAPES)
    assert doc["source"] == {
        "kind": "profiler",
        "since": doc["source"]["since"],
        "databases": ["app"],
        "files": None,
    }
    assert {(s["op"], s["shape"]) for s in doc["shapes"]} == EXPECTED_SHAPES
    assert "example.com" not in out.read_text(encoding="utf-8")
    assert mongo.opcounters() == before


def test_analyze_markdown_report(mongo: Mongo, workload: int) -> None:
    result = runner.invoke(app, ["analyze", "--uri", mongo.ro_uri, "--db", "app", "--format", "md"])
    assert result.exit_code in (EXIT_OK, EXIT_FINDINGS), result.output
    assert "# indexwright report" in result.output
    assert "## Recommended indexes" in result.output
    assert "db.customers.createIndex({email: 1}" in result.output
    assert "## Query rewrites" in result.output
