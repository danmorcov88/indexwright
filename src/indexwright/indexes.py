from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pymongo.errors import OperationFailure

from indexwright.model import IndexInfo

if TYPE_CHECKING:
    from indexwright.mongo import Connection

NAMESPACE_NOT_FOUND = 26


class IndexCatalog:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._cache: dict[str, list[IndexInfo]] = {}

    def for_ns(self, ns: str) -> list[IndexInfo]:
        if ns not in self._cache:
            self._cache[ns] = list_indexes(self.conn, ns)
        return self._cache[ns]


def list_indexes(conn: Connection, ns: str) -> list[IndexInfo]:
    db, coll = ns.split(".", 1)
    try:
        result = conn.command(db, {"listIndexes": coll})
    except OperationFailure as exc:
        if exc.code == NAMESPACE_NOT_FOUND:
            return []
        raise
    cursor = result["cursor"]
    specs: list[dict[str, Any]] = list(cursor["firstBatch"])
    while cursor.get("id"):
        result = conn.command(db, {"getMore": cursor["id"], "collection": coll})
        cursor = result["cursor"]
        specs.extend(cursor["nextBatch"])
    return [index_info(ns, spec) for spec in specs]


def index_info(ns: str, spec: dict[str, Any]) -> IndexInfo:
    return IndexInfo(
        ns=ns,
        name=str(spec["name"]),
        keys=tuple((str(k), v) for k, v in spec["key"].items()),
        unique=bool(spec.get("unique")),
        sparse=bool(spec.get("sparse")),
        partial="partialFilterExpression" in spec,
        hidden=bool(spec.get("hidden")),
    )
