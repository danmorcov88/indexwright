from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import OperationFailure

from indexwright.indexes import IndexCatalog, index_info, list_indexes


def test_index_info_maps_spec_fields() -> None:
    spec = {
        "v": 2,
        "key": {"status": 1, "created": -1, "loc": "2dsphere"},
        "name": "status_1_created_-1_loc_2dsphere",
        "unique": True,
        "partialFilterExpression": {"status": "new"},
        "hidden": True,
    }
    info = index_info("app.orders", spec)
    assert info.keys == (("status", 1), ("created", -1), ("loc", "2dsphere"))
    assert info.name == "status_1_created_-1_loc_2dsphere"
    assert info.unique and info.partial and info.hidden and not info.sparse


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
