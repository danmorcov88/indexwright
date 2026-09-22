from __future__ import annotations

import gzip
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.analysis import Analysis, analyze
from indexwright.cli import app
from indexwright.mongo import Settings, connect
from indexwright.shape import compact
from indexwright.source import read_all, read_log

if TYPE_CHECKING:
    from pathlib import Path

    from tests.integration.conftest import Mongo

runner = CliRunner()


def _summary(result: Analysis) -> set[tuple[str, str, int, str, str]]:
    shapes = {s.shape.fingerprint: s for s in result.shapes}
    rows = set()
    for f in result.findings:
        if not f.shape_id:
            continue
        stats = shapes[f.shape_id]
        rec = f.recommendation.statement if f.recommendation else ""
        rows.add((stats.shape.op, compact(stats.shape), stats.count, f.rule, rec))
    return rows


def _shapes(result: Analysis) -> dict[tuple[str, str], tuple[int, int, int]]:
    return {
        (s.shape.op, compact(s.shape)): (s.count, s.docs_examined, s.nreturned)
        for s in result.shapes
    }


def test_log_file_gives_the_same_findings_as_the_profiler(
    mongo: Mongo, workload: int, tmp_path: Path
) -> None:
    stdout, _ = mongo.container.get_logs()
    plain = tmp_path / "mongod.log"
    plain.write_bytes(stdout)
    packed = tmp_path / "mongod.log.1.gz"
    with gzip.open(packed, "wb") as handle:
        handle.write(stdout)
    since = datetime.now(UTC) - timedelta(hours=1)

    conn = connect(Settings(uri=mongo.ro_uri, db="app"))
    try:
        from_profiler = analyze(conn, read_all(conn, since, 50_000), 200)
        from_log = analyze(conn, read_log([str(packed)], since, 50_000, "app"), 200)
    finally:
        conn.close()
    assert _shapes(from_log) == _shapes(from_profiler)
    assert _summary(from_log) == _summary(from_profiler)
    assert from_log.entries_read == from_profiler.entries_read

    offline = analyze(None, read_log([str(plain)], since, 50_000, "app"), 200)
    assert offline.offline and offline.explains == 0 and offline.checks == []
    assert _shapes(offline) == _shapes(from_profiler)
    assert {f.rule for f in offline.findings} >= {"collscan", "sort_in_memory", "large_in"}
    assert not any(f.rule == "lookup_no_index" for f in offline.findings)


def test_analyze_command_from_log_without_uri(mongo: Mongo, workload: int, tmp_path: Path) -> None:
    stdout, _ = mongo.container.get_logs()
    log = tmp_path / "mongod.log"
    log.write_bytes(stdout)
    result = runner.invoke(
        app, ["analyze", "--log", str(log), "--db", "app"], env={"MONGODB_URI": ""}
    )
    assert result.exit_code in (0, 1), result.output
    assert "offline: no explain" in result.output
    assert "collscan" in result.output and "20 shapes, 0 explains" in result.output


def test_shapes_command_from_log(mongo: Mongo, workload: int, tmp_path: Path) -> None:
    log = tmp_path / "mongod.log"
    log.write_bytes(mongo.container.get_logs()[0])
    result = runner.invoke(
        app, ["shapes", "--log", str(log), "--db", "app"], env={"MONGODB_URI": ""}
    )
    assert result.exit_code == 0, result.output
    assert "20 shapes" in result.output


def test_missing_log_file_is_a_usage_error(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["analyze", "--log", str(tmp_path / "nope.log")], env={"MONGODB_URI": ""}
    )
    assert result.exit_code == 2
    assert "nope.log" in result.output
