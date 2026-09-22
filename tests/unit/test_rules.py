from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from indexwright.model import ExplainResult, IndexInfo, ShapeStats
from indexwright.rules import RULES, run_rules, sort_findings
from indexwright.rules.base import NoIndexes, severity
from indexwright.rules.predicates import negation_only
from indexwright.shape import normalize

NOW = datetime(2026, 9, 22, tzinfo=UTC)
STATUS_1 = IndexInfo("app.orders", "status_1", (("status", 1),))
CREATED_EMAIL = IndexInfo("app.orders", "created_1_email_1", (("created", 1), ("email", 1)))


class Catalog:
    known = True

    def __init__(self, *indexes: IndexInfo) -> None:
        self.indexes = list(indexes)

    def for_ns(self, ns: str) -> list[IndexInfo]:
        return [i for i in self.indexes if i.ns == ns]


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
        "unanchored_regex",
        "negation_predicate",
        "large_in",
        "lookup_no_index",
    ]


@pytest.mark.parametrize(
    ("count", "p99", "expected"),
    [(200, 2000, "critical"), (50, 2000, "high"), (2000, 5, "high"), (50, 20, "medium")],
)
def test_severity(count: int, p99: int, expected: str) -> None:
    assert severity(_stats({"a": 1}, count=count, p99=p99)) == expected


def test_collscan_recommends_esr_index() -> None:
    stats = _stats({"email": {"$regex": "x"}}, docs=5000, returned=10)
    findings = run_rules(stats, COLLSCAN, Catalog(STATUS_1))
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
    assert run_rules(stats, COLLSCAN, Catalog())[0].severity == "low"


def test_collscan_falls_back_to_profiler_plan_when_explain_is_missing() -> None:
    stats = _stats({"email": "x"}, docs=5000, returned=10, plans=("COLLSCAN",))
    assert [f.rule for f in run_rules(stats, None, Catalog())] == ["collscan"]
    assert [f.rule for f in run_rules(stats, FAILED, Catalog())] == ["collscan"]
    assert run_rules(_stats({"email": "x"}, plans=("IXSCAN { email: 1 }",)), None, Catalog()) == []


def test_collscan_or_gives_one_finding_per_uncovered_branch() -> None:
    stats = _stats({"$or": [{"status": "new"}, {"total": {"$gt": 990}}]}, docs=5000, returned=10)
    findings = run_rules(stats, ExplainResult(("SUBPLAN", "COLLSCAN")), Catalog(STATUS_1))
    assert len(findings) == 1
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("total", 1),)


def test_collscan_covered_by_existing_index_has_no_recommendation() -> None:
    stats = _stats({"status": "new"}, docs=5000, returned=10)
    finding = run_rules(stats, COLLSCAN, Catalog(STATUS_1))[0]
    assert finding.recommendation is None
    assert "status_1 already covers {status: 1}" in finding.message


def test_collscan_without_indexable_predicate() -> None:
    stats = _stats({"tags": {"$size": 3}}, docs=5000, returned=10)
    finding = run_rules(stats, COLLSCAN, Catalog())[0]
    assert finding.rule == "collscan" and finding.recommendation is None
    assert "no indexable predicate" in finding.message


def test_sort_in_memory() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True)
    findings = run_rules(stats, IXSCAN_SORT, Catalog(STATUS_1))
    assert [f.rule for f in findings] == ["sort_in_memory"]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("status", 1), ("total", -1))
    assert "sort on {total:-1}" in findings[0].message


def test_sort_in_memory_uses_profiler_flag_without_explain() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True)
    assert [f.rule for f in run_rules(stats, None, Catalog(STATUS_1))] == ["sort_in_memory"]
    quiet = _stats({"status": "new"}, {"total": -1}, has_sort_stage=False)
    assert run_rules(quiet, None, Catalog(STATUS_1)) == []


