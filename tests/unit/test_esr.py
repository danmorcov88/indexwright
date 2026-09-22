from __future__ import annotations

import re
from typing import Any

import pytest

from indexwright.model import IndexInfo
from indexwright.rules.esr import (
    Candidate,
    advise,
    candidates,
    classify,
    covered_by,
    format_keys,
    recommendation,
)
from indexwright.shape import normalize


def _shape(filter_: dict[str, Any], sort: dict[str, Any] | None = None) -> Any:
    command: dict[str, Any] = {"find": "orders", "filter": filter_}
    if sort:
        command["sort"] = sort
    shape, _ = normalize("app.orders", "find", command)
    return shape


def _keys(filter_: dict[str, Any], sort: dict[str, Any] | None = None) -> list[tuple[Any, ...]]:
    return [c.keys for c in candidates(_shape(filter_, sort))]


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("?", "equality"),
        (["?"], "equality"),
        ({"$in": ["?"]}, "equality"),
        ({"$eq": "?"}, "equality"),
        ({"$all": ["?"]}, "equality"),
        ({"$gt": "?"}, "range"),
        ({"$gte": "?", "$lt": "?"}, "range"),
        ({"$regex": "?"}, "range"),
        ({"$exists": True}, "range"),
        ({"$in": ["?"], "$gt": "?"}, "equality"),
        ({"$exists": False}, None),
        ({"$ne": "?"}, None),
        ({"$nin": ["?"]}, None),
        ({"$not": {"$gt": "?"}}, None),
        ({"$size": "?"}, None),
        ({"$elemMatch": {"a": "?"}}, None),
    ],
)
def test_classify(value: Any, kind: str | None) -> None:
    assert classify(value) == kind


def test_equality_then_sort_then_range() -> None:
    keys = _keys(
        {"status": "new", "created": {"$gte": 1, "$lt": 2}, "customer_id": 7, "total": {"$gt": 1}},
        {"priority": -1, "name": 1},
    )
    assert keys == [
        (
            ("customer_id", 1),
            ("status", 1),
            ("priority", -1),
            ("name", 1),
            ("created", 1),
            ("total", 1),
        )
    ]


def test_sort_field_that_is_also_equality_is_dropped_from_sort() -> None:
    assert _keys({"status": "new"}, {"status": 1, "created": -1}) == [
        (("status", 1), ("created", -1))
    ]


def test_range_field_that_is_also_sorted_keeps_sort_position() -> None:
    assert _keys({"created": {"$gte": 1}}, {"created": -1}) == [(("created", -1),)]


def test_and_clauses_are_merged() -> None:
    assert _keys({"$and": [{"status": "new"}, {"total": {"$gt": 1}}]}) == [
        (("status", 1), ("total", 1))
    ]


def test_or_gives_one_candidate_per_branch_with_shared_fields() -> None:
    keys = _keys(
        {"tenant": 1, "$or": [{"status": "new"}, {"total": {"$gt": 990}}]}, {"created": -1}
    )
    assert keys == [
        (("status", 1), ("tenant", 1), ("created", -1)),
        (("tenant", 1), ("created", -1), ("total", 1)),
    ]


def test_identical_or_branches_are_not_repeated() -> None:
    assert _keys({"$or": [{"a": 1}, {"a": 2}]}) == [(("a", 1),)]


def test_id_equality_needs_no_index() -> None:
    assert _keys({"_id": 1, "status": "x"}) == []


def test_negations_and_unknown_operators_give_nothing() -> None:
    assert _keys({"status": {"$ne": "x"}}) == []
    assert _keys({"$expr": "?"}) == []
    assert _keys({}) == []


def test_sort_only_query() -> None:
    assert _keys({}, {"created": -1}) == [(("created", -1),)]


def _index(name: str, *keys: tuple[str, Any], **flags: bool) -> IndexInfo:
    return IndexInfo("app.orders", name, keys, **flags)


@pytest.mark.parametrize(
    ("candidate", "index_keys", "covered"),
    [
        (Candidate((("status", 1),), 1), (("status", 1),), True),
        (Candidate((("status", 1),), 1), (("status", -1),), True),
        (Candidate((("status", 1),), 1), (("status", 1), ("created", -1)), True),
        (Candidate((("status", 1), ("created", -1)), 1), (("status", 1),), False),
        (Candidate((("status", 1), ("created", -1)), 1), (("status", 1), ("created", -1)), True),
        (Candidate((("status", 1), ("created", -1)), 1), (("status", 1), ("created", 1)), True),
        (Candidate((("status", 1), ("created", -1)), 1), (("status", -1), ("created", 1)), True),
        (Candidate((("a", 1), ("b", -1)), 0), (("a", 1), ("b", 1)), False),
        (Candidate((("a", 1), ("b", -1)), 0), (("a", -1), ("b", 1)), True),
        (Candidate((("status", 1),), 1), (("created", 1), ("status", 1)), False),
        (Candidate((("status", 1),), 1), (("status", "text"),), False),
    ],
)
def test_covered_by(candidate: Candidate, index_keys: tuple[Any, ...], covered: bool) -> None:
    index = _index("idx", *index_keys)
    assert (covered_by(candidate, [index]) is index) == covered


def test_partial_and_hidden_indexes_do_not_cover() -> None:
    candidate = Candidate((("status", 1),), 1)
    assert covered_by(candidate, [_index("p", ("status", 1), partial=True)]) is None
    assert covered_by(candidate, [_index("h", ("status", 1), hidden=True)]) is None


def test_recommendation_statement_and_name() -> None:
    rec = recommendation("app.orders", (("status", 1), ("created", -1), ("address.city", 1)))
    assert rec.kind == "create_index" and rec.ns == "app.orders"
    assert rec.statement.startswith(
        'db.orders.createIndex({status: 1, created: -1, "address.city": 1}, {name: "orders_esr_'
    )
    assert format_keys((("loc", "2dsphere"),)) == '{loc: "2dsphere"}'


def test_recommendation_name_is_stable() -> None:
    a = recommendation("app.orders", (("status", 1),))
    b = recommendation("app.orders", (("status", 1),))
    assert a == b
    assert re.search(r'name: "orders_esr_[0-9a-f]{6}"', a.statement)


def test_advise_splits_covered_and_missing() -> None:
    shape = _shape({"$or": [{"status": "new"}, {"total": {"$gt": 990}}]})
    advice = advise(shape, [_index("status_1", ("status", 1))])
    assert [r.keys for r in advice.recommendations] == [(("total", 1),)]
    assert advice.covered == ["status_1 already covers {status: 1}"]
    assert not advice.empty
    assert advise(_shape({"status": {"$ne": 1}}), []).empty
