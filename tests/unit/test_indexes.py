from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pymongo.errors import OperationFailure

from indexwright.indexes import (
    IndexCatalog,
    collect_usage,
    index_info,
    list_indexes,
    member_uri,
    members,
    write_share,
)
from indexwright.model import IndexUsage
from indexwright.mongo import ConnectError, Settings


def test_index_info_maps_spec_fields() -> None:
    spec = {
        "v": 2,
        "key": {"status": 1, "created": -1, "loc": "2dsphere"},
        "name": "status_1_created_-1_loc_2dsphere",
        "unique": True,
        "partialFilterExpression": {"status": "new"},
        "hidden": True,
        "collation": {"locale": "ro", "strength": 2},
    }
    info = index_info("app.orders", spec)
    assert info.keys == (("status", 1), ("created", -1), ("loc", "2dsphere"))
    assert info.name == "status_1_created_-1_loc_2dsphere"
    assert info.unique and info.partial and info.hidden and not info.sparse and not info.ttl
    assert info.partial_filter == '{"status": "new"}' and info.collation == '"ro"'
    plain = index_info("app.orders", {"key": {"a": 1}, "name": "a_1", "expireAfterSeconds": 60})
    assert plain.ttl and plain.partial_filter is None and plain.collation is None


class FakeConn:
    def __init__(self, responses: list[dict[str, Any] | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def command(self, db: str, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(spec)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _batch(specs: list[dict[str, Any]], cursor_id: int, key: str) -> dict[str, Any]:
    return {"cursor": {"id": cursor_id, key: specs}}


def test_list_indexes_follows_the_cursor() -> None:
    conn: Any = FakeConn(
        [
            _batch([{"key": {"_id": 1}, "name": "_id_"}], 42, "firstBatch"),
            _batch([{"key": {"a": 1}, "name": "a_1"}], 0, "nextBatch"),
        ]
    )
    names = [i.name for i in list_indexes(conn, "app.orders")]
    assert names == ["_id_", "a_1"]
    assert conn.calls[0] == {"listIndexes": "orders"}
    assert conn.calls[1] == {"getMore": 42, "collection": "orders"}


def test_list_indexes_of_missing_collection_is_empty() -> None:
    conn: Any = FakeConn([OperationFailure("ns not found", code=26)])
    assert list_indexes(conn, "app.gone") == []
    conn = FakeConn([OperationFailure("unauthorized", code=13)])
    with pytest.raises(OperationFailure):
        list_indexes(conn, "app.orders")


def test_catalog_caches_per_namespace() -> None:
    conn: Any = FakeConn([_batch([{"key": {"_id": 1}, "name": "_id_"}], 0, "firstBatch")])
    catalog = IndexCatalog(conn)
    first = catalog.for_ns("app.orders")
    assert catalog.for_ns("app.orders") is first
    assert len(conn.calls) == 1


@pytest.mark.parametrize(
    ("uri", "host", "expected"),
    [
        (
            "mongodb://u:p%40ss@a:27017,b:27017/app?replicaSet=rs0&authSource=admin",
            "b:27017",
            "mongodb://u:p%40ss@b:27017/app?authSource=admin&directConnection=true",
        ),
        ("mongodb://a:27017", "a:27017", "mongodb://a:27017/?directConnection=true"),
        (
            "mongodb+srv://u:p@cluster.example.net/app?retryWrites=false",
            "shard-00-01.example.net:27017",
            "mongodb://u:p@shard-00-01.example.net:27017/app?retryWrites=false&tls=true"
            "&directConnection=true",
        ),
        (
            "mongodb+srv://u:p@cluster.example.net/?tls=false",
            "h:27017",
            "mongodb://u:p@h:27017/?tls=false&directConnection=true",
        ),
        (
            "mongodb://a:27017/?directConnection=true&replicaSet=rs0",
            "c:27017",
            "mongodb://c:27017/?directConnection=true",
        ),
    ],
)
def test_member_uri(uri: str, host: str, expected: str) -> None:
    assert member_uri(uri, host) == expected


def test_member_uri_rejects_garbage() -> None:
    with pytest.raises(ConnectError):
        member_uri("not a uri", "h:27017")


def test_members_from_hello() -> None:
    assert members({"isWritablePrimary": True}) == []
    assert members({"msg": "isdbgrid"}) == []
    hello = {"setName": "rs0", "hosts": ["a:27017", "b:27017"], "passives": ["c:27017"]}
    assert members(hello) == ["a:27017", "b:27017", "c:27017"]


def test_write_share_from_top() -> None:
    conn: Any = FakeConn(
        [
            {
                "totals": {
                    "note": "x",
                    "app.orders": {
                        "total": {"count": 10},
                        "insert": {"count": 1},
                        "update": {"count": 2},
                        "remove": {"count": 0},
                    },
                    "app.empty": {"total": {"count": 0}},
                }
            }
        ]
    )
    assert write_share(conn) == {"app.orders": 0.3}
    conn = FakeConn([OperationFailure("no such command", code=59)])
    assert write_share(conn) is None


class FakeCollection:
    def __init__(self, docs: list[dict[str, Any]] | Exception) -> None:
        self.docs = docs

    def aggregate(self, pipeline: list[dict[str, Any]], **kwargs: Any) -> list[dict[str, Any]]:
        assert pipeline == [{"$indexStats": {}}] and "maxTimeMS" in kwargs
        if isinstance(self.docs, Exception):
            raise self.docs
        return self.docs


class FakeMember:
    def __init__(self, hello: dict[str, Any], stats: dict[str, Any]) -> None:
        self.hello = hello
        self.stats = stats
        self.settings = Settings("mongodb://u:p@a:27017,b:27017,c:27017/?replicaSet=rs0")
        self.max_time_ms = 5000
        self.closed = False

    def command(self, db: str, spec: dict[str, Any]) -> dict[str, Any]:
        assert (db, spec) == ("admin", {"hello": 1})
        return self.hello

    @property
    def client(self) -> Any:
        stats = self.stats

        class Db:
            def __getitem__(self, coll: str) -> FakeCollection:
                return FakeCollection(stats.get(coll, []))

        class Client:
            def __getitem__(self, db: str) -> Db:
                return Db()

        return Client()

    def close(self) -> None:
        self.closed = True


def _doc(name: str, ops: int, since: datetime, host: str, **extra: Any) -> dict[str, Any]:
    return {
        "name": name,
        "key": {name: 1},
        "host": host,
        "accesses": {"ops": ops, "since": since},
        **extra,
    }


RS_HELLO = {"setName": "rs0", "me": "a:27017", "hosts": ["a:27017", "b:27017", "c:27017"]}
T1 = datetime(2026, 8, 1, tzinfo=UTC)
T2 = datetime(2026, 9, 1, tzinfo=UTC)


def test_collect_usage_sums_members_and_reports_unreachable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = FakeMember(
        RS_HELLO, {"orders": [_doc("x_1", 5, T2, "a:27017"), _doc("y_1", 0, T2, "a:27017")]}
    )
    secondary = FakeMember(
        RS_HELLO, {"orders": [_doc("x_1", 7, T1, "b:27017"), _doc("y_1", 0, T1, "b:27017")]}
    )
    dialed: list[str] = []

    def fake_connect(settings: Settings) -> FakeMember:
        dialed.append(settings.uri)
        if "c:27017" in settings.uri:
            raise ConnectError("down")
        return secondary

    monkeypatch.setattr("indexwright.indexes.connect", fake_connect)
    report = collect_usage(primary, ["app.orders"])  # type: ignore[arg-type]
    assert dialed == [
        "mongodb://u:p@b:27017/?directConnection=true",
        "mongodb://u:p@c:27017/?directConnection=true",
    ]
    assert (report.members_total, report.members_reached, report.unreachable) == (3, 2, ["c:27017"])
    usage = report.usage["app.orders"]
    assert usage is not None
    assert usage["x_1"] == IndexUsage("app.orders", "x_1", 12, T1, 2)
    assert usage["y_1"].ops == 0 and usage["y_1"].members_reported == 2
    assert secondary.closed and not primary.closed


def test_collect_usage_standalone_and_building_indexes() -> None:
    node = FakeMember(
        {"isWritablePrimary": True},
        {"orders": [_doc("x_1", 3, T1, "h:27017"), _doc("b_1", 0, T1, "h:27017", building=True)]},
    )
    report = collect_usage(node, ["app.orders", "app.empty"])  # type: ignore[arg-type]
    assert (report.members_total, report.members_reached) == (1, 1)
    assert report.usage["app.orders"] is not None
    assert set(report.usage["app.orders"]) == {"x_1"}
    assert report.usage["app.empty"] == {}


def test_collect_usage_sums_shards_behind_mongos() -> None:
    docs = [_doc("x_1", 2, T2, "s1:27017", shard="s1"), _doc("x_1", 3, T1, "s2:27017", shard="s2")]
    node = FakeMember({"msg": "isdbgrid", "isWritablePrimary": True}, {"orders": docs})
    usage = collect_usage(node, ["app.orders"]).usage["app.orders"]  # type: ignore[arg-type]
    assert usage is not None and usage["x_1"] == IndexUsage("app.orders", "x_1", 5, T1, 2)


def test_collect_usage_degrades_when_index_stats_is_unavailable() -> None:
    node = FakeMember(
        {"isWritablePrimary": True}, {"orders": OperationFailure("unauthorized", code=13)}
    )
    report = collect_usage(node, ["app.orders"])  # type: ignore[arg-type]
    assert report.usage["app.orders"] is None
