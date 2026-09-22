from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from pymongo.errors import OperationFailure

from indexwright.model import ExplainResult

if TYPE_CHECKING:
    from collections.abc import Callable

    from indexwright.model import ShapeStats
    from indexwright.mongo import Connection

log = logging.getLogger(__name__)

# Session and cluster fields the profiler records with a command but explain must not see.
STRIPPED_KEYS = frozenset(
    {
        "lsid",
        "txnNumber",
        "autocommit",
        "startTransaction",
        "readConcern",
        "writeConcern",
        "maxTimeMS",
        "apiVersion",
        "apiStrict",
        "apiDeprecationErrors",
    }
)
CHILD_KEYS = (
    "inputStage",
    "inputStages",
    "queryPlan",
    "innerStage",
    "outerStage",
    "thenStage",
    "elseStage",
    "shards",
    "winningPlan",
)


class Explainer:
    def __init__(
        self,
        conn: Connection,
        *,
        max_per_second: float = 5.0,
        max_total: int = 200,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.conn = conn
        self.min_interval = 1.0 / max_per_second
        self.max_total = max_total
        self.sleep = sleep
        self.clock = clock
        self.executed = 0
        self.cache: dict[tuple[str, str], ExplainResult] = {}
        self._last = float("-inf")
        self._budget_warned = False

    def explain(self, stats: ShapeStats) -> ExplainResult | None:
        key = (stats.shape.ns, stats.shape.fingerprint)
        if key in self.cache:
            return self.cache[key]
        if self.executed >= self.max_total:
            if not self._budget_warned:
                log.warning(
                    "explain budget of %d reached, remaining shapes are not explained",
                    self.max_total,
                )
                self._budget_warned = True
            return None
        command = explain_command(stats.shape.op, stats.shape.ns, stats.sample)
        if command is None:
            return None
        self._throttle()
        db = stats.shape.ns.split(".", 1)[0]
        self.executed += 1
        try:
            raw = self.conn.command(db, {"explain": command, "verbosity": "queryPlanner"})
            result = parse_explain(raw)
        except OperationFailure as exc:
            log.warning(
                "explain failed for shape %s: code %s", stats.shape.fingerprint[:10], exc.code
            )
            result = ExplainResult(
                error=str(exc.details.get("errmsg", exc)) if exc.details else str(exc)
            )
        self.cache[key] = result
        return result

    def _throttle(self) -> None:
        wait = self._last + self.min_interval - self.clock()
        if wait > 0:
            self.sleep(wait)
        self._last = self.clock()


def explain_command(op: str, ns: str, sample: dict[str, Any]) -> dict[str, Any] | None:
    if not sample:
        return None
    coll = ns.split(".", 1)[1]
    clean = {k: v for k, v in sample.items() if k not in STRIPPED_KEYS and not k.startswith("$")}
    # A read-only user may not explain writes. The query part of a write plans exactly like
    # a find with the same filter, so writes are explained that way.
    if op in ("update", "delete"):
        return {"find": coll, "filter": clean.get("q", {}), **_pick(clean, "collation")}
    if op == "findAndModify":
        find = {"find": coll, "filter": clean.get("query", {}), "limit": 1}
        return {**find, **_pick(clean, "sort", "collation")}
    return clean if op in clean else None


def _pick(source: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: source[k] for k in keys if k in source}


def parse_explain(raw: dict[str, Any]) -> ExplainResult:
    planner = raw.get("queryPlanner")
    if planner is None and raw.get("stages"):
        planner = raw["stages"][0].get("$cursor", {}).get("queryPlanner")
    if not isinstance(planner, dict) or "winningPlan" not in planner:
        return ExplainResult(error="no winning plan in explain output")
    stages: list[str] = []
    indexes: list[str] = []
    fetch_filter_fields: set[str] = set()
    _walk(planner["winningPlan"], stages, indexes, fetch_filter_fields)
    return ExplainResult(tuple(stages), tuple(indexes), frozenset(fetch_filter_fields))


def _walk(node: Any, stages: list[str], indexes: list[str], fetch_fields: set[str]) -> None:
    if isinstance(node, list):
        for item in node:
            _walk(item, stages, indexes, fetch_fields)
        return
    if not isinstance(node, dict):
        return
    stage = node.get("stage")
    if isinstance(stage, str):
        stages.append(stage)
        if "indexName" in node:
            indexes.append(str(node["indexName"]))
        if stage == "FETCH" and isinstance(node.get("filter"), dict):
            fetch_fields.update(filter_fields(node["filter"]))
    for key in CHILD_KEYS:
        if key in node:
            _walk(node[key], stages, indexes, fetch_fields)


def filter_fields(doc: Any) -> set[str]:
    fields: set[str] = set()
    if isinstance(doc, dict):
        for key, value in doc.items():
            if key.startswith("$"):
                fields |= filter_fields(value)
            else:
                fields.add(key)
    elif isinstance(doc, list):
        for item in doc:
            fields |= filter_fields(item)
    return fields