def test_sort_merge_is_not_an_in_memory_sort() -> None:
    stats = _stats({"status": {"$in": [1, 2]}}, {"total": -1}, has_sort_stage=True)
    plan = ExplainResult(("SORT_MERGE", "IXSCAN", "IXSCAN"), ("status_1_total_-1",))
    assert run_rules(stats, plan, Catalog()) == []


def test_collscan_owns_shapes_that_also_sort() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, has_sort_stage=True, docs=5000, returned=10)
    findings = run_rules(stats, ExplainResult(("SORT", "COLLSCAN")), Catalog())
    assert [f.rule for f in findings] == ["collscan"]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("status", 1), ("total", -1))


def test_docs_examined_ratio() -> None:
    stats = _stats({"status": "new", "total": {"$gt": 990}}, docs=1000, keys=1000, returned=5)
    findings = run_rules(stats, IXSCAN_RESIDUAL, Catalog(STATUS_1))
    assert [f.rule for f in findings] == ["docs_examined_ratio"]
    finding = findings[0]
    assert finding.recommendation is not None
    assert finding.recommendation.keys == (("status", 1), ("total", 1))
    assert "filter on total runs after fetching" in finding.message
    assert finding.evidence["filter_fields_not_in_index"] == ["total"]


def test_docs_examined_ratio_needs_a_bad_ratio_and_a_residual_filter() -> None:
    fine = _stats({"status": "new", "total": {"$gt": 1}}, docs=1000, keys=1000, returned=200)
    assert run_rules(fine, IXSCAN_RESIDUAL, Catalog(STATUS_1)) == []
    no_residual = _stats({"status": "new"}, docs=1000, keys=1000, returned=5)
    assert [f.rule for f in run_rules(no_residual, IXSCAN, Catalog(STATUS_1))] == [
        "low_selectivity_index"
    ]
    assert run_rules(no_residual, None, Catalog(STATUS_1)) == []


def test_low_selectivity_index() -> None:
    stats = _stats({"created": {"$gte": 1, "$lt": 2}, "email": "x"}, docs=1, keys=166, returned=1)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    findings = run_rules(stats, plan, Catalog(CREATED_EMAIL))
    assert [f.rule for f in findings] == ["low_selectivity_index"]
    finding = findings[0]
    assert finding.recommendation is not None
    assert finding.recommendation.keys == (("email", 1), ("created", 1))
    assert "created_1_email_1 examines 166 index keys" in finding.message


def test_low_selectivity_with_the_right_index_already_present() -> None:
    stats = _stats({"country": "RO"}, docs=1, keys=100, returned=1)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("country_1",))
    country = IndexInfo("app.orders", "country_1", (("country", 1),))
    finding = run_rules(stats, plan, Catalog(country))[0]
    assert finding.rule == "low_selectivity_index" and finding.recommendation is None
    assert "country_1 already covers {country: 1}" in finding.message


def test_ratio_rules_need_a_known_returned_count() -> None:
    stats = _stats({"created": {"$gte": 1}}, op="distinct", keys=5000, returned=0)
    unknown = ShapeStats(**{**stats.__dict__, "returned_known": False})
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    assert run_rules(unknown, plan, Catalog(CREATED_EMAIL)) == []
    assert run_rules(unknown, IXSCAN_RESIDUAL, Catalog(STATUS_1)) == []
    assert [f.rule for f in run_rules(unknown, COLLSCAN, Catalog())] == ["collscan"]


def test_ratio_rules_skip_shapes_that_reduce_output() -> None:
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    distinct = _stats({"created": {"$gte": 1}}, op="distinct", keys=5000, returned=5)
    assert run_rules(distinct, plan, Catalog(CREATED_EMAIL)) == []
    command = {
        "aggregate": "orders",
        "pipeline": [{"$match": {"status": "new"}}, {"$group": {"_id": "$customer_id"}}],
    }
    shape, meta = normalize("app.orders", "aggregate", command)
    base = _stats({"status": "new"}, keys=1000, returned=5)
    grouped = ShapeStats(**{**base.__dict__, "shape": shape, "meta": meta})
    assert run_rules(grouped, IXSCAN, Catalog(STATUS_1)) == []
    assert run_rules(grouped, IXSCAN_RESIDUAL, Catalog(STATUS_1)) == []
    assert [f.rule for f in run_rules(grouped, COLLSCAN, Catalog())] == ["collscan"]


