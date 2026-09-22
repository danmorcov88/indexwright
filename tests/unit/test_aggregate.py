from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from indexwright.aggregate import group, percentile
from indexwright.model import Entry

T0 = datetime(2026, 9, 22, 6, 0, tzinfo=UTC)


def _entry(
    millis: int,
    command: dict[str, Any] | None = None,
    *,
    ns: str = "app.orders",
    op: str = "find",
    docs: int = 100,
    keys: int = 0,
    returned: int = 10,
    getmore: bool = False,
    minute: int = 0,
    plan: str = "COLLSCAN",
) -> Entry:
    return Entry(
        ns=ns,
        op=op,
        ts=T0 + timedelta(minutes=minute),
        millis=millis,
        docs_examined=docs,
        keys_examined=keys,
        nreturned=returned,
        has_sort_stage=False,
        plan_summary=plan,
        command=command or {"find": "orders", "filter": {"status": "new"}},
        getmore=getmore,
    )


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([], 50, 0),
        ([7], 99, 7),
        ([1, 2, 3, 4], 50, 2),
        ([4, 3, 2, 1], 50, 2),
        ([1, 2, 3, 4], 99, 4),
        (list(range(1, 101)), 99, 99),
        (list(range(1, 101)), 50, 50),
    ],
)
def test_percentile_nearest_rank(values: list[int], p: float, expected: int) -> None:
    assert percentile(values, p) == expected


def test_groups_by_namespace_and_fingerprint() -> None:
    entries = [
        _entry(10, {"find": "orders", "filter": {"status": "new"}}),
        _entry(30, {"find": "orders", "filter": {"status": "done"}}),
        _entry(5, {"find": "orders", "filter": {"status": "new"}}, ns="app.archive"),
        _entry(1, {"find": "orders", "filter": {"n": {"$gt": 1}}}),
    ]
    stats = group(entries)
    assert [(s.shape.ns, s.count, s.total_ms) for s in stats] == [
        ("app.orders", 2, 40),
        ("app.archive", 1, 5),
        ("app.orders", 1, 1),
    ]


def test_latency_and_work_are_accumulated() -> None:
    stats = group([_entry(10, minute=2), _entry(20, minute=0), _entry(90, minute=1)])[0]
    assert (stats.count, stats.p50_ms, stats.p99_ms, stats.max_ms, stats.total_ms) == (
        3,
        20,
        90,
        90,
        120,
    )
    assert (stats.docs_examined, stats.nreturned) == (300, 30)
    assert stats.docs_per_returned == 10
    assert stats.first_seen == T0 and stats.last_seen == T0 + timedelta(minutes=2)
    assert stats.plan_summaries == frozenset({"COLLSCAN"})


def test_getmore_adds_work_but_not_executions() -> None:
    stats = group(
        [_entry(10, docs=100, returned=20), _entry(500, docs=50, returned=20, getmore=True)]
    )
    assert len(stats) == 1
    s = stats[0]
    assert (s.count, s.p50_ms, s.p99_ms, s.total_ms) == (1, 10, 10, 510)
    assert (s.docs_examined, s.nreturned) == (150, 40)


def test_only_getmores_gives_zero_count() -> None:
    s = group([_entry(5, getmore=True)])[0]
    assert (s.count, s.p50_ms, s.max_ms, s.total_ms) == (0, 0, 0, 5)


def test_metadata_is_merged_across_entries() -> None:
    entries = [
        _entry(1, {"find": "orders", "filter": {"a": {"$in": [1, 2]}, "n": {"$regex": "^x"}}}),
        _entry(1, {"find": "orders", "filter": {"a": {"$in": [1, 2, 3]}, "n": {"$regex": "y"}}}),
        _entry(
            1,
            {
                "find": "orders",
                "filter": {"a": {"$in": [1]}, "n": {"$regex": "^z"}},
                "projection": {"a": 1},
            },
        ),
    ]
    stats = group(entries)
    assert len(stats) == 1
    meta = stats[0].meta
    assert meta.in_size == 3
    assert meta.unanchored_regex == frozenset({"n"})
    assert meta.projection_present


def test_ratio_with_nothing_returned_does_not_divide_by_zero() -> None:
    s = group([_entry(1, docs=500, returned=0)])[0]
    assert s.docs_per_returned == 500
    assert s.keys_per_returned == 0
