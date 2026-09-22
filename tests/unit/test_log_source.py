from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest

from indexwright.source import LogFormatError, read_log

if TYPE_CHECKING:
    from pathlib import Path


def _line(t: str, attr: dict[str, Any], msg: str = "Slow query", c: str = "COMMAND") -> str:
    record = {"t": {"$date": t}, "s": "I", "c": c, "id": 51803, "ctx": "conn1", "msg": msg}
    record["attr"] = attr
    return json.dumps(record)


FIND = {
    "type": "command",
    "ns": "app.orders",
    "appName": "app",
    "command": {"find": "orders", "filter": {"status": "new"}, "lsid": {"id": 1}, "$db": "app"},
    "planSummary": "COLLSCAN",
    "keysExamined": 0,
    "docsExamined": 5000,
    "nreturned": 10,
    "durationMillis": 12,
}
GETMORE = {
    "type": "command",
    "ns": "app.orders",
    "command": {"getMore": {"$numberLong": "42"}, "collection": "orders"},
    "originatingCommand": {"find": "orders", "filter": {"status": "new"}},
    "nreturned": 10,
    "docsExamined": 0,
    "durationMillis": 1,
}
UPDATE = {
    "type": "update",
    "ns": "app.orders",
    "command": {"q": {"_id": 1}, "u": {"$set": {"x": 1}}, "multi": False, "upsert": False},
    "planSummary": "IDHACK",
    "nMatched": 1,
    "nModified": 1,
    "durationMillis": 3,
}
REMOVE = {
    "type": "remove",
    "ns": "other.things",
    "command": {"q": {"n": {"$regularExpression": {"pattern": "^x", "options": ""}}}, "limit": 1},
    "ndeleted": 1,
    "durationMillis": 2,
}
TRUNCATED = {
    "type": "command",
    "ns": "app.orders",
    "command": {"$truncated": "{ find: ...", "comment": "big"},
    "durationMillis": 2,
}
OWN = {**FIND, "appName": "indexwright/0.0.1"}

LINES = [
    "text log line from an old server",
    _line("2026-09-22T08:00:00.000+00:00", FIND),
    _line("2026-09-22T08:00:01.000+00:00", GETMORE),
    _line("2026-09-22T08:00:02.000+00:00", UPDATE, c="WRITE"),
    _line("2026-09-22T08:00:03.000+00:00", REMOVE, c="WRITE"),
    _line("2026-09-22T08:00:04.000+00:00", TRUNCATED),
    _line("2026-09-22T08:00:05.000+00:00", OWN),
    _line("2026-09-22T08:00:06.000+00:00", {"type": "command"}, msg="Connection accepted"),
    "{not json",
]


@pytest.fixture
def log_file(tmp_path: Path) -> Path:
    path = tmp_path / "mongod.log"
    path.write_text("\n".join(LINES) + "\n", encoding="utf-8")
    return path


def test_read_log_maps_slow_queries(log_file: Path) -> None:
    entries = list(read_log([str(log_file)], None, 1000, None))
    assert [(e.op, e.ns, e.millis, e.getmore) for e in entries] == [
        ("find", "app.orders", 12, False),
        ("find", "app.orders", 1, True),
        ("update", "app.orders", 3, False),
        ("delete", "other.things", 2, False),
    ]
    find = entries[0]
    assert find.ts == datetime(2026, 9, 22, 8, 0, tzinfo=UTC)
    assert (find.docs_examined, find.nreturned, find.plan_summary) == (5000, 10, "COLLSCAN")
    assert find.command["filter"] == {"status": "new"}
    assert entries[1].command is not None and entries[1].command["find"] == "orders"
    assert entries[2].nreturned == 1 and entries[3].nreturned == 1
    assert entries[3].command["q"]["n"].pattern == "^x"


def test_read_log_filters(log_file: Path) -> None:
    since = datetime(2026, 9, 22, 8, 0, 2, tzinfo=UTC)
    assert [e.op for e in read_log([str(log_file)], since, 1000, None)] == ["update", "delete"]
    assert [e.ns for e in read_log([str(log_file)], None, 1000, "app")] == ["app.orders"] * 3
    assert len(list(read_log([str(log_file)], None, 2, None))) == 2


def test_read_log_gzip_and_glob(log_file: Path, tmp_path: Path) -> None:
    packed = tmp_path / "mongod.log.1.gz"
    with gzip.open(packed, "wt", encoding="utf-8") as handle:
        handle.write(log_file.read_text(encoding="utf-8"))
    assert len(list(read_log([str(packed)], None, 1000, None))) == 4
    pattern = str(tmp_path / "mongod.log*")
    assert len(list(read_log([pattern], None, 1000, None))) == 8


def test_read_log_rejects_text_logs(tmp_path: Path) -> None:
    old = tmp_path / "old.log"
    old.write_text("2020-01-01T00:00:00.000+0000 I COMMAND [conn1] command app.orders ...\n")
    with pytest.raises(LogFormatError, match=r"4\.4"):
        list(read_log([str(old)], None, 1000, None))
    empty = tmp_path / "empty.log"
    empty.write_text("")
    assert list(read_log([str(empty)], None, 1000, None)) == []
