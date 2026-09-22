from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from indexwright.model import ExplainResult, IndexInfo, ShapeStats
from indexwright.rules import RULES, run_rules, sort_findings
from indexwright.rules.base import severity
from indexwright.shape import normalize

NOW = datetime(2026, 9, 22, tzinfo=UTC)
STATUS_1 = IndexInfo("app.orders", "status_1", (("status", 1),))
CREATED_EMAIL = IndexInfo("app.orders", "created_1_email_1", (("created", 1), ("email", 1)))


def _stats(
    filter_: dict[str, Any],
    sort: dict[str, Any] | None = None,
    *,
    op: str = "find",
    count: int = 50,
    p99: int = 20,
    docs: int = 1000,
    keys: int = 1000,
    returned: int = 1000,
    has_sort_stage: bool = False,
    plans: tuple[str, ...] = (),
) -> ShapeStats:
    command: dict[str, Any] = {"find": "orders", "filter": filter_}
    if sort:
        command["sort"] = sort
    shape, meta = normalize("app.orders", op, command)
    return ShapeStats(
        shape=shape,
        count=count,
        p50_ms=p99 // 2,
        p99_ms=p99,
        max_ms=p99,
        total_ms=p99 * count,
        docs_examined=docs * count,
        keys_examined=keys * count,
        nreturned=returned * count,
        has_sort_stage=has_sort_stage,
        plan_summaries=frozenset(plans),
        meta=meta,
        first_seen=NOW,
        last_seen=NOW,
        sample=command,
    )


COLLSCAN = ExplainResult(("COLLSCAN",))
IXSCAN = ExplainResult(("FETCH", "IXSCAN"), ("status_1",))
IXSCAN_RESIDUAL = ExplainResult(("FETCH", "IXSCAN"), ("status_1",), frozenset({"total"}))
IXSCAN_SORT = ExplainResult(("SORT", "FETCH", "IXSCAN"), ("status_1",))
FAILED = ExplainResult(error="boom")


def test_registry_has_the_four_rules_in_order() -> None:
    assert [name for name, _ in RULES] == [
        "collscan",
        "sort_in_memory",
        "docs_examined_ratio",
        "low_selectivity_index",
    ]


@pytest.mark.parametrize(
    ("count", "p99", "expected"),
    [(200, 2000, "critical"), (50, 2000, "high"), (2000, 5, "high"), (50, 20, "medium")],
)
def test_severity(count: int, p99: int, expected: str) -> None:
    assert severity(_stats({"a": 1}, count=count, p99=p99)) == expected


def test_collscan_recommends_esr_index() -> None:
    stats = _stats({"email": {"$regex": "x"}}, docs=5000, returned=10)
    findings = run_rules(stats, COLLSCAN, [STATUS_1])
    assert [f.rule for f in findings] == ["collscan"]
    finding = findings[0]
    assert finding.severity == "medium"
    assert finding.recommendation is not None
    assert finding.recommendation.keys == (("email", 1),)
    assert "5000 documents examined per execution" in finding.message
    assert finding.evidence["docs_per_returned"] == 500
    assert finding.shape_id == stats.shape.fingerprint and finding.ns == "app.orders"


def test_collscan_on_small_collection_is_low() -> None:
    stats = _stats({"email": "x"}, docs=40, returned=1)
    assert run_rules(stats, COLLSCAN, [])[0].severity == "low"


def test_collscan_falls_back_to_profiler_plan_when_explain_is_missing() -> None:
    stats = _stats({"email": "x"}, docs=5000, returned=10, plans=("COLLSCAN",))
    assert [f.rule for f in run_rules(stats, None, [])] == ["collscan"]
    assert [f.rule for f in run_rules(stats, FAILED, [])] == ["collscan"]
    assert run_rules(_stats({"email": "x"}, plans=("IXSCAN { email: 1 }",)), None, []) == []


def test_collscan_or_gives_one_finding_per_uncovered_branch() -> None:
    stats = _stats({"$or": [{"status": "new"}, {"total": {"$gt": 990}}]}, docs=5000, returned=10)
    findings = run_rules(stats, ExplainResult(("SUBPLAN", "COLLSCAN")), [STATUS_1])
    assert len(findings) == 1
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("total", 1),)


def test_collscan_covered_by_existing_index_has_no_recommendation() -> None:
    stats = _stats({"status": "new"}, docs=5000, returned=10)
    finding = run_rules(stats, COLLSCAN, [STATUS_1])[0]
    assert finding.recommendation is None
    assert "status_1 already covers {status: 1}" in finding.message


def test_collscan_without_indexable_predicate() -> None:
    stats = _stats({"status": {"$ne": "done"}}, docs=5000, returned=10)
    finding = run_rules(stats, COLLSCAN, [])[0]
    assert finding.recommendation is None
    assert "no indexable predicate" in finding.message


def test_sort_in_memory() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True)
    findings = run_rules(stats, IXSCAN_SORT, [STATUS_1])
    assert [f.rule for f in findings] == ["sort_in_memory"]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("status", 1), ("total", -1))
    assert "sort on {total:-1}" in findings[0].message


def test_sort_in_memory_uses_profiler_flag_without_explain() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True)
    assert [f.rule for f in run_rules(stats, None, [STATUS_1])] == ["sort_in_memory"]
    quiet = _stats({"status": "new"}, {"total": -1}, has_sort_stage=False)
    assert run_rules(quiet, None, [STATUS_1]) == []


