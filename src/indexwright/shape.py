from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from bson import Regex

from indexwright.model import Shape, ShapeMeta

LOGICAL = frozenset({"$and", "$or", "$nor"})
LIST_OPERATORS = frozenset({"$in", "$nin", "$all"})
# Stages that leave documents unchanged, so a $match or $sort after them can still use an index.
TRANSPARENT_STAGES = frozenset({"$match", "$sort", "$limit", "$skip"})
PLACEHOLDER = "?"


class _Meta:
    def __init__(self) -> None:
        self.in_size = 0
        self.unanchored: set[str] = set()
        self.projection = False

    def regex(self, field: str, pattern: Any) -> None:
        text = pattern.pattern if isinstance(pattern, Regex | re.Pattern) else str(pattern)
        if not text.startswith("^"):
            self.unanchored.add(field)

    def frozen(self) -> ShapeMeta:
        return ShapeMeta(self.in_size, frozenset(self.unanchored), self.projection)


def normalize(ns: str, op: str, command: dict[str, Any]) -> tuple[Shape, ShapeMeta]:
    meta = _Meta()
    pipeline: list[dict[str, Any]] | None = None
    if op == "aggregate":
        pipeline = [_stage(stage, meta) for stage in command.get("pipeline", [])]
        filter_, sort = _indexable_part(pipeline)
    else:
        raw_filter, raw_sort, projection = _extract(op, command)
        filter_ = _filter(raw_filter, meta)
        sort = _sort(raw_sort)
        meta.projection = bool(projection)
    canonical = _canonical({"op": op, "filter": filter_, "sort": sort, "pipeline": pipeline})
    fingerprint = hashlib.sha1(canonical.encode()).hexdigest()
    return Shape(ns, op, filter_, sort, pipeline, fingerprint), meta.frozen()


def _extract(op: str, command: dict[str, Any]) -> tuple[Any, Any, Any]:
    if op == "find":
        return command.get("filter"), command.get("sort"), command.get("projection")
    if op in ("count", "distinct"):
        return command.get("query"), None, None
    if op in ("update", "delete"):
        return command.get("q"), None, None
    if op == "findAndModify":
        return command.get("query"), command.get("sort"), command.get("fields")
    return None, None, None


def _filter(doc: Any, meta: _Meta) -> dict[str, Any]:
    if not isinstance(doc, dict):
        return {}
    out: dict[str, Any] = {}
    for key, value in doc.items():
        if key == "$comment":
            continue
        if key in LOGICAL and isinstance(value, list):
            out[key] = sorted((_filter(clause, meta) for clause in value), key=_canonical)
        elif key.startswith("$"):
            out[key] = PLACEHOLDER
        else:
            out[key] = _predicate(value, key, meta)
    return dict(sorted(out.items()))


def _predicate(value: Any, field: str, meta: _Meta) -> Any:
    if isinstance(value, Regex | re.Pattern):
        meta.regex(field, value)
        return {"$regex": PLACEHOLDER}
    if isinstance(value, dict) and any(k.startswith("$") for k in value):
        return _operators(value, field, meta)
    if isinstance(value, list):
        return [PLACEHOLDER]
    return PLACEHOLDER


def _operators(ops: dict[str, Any], field: str, meta: _Meta) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for op, arg in ops.items():
        if op == "$options":
            continue
        if op in LIST_OPERATORS:
            if op == "$in" and isinstance(arg, list):
                meta.in_size = max(meta.in_size, len(arg))
            out[op] = [PLACEHOLDER]
        elif op == "$regex":
            meta.regex(field, arg)
            out[op] = PLACEHOLDER
        elif op == "$exists":
            out[op] = bool(arg)
        elif op == "$not":
            out[op] = _predicate(arg, field, meta)
        elif op == "$elemMatch":
            out[op] = _elem_match(arg, field, meta)
        else:
            out[op] = PLACEHOLDER
    return dict(sorted(out.items()))


def _elem_match(arg: Any, field: str, meta: _Meta) -> Any:
    if isinstance(arg, dict) and arg and all(k.startswith("$") and k not in LOGICAL for k in arg):
        return _operators(arg, field, meta)
    return _filter(arg, meta)


def _sort(spec: Any) -> list[list[Any]]:
    if not isinstance(spec, dict):
        return []
    return [[field, direction] for field, direction in spec.items()]


def _stage(stage: dict[str, Any], meta: _Meta) -> dict[str, Any]:
    name, body = next(iter(stage.items()))
    if name == "$match":
        return {name: _filter(body, meta)}
    if name == "$sort":
        return {name: _sort(body)}
    if name == "$lookup" and isinstance(body, dict):
        return {name: _lookup(body)}
    if name == "$group" and isinstance(body, dict):
        return {name: {"_id": _group_id(body.get("_id"))}}
    return {name: PLACEHOLDER}


def _lookup(body: dict[str, Any]) -> dict[str, Any]:
    if "pipeline" in body:
        return {"from": body.get("from"), "pipeline": PLACEHOLDER}
    return {
        "foreignField": body.get("foreignField"),
        "from": body.get("from"),
        "localField": body.get("localField"),
    }


def _group_id(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, str) and value.startswith("$"):
        return value
    if isinstance(value, dict) and not any(k.startswith("$") for k in value):
        return {k: _group_id(v) for k, v in sorted(value.items())}
    return PLACEHOLDER


def _indexable_part(pipeline: list[dict[str, Any]]) -> tuple[dict[str, Any], list[list[Any]]]:
    filter_: dict[str, Any] | None = None
    sort: list[list[Any]] | None = None
    for stage in pipeline:
        name, body = next(iter(stage.items()))
        if name not in TRANSPARENT_STAGES:
            break
        if name == "$match" and filter_ is None:
            filter_ = body
        elif name == "$sort" and sort is None:
            sort = body
    return filter_ or {}, sort or []


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compact(shape: Shape) -> str:
    if shape.pipeline is not None:
        return _compact(shape.pipeline)
    text = _compact(shape.filter)
    if shape.sort:
        text += f" sort {_compact(shape.sort)}"
    return text


def _compact(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}:{_compact(v)}" for k, v in value.items()) + "}"
    if isinstance(value, list):
        if value and all(isinstance(item, list) and len(item) == 2 for item in value):
            return "{" + ", ".join(f"{k}:{_compact(v)}" for k, v in value) + "}"
        return "[" + ", ".join(_compact(item) for item in value) + "]"
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)