def test_good_queries_produce_nothing() -> None:
    assert run_rules(_stats({"customer_id": 7}), IXSCAN, Catalog(STATUS_1)) == []
    assert run_rules(_stats({"_id": 1}), ExplainResult(("IDHACK",)), Catalog()) == []
    assert run_rules(_stats({"status": "new"}, returned=1000), IXSCAN, Catalog(STATUS_1)) == []


def test_sort_findings_orders_by_severity_then_weight() -> None:
    a = run_rules(
        _stats({"a": 1}, count=200, p99=2000, docs=5000, returned=1), COLLSCAN, Catalog()
    )[0]
    b = run_rules(_stats({"b": 1}, count=10, docs=5000, returned=1), COLLSCAN, Catalog())[0]
    c = run_rules(_stats({"c": 1}, count=10, docs=5000, returned=1), COLLSCAN, Catalog())[0]
    ordered = sort_findings([c, b, a], {b.shape_id: 10, c.shape_id: 20})
    assert [f.shape_id for f in ordered] == [a.shape_id, c.shape_id, b.shape_id]


def test_sort_in_memory_owns_shapes_with_a_limit_ratio() -> None:
    stats = _stats({"status": "new"}, {"total": -1}, keys=1000, docs=1000, returned=20)
    assert [f.rule for f in run_rules(stats, IXSCAN_SORT, Catalog(STATUS_1))] == ["sort_in_memory"]
    with_residual = ExplainResult(("SORT", "FETCH", "IXSCAN"), ("status_1",), frozenset({"x"}))
    stats = _stats({"status": "new", "x": {"$gt": 1}}, {"total": -1}, docs=1000, returned=5)
    assert [f.rule for f in run_rules(stats, with_residual, Catalog(STATUS_1))] == [
        "sort_in_memory"
    ]


def test_unanchored_regex_on_an_indexed_field() -> None:
    stats = _stats({"email": {"$regex": "user1"}}, keys=5000, returned=16)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("email_1",))
    email = IndexInfo("app.orders", "email_1", (("email", 1),))
    findings = run_rules(stats, plan, Catalog(email))
    assert [f.rule for f in findings] == ["unanchored_regex"]
    finding = findings[0]
    assert "not anchored with ^" in finding.message
    assert finding.recommendation is not None and finding.recommendation.kind == "rewrite"
    assert "text index" in finding.recommendation.statement
    assert finding.evidence["fields"] == ["email"]


def test_unanchored_regex_on_an_unindexed_field_is_a_collscan() -> None:
    stats = _stats({"email": {"$regex": "user1"}}, docs=5000, returned=16)
    findings = run_rules(stats, COLLSCAN, Catalog(STATUS_1))
    assert [f.rule for f in findings] == ["collscan"]
    partial = IndexInfo("app.orders", "email_p", (("email", 1),), partial=True)
    plan = ExplainResult(("FETCH", "IXSCAN"), ("created_1_email_1",))
    fine = _stats({"email": {"$regex": "x"}}, returned=1000)
    assert run_rules(fine, plan, Catalog(partial)) == []


def test_negation_only_shapes_are_owned_by_negation_predicate() -> None:
    stats = _stats({"status": {"$ne": "done"}}, docs=5000, returned=10)
    findings = run_rules(stats, COLLSCAN, Catalog())
    assert [f.rule for f in findings] == ["negation_predicate"]
    assert "only excludes values" in findings[0].message
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.kind == "rewrite"
    indexed = _stats(
        {"status": {"$ne": "done"}, "flag": {"$exists": False}}, keys=4000, returned=50
    )
    assert [f.rule for f in run_rules(indexed, IXSCAN, Catalog(STATUS_1))] == ["negation_predicate"]
    residual = ExplainResult(("FETCH", "IXSCAN"), ("status_1",), frozenset({"flag"}))
    assert [f.rule for f in run_rules(indexed, residual, Catalog(STATUS_1))] == [
        "negation_predicate"
    ]


