from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import (
    AutoReconnect,
    NetworkTimeout,
    NotPrimaryError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from indexwright import mongo
from indexwright.mongo import Connection, Settings, mask_uri, retry


@pytest.mark.parametrize(
    ("uri", "expected"),
    [
        ("mongodb://alice:s3cret@host:27017/app", "mongodb://alice:***@host:27017/app"),
        (
            "mongodb+srv://alice:p%40ss@cluster.example.net/",
            "mongodb+srv://alice:***@cluster.example.net/",
        ),
        ("mongodb://host:27017", "mongodb://host:27017"),
        ("mongodb://alice@host:27017", "mongodb://alice@host:27017"),
    ],
)
def test_mask_uri(uri: str, expected: str) -> None:
    assert mask_uri(uri) == expected


def test_mask_uri_inside_error_text_leaves_other_words_alone() -> None:
    text = "cannot reach mongodb://wr:wr@host/app: write access to wr failed"
    assert mask_uri(text) == "cannot reach mongodb://wr:***@host/app: write access to wr failed"


def test_retry_recovers_from_transient_errors() -> None:
    calls: list[int] = []
    delays: list[float] = []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise NetworkTimeout("boom")
        return "ok"

    assert retry(flaky, sleep=delays.append) == "ok"
    assert len(calls) == 3
    assert len(delays) == 2
    assert 0.5 <= delays[0] < 0.7 and 1.0 <= delays[1] < 1.3


def test_retry_gives_up_after_attempts() -> None:
    def always_failing() -> None:
        raise NotPrimaryError("stepping down")

    with pytest.raises(AutoReconnect):
        retry(always_failing, sleep=lambda _: None)


def test_retry_does_not_retry_server_selection_timeout() -> None:
    calls: list[int] = []

    def no_server() -> None:
        calls.append(1)
        raise ServerSelectionTimeoutError("no primary")

    with pytest.raises(ServerSelectionTimeoutError):
        retry(no_server, sleep=lambda _: None)
    assert len(calls) == 1


def test_retry_does_not_retry_operation_failure() -> None:
    calls: list[int] = []

    def unauthorized() -> None:
        calls.append(1)
        raise OperationFailure("not authorized", code=13)

    with pytest.raises(OperationFailure):
        retry(unauthorized, sleep=lambda _: None)
    assert len(calls) == 1


class FakeDatabase:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.responses = responses
        self.commands: list[dict[str, Any]] = []

    def command(self, spec: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(spec)
        return self.responses[next(iter(spec))]


class FakeClient:
    def __init__(self, responses: dict[str, dict[str, Any]]) -> None:
        self.db = FakeDatabase(responses)

    def __getitem__(self, name: str) -> FakeDatabase:
        return self.db


def _conn(responses: dict[str, dict[str, Any]], db: str | None = None) -> Connection:
    client: Any = FakeClient(responses)
    return Connection(client, Settings(uri="mongodb://h", timeout=2.5, db=db))


def test_command_injects_max_time_ms() -> None:
    conn = _conn({"ping": {"ok": 1}})
    conn.command("admin", {"ping": 1})
    assert conn.client.db.commands == [{"ping": 1, "maxTimeMS": 2500}]


def test_server_version_accepts_5_and_newer() -> None:
    conn = _conn({"buildInfo": {"version": "7.0.12", "versionArray": [7, 0, 12, 0]}})
    assert mongo.server_version(conn) == (7, 0, 12)


def test_server_version_rejects_old_servers() -> None:
    conn = _conn({"buildInfo": {"version": "4.4.29", "versionArray": [4, 4, 29, 0]}})
    with pytest.raises(mongo.UnsupportedVersionError, match=r"4\.4\.29"):
        mongo.server_version(conn)


def _connection_status(
    roles: list[tuple[str, str]], privileges: list[tuple[dict[str, Any], list[str]]]
) -> dict[str, Any]:
    return {
        "authInfo": {
            "authenticatedUsers": [{"user": "u", "db": "admin"}] if roles else [],
            "authenticatedUserRoles": [{"role": r, "db": d} for r, d in roles],
            "authenticatedUserPrivileges": [
                {"resource": res, "actions": actions} for res, actions in privileges
            ],
        },
        "ok": 1,
    }


READ_ONLY: list[tuple[dict[str, Any], list[str]]] = [
    ({"db": "app", "collection": ""}, ["find", "listCollections", "listIndexes", "collStats"]),
    ({"cluster": True}, ["serverStatus", "listDatabases", "inprog"]),
    ({"db": "", "collection": ""}, ["indexStats", "collStats"]),
]


def test_privileges_read_only_user_passes() -> None:
    status = _connection_status([("read", "app"), ("clusterMonitor", "admin")], READ_ONLY)
    conn = _conn({"connectionStatus": status})
    result = mongo.check_privileges(conn)
    assert result.authenticated
    assert result.write_grants == ()
    assert result.roles == ("read@app", "clusterMonitor@admin")


def test_privileges_read_write_user_is_refused() -> None:
    privileges = [
        *READ_ONLY,
        ({"db": "app", "collection": ""}, ["find", "insert", "update", "remove", "createIndex"]),
    ]
    status = _connection_status([("readWrite", "app")], privileges)
    conn = _conn({"connectionStatus": status})
    result = mongo.check_privileges(conn)
    assert result.write_grants == (
        "createIndex on app.*",
        "insert on app.*",
        "remove on app.*",
        "update on app.*",
    )


def test_privileges_write_on_other_db_is_fine_when_db_is_targeted() -> None:
    privileges = [*READ_ONLY, ({"db": "other", "collection": ""}, ["insert"])]
    status = _connection_status([("readWrite", "other")], privileges)
    assert mongo.check_privileges(_conn({"connectionStatus": status}, "app")).write_grants == ()
    assert mongo.check_privileges(_conn({"connectionStatus": status})).write_grants == (
        "insert on other.*",
    )


def test_privileges_root_is_refused() -> None:
    status = _connection_status([("root", "admin")], [({"anyResource": True}, ["anyAction"])])
    conn = _conn({"connectionStatus": status}, "app")
    result = mongo.check_privileges(conn)
    assert result.write_grants == ("anyAction on anyResource",)


def test_privileges_unauthenticated_connection() -> None:
    conn = _conn({"connectionStatus": _connection_status([], [])})
    result = mongo.check_privileges(conn)
    assert not result.authenticated
    assert result.write_grants == ()


@pytest.mark.parametrize(
    ("hello", "kind", "name", "members"),
    [
        ({"ok": 1}, "standalone", None, 0),
        ({"setName": "rs0", "hosts": ["a:27017", "b:27017"]}, "replica set", "rs0", 2),
        ({"msg": "isdbgrid"}, "sharded", None, 0),
    ],
)
def test_topology(hello: dict[str, Any], kind: str, name: str | None, members: int) -> None:
    conn = _conn({"hello": hello})
    topo = mongo.topology(conn)
    assert (topo.kind, topo.name, len(topo.hosts)) == (kind, name, members)
