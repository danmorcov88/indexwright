from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.analysis import analyze
from indexwright.cli import EXIT_FINDINGS, EXIT_OK, app
from indexwright.mongo import Settings, connect
from indexwright.rules.esr import format_keys
from indexwright.shape import compact
from tests.integration.workload import EXPECTED_FINDINGS, EXPECTED_SHAPES

if TYPE_CHECKING:
    from tests.integration.conftest import Mongo

runner = CliRunner()


def test_workload_findings_match_exactly(mongo: Mongo, workload: int) -> None:
    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        result = analyze(conn, datetime.now(UTC) - timedelta(hours=1), 50_000, 200)
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
        found[label] = (finding.rule, format_keys(finding.recommendation.keys))
    assert found == EXPECTED_FINDINGS

    for finding in shape_findings:
        assert finding.severity in ("medium", "high"), finding
        assert "example.com" not in str(finding.evidence)

    statements = result.create_index_statements()
    assert len(statements) == 5
    assert all(rec.statement.startswith("db.orders.createIndex(") for rec, _ in statements)


def test_analyze_command(mongo: Mongo, workload: int) -> None:
    before = mongo.opcounters()
    result = runner.invoke(app, ["analyze", "--uri", mongo.ro_uri, "--db", "app"])
    actionable = " high " in result.output or " critical " in result.output
    assert result.exit_code == (EXIT_FINDINGS if actionable else EXIT_OK), result.output
    assert "Recommended indexes" in result.output
    assert "db.orders.createIndex({email: 1}" in result.output
    assert "Indexes to drop" in result.output
    assert 'db.orders.dropIndex("created_1")' in result.output
    assert "analyze:" in result.output and "19 shapes, 19 explains" in result.output
    assert mongo.opcounters() == before
