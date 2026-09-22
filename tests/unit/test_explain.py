from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pymongo.errors import OperationFailure

from indexwright.explain import Explainer, explain_command, filter_fields, parse_explain
from indexwright.model import ShapeMeta, ShapeStats
from indexwright.shape import normalize

FIXTURES = Path(__file__).with_name("explain_fixtures")


def _fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text())
    return data


@pytest.mark.parametrize("version", ["mongo7", "mongo8"])
class TestParseFixtures:
    def test_collscan(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_collscan"))
        assert result.stages == ("COLLSCAN",)
        assert result.index_names == ()
        assert result.error is None

    def test_ixscan_with_residual_filter(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_ixscan_fetch_filter"))
        assert result.stages == ("FETCH", "IXSCAN")
        assert result.index_names == ("status_1",)
        assert result.fetch_filter_fields == {"total"}

    def test_ixscan_without_residual_filter(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_low_selectivity"))
        assert result.stages == ("FETCH", "IXSCAN")
        assert result.index_names == ("created_1_email_1",)
        assert result.fetch_filter_fields == frozenset()

    def test_sort_in_memory(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_sort_in_memory"))
        assert result.has("SORT") and result.has("IXSCAN") and not result.has("COLLSCAN")

    def test_aggregate_sort(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_aggregate_match_sort_group"))
        assert result.has("SORT") and result.index_names == ("status_1",)

    def test_or_is_a_collscan(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_or"))
        assert result.stages == ("SUBPLAN", "COLLSCAN")

    def test_update_and_delete(self, version: str) -> None:
        update = parse_explain(_fixture(f"{version}_update"))
        assert update.stages == ("UPDATE", "FETCH", "IXSCAN")
        assert update.fetch_filter_fields == {"total"}
        delete = parse_explain(_fixture(f"{version}_delete"))
        assert delete.stages[0] == "DELETE" and delete.index_names == ("status_1",)

    def test_id_lookup_uses_the_id_index(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_find_id"))
        assert result.stages in (("IDHACK",), ("EXPRESS_IXSCAN",))
        assert not result.has("COLLSCAN")

    def test_lookup(self, version: str) -> None:
        result = parse_explain(_fixture(f"{version}_aggregate_lookup"))
        assert "customer_id_1" in result.index_names


def test_parse_sharded_plan() -> None:
    raw = {
        "queryPlanner": {
            "winningPlan": {
                "stage": "SHARD_MERGE",
                "shards": [
                    {"shardName": "a", "winningPlan": {"stage": "COLLSCAN"}},
                    {
                        "shardName": "b",
                        "winningPlan": {
                            "stage": "FETCH",
                            "inputStage": {"stage": "IXSCAN", "indexName": "x_1"},
                        },
                    },
                ],
            }
        }
    }
    result = parse_explain(raw)
    assert result.stages == ("SHARD_MERGE", "COLLSCAN", "FETCH", "IXSCAN")
    assert result.index_names == ("x_1",)


def test_parse_without_winning_plan() -> None:
    assert parse_explain({"ok": 1}).error is not None
    assert parse_explain({"stages": [{"$group": {}}]}).error is not None


def test_filter_fields_collects_nested_field_names() -> None:
    doc = {"$and": [{"a": {"$gt": 1}}, {"$or": [{"b": 1}, {"c.d": {"$in": [1]}}]}], "e": 1}
    assert filter_fields(doc) == {"a", "b", "c.d", "e"}


def test_explain_command_strips_session_fields() -> None:
    sample = {
        "find": "orders",
        "filter": {"a": 1},
        "lsid": {"id": 1},
        "$db": "app",
        "$clusterTime": {},
        "readConcern": {"level": "local"},
        "maxTimeMS": 5,
    }
    assert explain_command("find", "app.orders", sample) == {"find": "orders", "filter": {"a": 1}}


def test_writes_are_explained_as_the_equivalent_find() -> None:
    update = explain_command(
        "update", "app.orders", {"q": {"a": 1}, "u": {"$set": {"b": 1}}, "multi": True}
    )
    assert update == {"find": "orders", "filter": {"a": 1}}
    delete = explain_command("delete", "app.orders", {"q": {"a": 1}, "limit": 1})
    assert delete == {"find": "orders", "filter": {"a": 1}}
    modify = explain_command(
        "findAndModify",
        "app.orders",
        {"findAndModify": "orders", "query": {"a": 1}, "sort": {"b": 1}, "update": {"$set": {}}},
    )
    assert modify == {"find": "orders", "filter": {"a": 1}, "limit": 1, "sort": {"b": 1}}


def test_explain_command_rejects_empty_or_foreign_samples() -> None:
    assert explain_command("find", "app.orders", {}) is None
    assert explain_command("find", "app.orders", {"count": "orders"}) is None


class FakeConn:
    def __init__(self, response: dict[str, Any] | Exception) -> None:
        self.response = response
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def command(self, db: str, spec: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((db, spec))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def _stats(command: dict[str, Any], op: str = "find", ns: str = "app.orders") -> ShapeStats:
    shape, meta = normalize(ns, op, command)
    now = datetime(2026, 9, 22, tzinfo=UTC)
    return ShapeStats(
        shape, 1, 1, 1, 1, 1, 10, 0, 1, False, frozenset(), meta, now, now, sample=command
    )


def _explainer(conn: Any, **kw: Any) -> tuple[Explainer, list[float]]:
    slept: list[float] = []
    tick = [0.0]

    def now() -> float:
        return tick[0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        tick[0] += seconds

    return Explainer(conn, sleep=sleep, clock=now, **kw), slept


def test_explainer_runs_and_caches() -> None:
    conn = FakeConn(_fixture("mongo7_find_collscan"))
    explainer, _ = _explainer(conn)
    stats = _stats({"find": "orders", "filter": {"email": {"$regex": "x"}}})
    first = explainer.explain(stats)
    second = explainer.explain(stats)
    assert first is second and first is not None and first.has("COLLSCAN")
    assert len(conn.calls) == 1
    db, spec = conn.calls[0]
    assert db == "app"
    assert spec == {
        "explain": {"find": "orders", "filter": {"email": {"$regex": "x"}}},
        "verbosity": "queryPlanner",
    }


def test_explainer_throttles_between_calls() -> None:
    conn = FakeConn(_fixture("mongo7_find_collscan"))
    explainer, slept = _explainer(conn, max_per_second=5)
    for i in range(3):
        explainer.explain(_stats({"find": "orders", "filter": {"a": i, f"f{i}": 1}}))
    assert len(conn.calls) == 3
    assert slept == [pytest.approx(0.2), pytest.approx(0.2)]


def test_explainer_stops_at_budget() -> None:
    conn = FakeConn(_fixture("mongo7_find_collscan"))
    explainer, _ = _explainer(conn, max_total=2)
    results = [
        explainer.explain(_stats({"find": "orders", "filter": {f"f{i}": 1}})) for i in range(4)
    ]
    assert [r is not None for r in results] == [True, True, False, False]
    assert explainer.executed == 2


def test_explainer_reports_server_errors_without_raising() -> None:
    conn = FakeConn(OperationFailure("no such command", code=59, details={"errmsg": "boom"}))
    explainer, _ = _explainer(conn)
    result = explainer.explain(_stats({"find": "orders", "filter": {"$where": "1"}}))
    assert result is not None and result.error == "boom" and result.stages == ()


def test_explainer_skips_shapes_without_sample() -> None:
    conn = FakeConn(_fixture("mongo7_find_collscan"))
    explainer, _ = _explainer(conn)
    shape, _ = normalize("app.orders", "find", {"find": "orders"})
    now = datetime(2026, 9, 22, tzinfo=UTC)
    stats = ShapeStats(shape, 1, 1, 1, 1, 1, 1, 0, 1, False, frozenset(), ShapeMeta(), now, now)
    assert explainer.explain(stats) is None and conn.calls == []
