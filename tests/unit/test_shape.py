from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from bson import json_util

from indexwright.shape import compact, normalize

_TEXT = Path(__file__).with_name("shape_cases.json").read_text()
# Commands are loaded as BSON so dates, ObjectIds and regexes are real types, like from the
# profiler. Expected shapes are plain JSON; json_util would turn {"$regex": "?"} into a Regex.
CASES: list[dict[str, Any]] = [
    {**plain, "command": bson["command"]}
    for plain, bson in zip(json.loads(_TEXT), json_util.loads(_TEXT), strict=True)
]
BY_NAME = {c["name"]: c for c in CASES}


def _fingerprint(case: dict[str, Any]) -> str:
    shape, _ = normalize("app.orders", case["op"], case["command"])
    return shape.fingerprint


def test_golden_file_has_enough_cases() -> None:
    assert len(CASES) >= 40
    assert len(BY_NAME) == len(CASES), "duplicate case names"


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_golden(case: dict[str, Any]) -> None:
    shape, meta = normalize("app.orders", case["op"], case["command"])
    expected = case["expected"]
    assert shape.filter == expected["filter"]
    assert shape.sort == expected["sort"]
    assert shape.pipeline == expected["pipeline"]
    assert meta.in_size == expected["in_size"]
    assert sorted(meta.unanchored_regex) == expected["unanchored_regex"]
    assert meta.projection_present == expected["projection_present"]
    assert shape.ns == "app.orders"
    assert shape.op == case["op"]


@pytest.mark.parametrize(
    "case", [c for c in CASES if "same_as" in c], ids=[c["name"] for c in CASES if "same_as" in c]
)
def test_same_fingerprint(case: dict[str, Any]) -> None:
    assert _fingerprint(case) == _fingerprint(BY_NAME[case["same_as"]])


@pytest.mark.parametrize(
    "case",
    [c for c in CASES if "differs_from" in c],
    ids=[c["name"] for c in CASES if "differs_from" in c],
)
def test_different_fingerprint(case: dict[str, Any]) -> None:
    assert _fingerprint(case) != _fingerprint(BY_NAME[case["differs_from"]])


def test_fingerprint_is_stable_across_runs_and_machines() -> None:
    shape, _ = normalize("app.orders", "find", {"find": "orders", "filter": {"status": "new"}})
    assert shape.fingerprint == "5941a1b7f5add3343d2f29da3b54a54a5d36215e"


def test_fingerprint_does_not_depend_on_namespace() -> None:
    command = {"find": "orders", "filter": {"status": "new"}}
    a, _ = normalize("app.orders", "find", command)
    b, _ = normalize("other.things", "find", command)
    assert a.fingerprint == b.fingerprint


def test_large_in_size() -> None:
    command = {"find": "orders", "filter": {"_id": {"$in": list(range(250))}}}
    _, meta = normalize("app.orders", "find", command)
    assert meta.in_size == 250


def test_python_compiled_regex() -> None:
    command = {"find": "orders", "filter": {"a": re.compile("x"), "b": re.compile("^y")}}
    shape, meta = normalize("app.orders", "find", command)
    assert shape.filter == {"a": {"$regex": "?"}, "b": {"$regex": "?"}}
    assert meta.unanchored_regex == frozenset({"a"})


def test_unknown_op_still_produces_a_shape() -> None:
    shape, _ = normalize("app.orders", "explain", {"explain": {}})
    assert shape.filter == {} and shape.sort == [] and shape.pipeline is None


@pytest.mark.parametrize(
    ("op", "command", "expected"),
    [
        ("find", {"filter": {"status": "new", "n": {"$gte": 1}}}, "{n:{$gte:?}, status:?}"),
        ("find", {"filter": {"a": 1}, "sort": {"b": -1, "c": 1}}, "{a:?} sort {b:-1, c:1}"),
        ("find", {}, "{}"),
        (
            "aggregate",
            {"pipeline": [{"$match": {"a": 1}}, {"$sort": {"b": 1}}, {"$group": {"_id": None}}]},
            "[{$match:{a:?}}, {$sort:{b:1}}, {$group:{_id:null}}]",
        ),
        ("find", {"filter": {"deleted": {"$exists": False}}}, "{deleted:{$exists:false}}"),
        (
            "find",
            {"filter": {"$or": [{"a": [1]}, {"b": {"$in": [1]}}]}},
            "{$or:[{a:[?]}, {b:{$in:[?]}}]}",
        ),
    ],
)
def test_compact(op: str, command: dict[str, Any], expected: str) -> None:
    shape, _ = normalize("app.orders", op, command)
    assert compact(shape) == expected
