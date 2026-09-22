from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from indexwright.source import to_entry

TS = datetime(2026, 9, 22, 6, 5, 24, tzinfo=UTC)
FIND = {"find": "orders", "filter": {"status": "new"}, "sort": {"n": -1}, "$db": "app"}


def _doc(op: str, command: dict[str, Any], **extra: Any) -> dict[str, Any]:
    return {"op": op, "ns": "app.orders", "command": command, "ts": TS, "millis": 12, **extra}


def test_find() -> None:
    doc = _doc(
        "query",
        FIND,
        docsExamined=200,
        keysExamined=0,
        nreturned=10,
        hasSortStage=True,
        planSummary="COLLSCAN",
    )
    entry = to_entry(doc)
    assert entry is not None
    assert (entry.ns, entry.op, entry.ts, entry.millis) == ("app.orders", "find", TS, 12)
    assert (entry.docs_examined, entry.keys_examined, entry.nreturned) == (200, 0, 10)
    assert entry.has_sort_stage and entry.plan_summary == "COLLSCAN"
    assert entry.command is FIND
    assert not entry.getmore


def test_getmore_is_attributed_to_the_originating_command() -> None:
    doc = _doc("getmore", {"getMore": 42, "collection": "orders"}, originatingCommand=FIND)
    entry = to_entry(doc)
    assert entry is not None
    assert entry.op == "find" and entry.getmore and entry.command is FIND


def test_getmore_without_originating_command_is_skipped() -> None:
    assert to_entry(_doc("getmore", {"getMore": 42, "collection": "orders"})) is None


@pytest.mark.parametrize(
    ("profile_op", "command", "op"),
    [
        ("update", {"q": {"status": "new"}, "u": {"$set": {"x": 1}}}, "update"),
        ("remove", {"q": {"n": 1}, "limit": 1}, "delete"),
        ("command", {"count": "orders", "query": {}}, "count"),
        ("command", {"distinct": "orders", "key": "status"}, "distinct"),
        ("command", {"findAndModify": "orders", "query": {}}, "findAndModify"),
        ("command", {"aggregate": "orders", "pipeline": []}, "aggregate"),
    ],
)
def test_ops(profile_op: str, command: dict[str, Any], op: str) -> None:
    entry = to_entry(_doc(profile_op, command))
    assert entry is not None and entry.op == op


def test_update_uses_matched_count_as_returned() -> None:
    entry = to_entry(_doc("update", {"q": {}, "u": {}}, nMatched=3, nModified=3))
    assert entry is not None and entry.nreturned == 3


def test_delete_uses_deleted_count_as_returned() -> None:
    entry = to_entry(_doc("remove", {"q": {}}, ndeleted=2))
    assert entry is not None and entry.nreturned == 2


def test_missing_counters_default_to_zero_and_unknown_returned() -> None:
    entry = to_entry(_doc("command", {"distinct": "orders", "key": "s"}))
    assert entry is not None
    assert (entry.docs_examined, entry.keys_examined, entry.nreturned) == (0, 0, None)
    assert not entry.has_sort_stage and entry.plan_summary == ""


@pytest.mark.parametrize(
    "doc",
    [
        _doc("insert", {"insert": "orders"}),
        _doc("command", {"killCursors": "orders", "cursors": [1]}),
        _doc("command", {"hello": 1}),
        _doc("command", {"update": "orders", "ordered": True}),
        _doc("command", {"delete": "orders", "ordered": True}),
        _doc("command", {"$truncated": "…", "comment": "too long"}),
        _doc("query", FIND) | {"ns": "app.system.profile"},
        _doc("query", FIND) | {"command": None},
        _doc(
            "command",
            {"aggregate": "orders", "pipeline": [{"$indexStats": {}}]},
            appName="indexwright/0.0.1",
        ),
        {"op": "query", "ns": "app.orders", "ts": TS},
    ],
    ids=[
        "insert",
        "killCursors",
        "admin command",
        "update wrapper",
        "delete wrapper",
        "truncated",
        "system ns",
        "no command",
        "own",
        "bare",
    ],
)
def test_skipped(doc: dict[str, Any]) -> None:
    assert to_entry(doc) is None
