from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.analysis import analyze
from indexwright.cli import EXIT_OK, app
from indexwright.indexes import inventory
from indexwright.mongo import Settings, connect
from indexwright.source import read_all
from tests.integration.workload import EXPECTED_INDEX_FINDINGS

if TYPE_CHECKING:
    from tests.integration.conftest import Mongo

runner = CliRunner()


def test_index_findings_match_exactly(mongo: Mongo, workload: int) -> None:
    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        entries = read_all(conn, datetime.now(UTC) - timedelta(hours=1), 50_000)
        result = analyze(conn, entries, 200, unused_days=0)
    finally:
        conn.close()
    index_findings = [f for f in result.findings if not f.shape_id]
    by_index = {
        f"{f.ns}.{f.evidence['index']}": f.rule for f in index_findings if "index" in f.evidence
    }
    assert by_index == EXPECTED_INDEX_FINDINGS
    assert "_id_" not in {f.evidence.get("index") for f in index_findings}
    too_many = [f for f in index_findings if f.rule == "too_many_indexes"]
    assert [f.ns for f in too_many] == ["app.customers"]
    assert (result.members_total, result.members_reached, result.unreachable) == (1, 1, [])
    drops = {rec.statement for rec in result.drop_index_statements()}
    assert 'db.orders.dropIndex("created_1")' in drops
    assert 'db.orders.dropIndex("tags_1")' in drops


def test_default_unused_days_hides_fresh_indexes(mongo: Mongo, workload: int) -> None:
    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        result = inventory(conn, unused_days=30)
    finally:
        conn.close()
    orders = next(c for c in result.collections if c.ns == "app.orders")
    assert orders.usage is not None
    assert orders.usage["tags_1"].ops == 0
    assert orders.usage["status_1"].ops > 0
    assert orders.usage["_id_"].members_reported == 1


def test_indexes_command(mongo: Mongo, workload: int) -> None:
    before = mongo.opcounters()
    result = runner.invoke(app, ["indexes", "--uri", mongo.ro_uri, "--db", "app"])
    assert result.exit_code == EXIT_OK, result.output
    assert "email_1" in result.output and "created_1_email_1" in result.output
    assert "partial" in result.output
    assert "indexes: " in result.output and "2 collections" in result.output
    assert mongo.opcounters() == before
