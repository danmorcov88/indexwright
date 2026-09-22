from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.aggregate import group
from indexwright.cli import EXIT_OK, app
from indexwright.mongo import Settings, connect
from indexwright.shape import compact
from indexwright.source import read_all
from tests.integration.workload import EXPECTED_SHAPES

if TYPE_CHECKING:
    from tests.integration.conftest import Mongo

runner = CliRunner()


def test_workload_groups_into_expected_shapes(mongo: Mongo, workload: int) -> None:
    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        stats = group(read_all(conn, datetime.now(UTC) - timedelta(hours=1), 50_000))
    finally:
        conn.close()
    found = {(s.shape.op, compact(s.shape)) for s in stats if s.shape.ns == "app.orders"}
    assert found == EXPECTED_SHAPES
    assert sum(s.count for s in stats) == workload

    by_shape = {(s.shape.op, compact(s.shape)): s for s in stats}
    equality = by_shape[("find", "{status:?}")]
    assert equality.count == 70, "getMore batches must not count as executions"
    assert equality.nreturned > 70 * 101, "getMore batches must add to returned documents"
    assert "COLLSCAN" in equality.plan_summaries
    assert by_shape[("find", "{customer_id:{$in:[?]}}")].meta.in_size == 250
    assert by_shape[("find", "{email:{$regex:?}}")].meta.unanchored_regex == frozenset({"email"})
    assert (
        by_shape[("find", "{_id:?}")].plan_summaries == frozenset({"IDHACK"})
        or by_shape[("find", "{_id:?}")].docs_per_returned <= 1
    )


def test_shapes_command(mongo: Mongo, workload: int) -> None:
    before = mongo.opcounters()
    result = runner.invoke(app, ["shapes", "--uri", mongo.ro_uri, "--db", "app"])
    assert result.exit_code == EXIT_OK, result.output
    assert "query shapes" in result.output
    assert "shapes: " in result.output and f"{len(EXPECTED_SHAPES)} shapes" in result.output
    assert mongo.opcounters() == before


def test_shapes_rejects_bad_since(mongo: Mongo) -> None:
    result = runner.invoke(app, ["shapes", "--uri", mongo.ro_uri, "--since", "yesterday"])
    assert result.exit_code == 2
    assert "24h" in result.output