def test_negation_with_a_positive_predicate_is_not_owned() -> None:
    stats = _stats({"status": {"$ne": "done"}, "customer_id": 7}, returned=1000)
    assert run_rules(stats, IXSCAN, Catalog(STATUS_1)) == []
    assert not negation_only({"$or": [{"a": {"$ne": 1}}]})
    assert not negation_only({})
    assert not negation_only({"a": {"$ne": 1, "$gt": 0}})
    assert negation_only(
        {"a": {"$nin": ["?"]}, "b": {"$not": {"$gt": "?"}}, "c": {"$exists": False}}
    )


def test_negation_with_a_sort_still_gets_sort_advice() -> None:
    stats = _stats({"status": {"$ne": "done"}}, {"created": -1}, has_sort_stage=True, returned=50)
    findings = run_rules(stats, IXSCAN_SORT, Catalog(STATUS_1))
    assert [f.rule for f in findings] == ["sort_in_memory", "negation_predicate"]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.keys == (("created", -1),)


def test_large_in_is_additive() -> None:
    stats = _stats({"customer_id": {"$in": list(range(250))}}, count=2000, docs=5000, returned=250)
    findings = run_rules(stats, COLLSCAN, Catalog())
    assert [f.rule for f in findings] == ["collscan", "large_in"]
    large = findings[1]
    assert large.severity == "medium" and large.evidence == {"in_size": 250}
    assert large.recommendation is not None and large.recommendation.kind == "rewrite"
    ok = _stats({"a": {"$in": list(range(200))}}, returned=1000)
    assert run_rules(ok, IXSCAN, Catalog()) == []


def _pipeline_stats(pipeline: list[dict[str, Any]]) -> ShapeStats:
    command = {"aggregate": "orders", "pipeline": pipeline}
    shape, meta = normalize("app.orders", "aggregate", command)
    base = _stats({"customer_id": 7}, returned=1000)
    return ShapeStats(**{**base.__dict__, "shape": shape, "meta": meta, "sample": command})


LOOKUP_EMAIL = {
    "$lookup": {"from": "customers", "localField": "email", "foreignField": "email", "as": "c"}
}


def test_lookup_without_index_on_foreign_field() -> None:
    stats = _pipeline_stats([{"$match": {"customer_id": 7}}, LOOKUP_EMAIL])
    customers_id = IndexInfo("app.customers", "_id_", (("_id", 1),))
    findings = run_rules(stats, IXSCAN, Catalog(STATUS_1, customers_id))
    assert [f.rule for f in findings] == ["lookup_no_index"]
    finding = findings[0]
    assert finding.recommendation is not None
    assert finding.recommendation.ns == "app.customers"
    assert finding.recommendation.statement.startswith("db.customers.createIndex({email: 1}")
    assert finding.evidence == {"from": "customers", "foreignField": "email"}
    assert "scans customers" in finding.message


def test_lookup_with_an_index_or_on_id_is_fine() -> None:
    on_id = {
        "$lookup": {
            "from": "customers",
            "localField": "customer_id",
            "foreignField": "_id",
            "as": "c",
        }
    }
    assert run_rules(_pipeline_stats([on_id]), IXSCAN, Catalog()) == []
    email = IndexInfo("app.customers", "email_1", (("email", 1), ("x", 1)))
    assert run_rules(_pipeline_stats([LOOKUP_EMAIL]), IXSCAN, Catalog(email)) == []
    pipeline_form = {"$lookup": {"from": "customers", "pipeline": [], "as": "c"}}
    assert run_rules(_pipeline_stats([pipeline_form]), IXSCAN, Catalog()) == []


def test_lookup_rule_needs_a_known_catalog() -> None:
    assert run_rules(_pipeline_stats([LOOKUP_EMAIL]), None, NoIndexes()) == []
