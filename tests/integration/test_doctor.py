from __future__ import annotations

from typing import TYPE_CHECKING

from typer.testing import CliRunner

from indexwright.cli import EXIT_OK, EXIT_USAGE, EXIT_WRITE_ACCESS, app

if TYPE_CHECKING:
    from tests.integration.conftest import Mongo

runner = CliRunner()


def test_doctor_passes_for_read_only_user(mongo: Mongo) -> None:
    result = runner.invoke(app, ["doctor", "--uri", mongo.ro_uri])
    assert result.exit_code == EXIT_OK, result.output
    assert "FAIL" not in result.output
    assert "server version" in result.output
    assert "replica set" in result.output or "standalone" in result.output
    assert "profiler app" in result.output
    assert "$indexStats" in result.output


def test_doctor_refuses_read_write_user(mongo: Mongo) -> None:
    result = runner.invoke(app, ["doctor", "--uri", mongo.rw_uri])
    assert result.exit_code == EXIT_WRITE_ACCESS
    assert "refusing to continue" in result.output
    assert "insert on app.*" in result.output


def test_doctor_masks_password_on_auth_failure(mongo: Mongo) -> None:
    uri = mongo.uri("advisor", "wrong-pass")
    result = runner.invoke(app, ["doctor", "--uri", uri])
    assert result.exit_code == EXIT_USAGE
    assert "authentication failed" in result.output
    assert "wrong-pass" not in result.output


def test_doctor_reads_uri_from_env(mongo: Mongo) -> None:
    result = runner.invoke(app, ["doctor"], env={"MONGODB_URI": mongo.ro_uri})
    assert result.exit_code == EXIT_OK, result.output


def test_doctor_never_writes(mongo: Mongo) -> None:
    before = mongo.opcounters()
    runner.invoke(app, ["doctor", "--uri", mongo.ro_uri])
    assert mongo.opcounters() == before