def test_sort_merge_is_not_an_in_memory_sort() -> None:
    stats = _stats({"status": {"$in": [1, 2]}}, {"total": -1}, has_sort_stage=True)
    plan = ExplainResult(("SORT_MERGE", "IXSCAN", "IXSCAN"), ("status_1_total_-1",))
    assert run_rules(stats, plan, []) == []


def test_collscan_owns_shapes_that_also_sort() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True, docs=5000, returned=10)
    findings = run_rules(stats, ExplainResult(("SORT", "COLLSCAN")), [])
    assert [f.rule for f in findings] == ["collscan"]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("status", 1), ("total", -1))


def test_docs_examined_ratio() -> None:
    stats = _stats({"status": "new", "total": {"$gt": 990}}, docs=1000, keys=1000, returned=5)
    findings = run_rules(stats, IXSCAN_RESIDUAL, [STATUS_1])
    assert [f.rule for f in findings] == ["docs_examined_ratio"]
    finding = findings[0]
    assert finding.recommendation is not None
    assert finding.recommendation.keys == (("status", 1), ("total", 1))
    assert "filter on total runs after fetching" in finding.message
    assert finding.evidence["filter_fields_not_in_index"] == ["total"]


def test_docs_examined_ratio_needs_a_bad_ratio_and_a_residual_filter() -> None:
    fine = _stats({"status": "new", "total": {"$gt": 1}}, docs=1000, keys=1000, returned=200)
    assert run_rules(fine, IXSCAN_RESIDUAL, [STATUS_1]) == []
    no_residual = _stats({"status": "new"}, docs=1000, keys=1000, returned=5)
    assert [f.rule for f in run_rules(no_residual, IXSCAN, [STATUS_1])] == ["low_selectivity_index"]
    assert run_rules(no_residual, None, [STATUS_1]) == []


def test_low_selectivity_index() -> None:
    stats = _stats({"created": {"$gte": 1, "$lt": 2}, "email": "x"}, docs=1, keys=166, returned=1)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    findings = run_rules(stats, plan, [CREATED_EMAIL])
    assert [f.rule for f in findings] == ["low_selectivity_index"]
    finding = findings[0]
    assert finding.recommendation is not None
    assert finding.recommendation.keys == (("email", 1), ("created", 1))
    assert "created_1_email_1 examines 166 index keys" in finding.message


def test_low_selectivity_with_the_right_index_already_present() -> None:
    stats = _stats({"country": "RO"}, docs=1, keys=100, returned=1)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("country_1",))
    country = IndexInfo("app.orders", "country_1", (("country", 1),))
    finding = run_rules(stats, plan, [country])[0]
    assert finding.rule == "low_selectivity_index" and finding.recommendation is None
    assert "country_1 already covers {country: 1}" in finding.message


def test_ratio_rules_need_a_known_returned_count() -> None:
    stats = _stats({"created": {"$gte": 1}}, op="distinct", keys=5000, returned=0)
    unknown = ShapeStats(**{**stats.__dict__, "returned_known": False})
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    assert run_rules(unknown, plan, [CREATED_EMAIL]) == []
    assert run_rules(unknown, IXSCAN_RESIDUAL, [STATUS_1]) == []
    assert [f.rule for f in run_rules(unknown, COLLSCAN, [])] == ["collscan"]


def test_good_queries_produce_nothing() -> None:
    assert run_rules(_stats({"customer_id": 7}), IXSCAN, [STATUS_1]) == []
    assert run_rules(_stats({"_id": 1}), ExplainResult(("IDHACK",)), []) == []
    assert run_rules(_stats({"status": "new"}, returned=1000), IXSCAN, [STATUS_1]) == []


def test_sort_findings_orders_by_severity_then_weight() -> None:
    a = run_rules(_stats({"a": 1}, count=200, p99=2000, docs=5000, returned=1), COLLSCAN, [])[0]
    b = run_rules(_stats({"b": 1}, count=10, docs=5000, returned=1), COLLSCAN, [])[0]
    c = run_rules(_stats({"c": 1}, count=10, docs=5000, returned=1), COLLSCAN, [])[0]
    ordered = sort_findings([c, b, a], {b.shape_id: 10, c.shape_id: 20})
    assert [f.shape_id for f in ordered] == [a.shape_id, c.shape_id, b.shape_id]


def test_sort_in_memory_owns_shapes_with_a_limit_ratio() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, keys=1000, docs=1000, returned=20)
    assert [f.rule for f in run_rules(stats, IXSCAN_SORT, [STATUS_1])] == ["sort_in_memory"]
    with_residual = ExplainResult(("SORT", "FETCH", "IXSCAN"), ("status_1",), frozenset({"x"}))
    stats = _stats({"status": "new", "x": {"$gt": 1}}, {"total": -1}, docs=1000, returned=5)
    assert [f.rule for f in run_rules(stats, with_residual, [STATUS_1])] == ["sort_in_memory"]


def test_low_selectivity_names_an_unanchored_regex_as_the_cause() -> None:
    stats = _stats({"email": {"$regex": "user1"}}, keys=5000, returned=16)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("email_1",))
    finding = run_rules(stats, plan, [IndexInfo("app.orders", "email_1", (("email", 1),))])[0]
    assert "regex on email is not anchored" in finding.message
    assert finding.recommendation is None
