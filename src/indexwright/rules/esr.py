from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from indexwright.model import Recommendation

if TYPE_CHECKING:
    from indexwright.model import IndexInfo, Shape

EQUALITY_OPERATORS = frozenset({"$eq", "$in", "$all"})
RANGE_OPERATORS = frozenset({"$gt", "$gte", "$lt", "$lte", "$regex"})
_IDENTIFIER = re.compile(r"^[A-Za-z_$][A-Za-z0-9_$]*$")

Keys = tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class Candidate:
    keys: Keys
    equality: int


@dataclass(frozen=True)
class Advice:
    recommendations: list[Recommendation]
    covered: list[str]

    @property
    def empty(self) -> bool:
        return not self.recommendations and not self.covered


def advise(shape: Shape, indexes: list[IndexInfo]) -> Advice:
    recommendations: list[Recommendation] = []
    covered: list[str] = []
    for candidate in candidates(shape):
        existing = covered_by(candidate, indexes)
        if existing is None:
            recommendations.append(recommendation(shape.ns, candidate.keys))
        else:
            covered.append(f"{existing.name} already covers {format_keys(candidate.keys)}")
    return Advice(recommendations, covered)


def candidates(shape: Shape) -> list[Candidate]:
    result: list[Candidate] = []
    for fields in _branches(shape.filter):
        candidate = _candidate(fields, shape.sort)
        if candidate.keys and candidate not in result:
            result.append(candidate)
    return result


def _branches(filter_: dict[str, Any]) -> list[dict[str, Any]]:
    fields, ors = _split(filter_)
    if not ors:
        return [fields]
    branches = []
    for clause in ors[0]:
        clause_fields, _ = _split(clause)
        branches.append({**fields, **clause_fields})
    return branches


def _split(doc: dict[str, Any]) -> tuple[dict[str, Any], list[list[dict[str, Any]]]]:
    fields: dict[str, Any] = {}
    ors: list[list[dict[str, Any]]] = []
    for key, value in doc.items():
        if key == "$and":
            for clause in value:
                clause_fields, clause_ors = _split(clause)
                fields.update(clause_fields)
                ors.extend(clause_ors)
        elif key == "$or":
            ors.append(value)
        elif not key.startswith("$"):
            fields[key] = value
    return fields, ors


def classify(value: Any) -> str | None:
    if not isinstance(value, dict):
        return "equality"
    operators = set(value)
    if operators & EQUALITY_OPERATORS:
        return "equality"
    if value.get("$exists") is True or operators & RANGE_OPERATORS:
        return "range"
    return None


def _candidate(fields: dict[str, Any], sort: list[list[Any]]) -> Candidate:
    kinds = {field: classify(value) for field, value in fields.items()}
    equality = sorted(f for f, kind in kinds.items() if kind == "equality")
    if "_id" in equality:
        return Candidate((), 0)
    keys: list[tuple[str, Any]] = [(f, 1) for f in equality]
    sorted_fields: set[str] = set()
    for field, direction in sort:
        if direction in (1, -1):
            sorted_fields.add(field)
            if field not in equality:
                keys.append((field, direction))
    ranges = sorted(f for f, kind in kinds.items() if kind == "range")
    keys += [(f, 1) for f in ranges if f not in sorted_fields and f not in equality]
    return Candidate(tuple(keys), len(equality))


def covered_by(candidate: Candidate, indexes: list[IndexInfo]) -> IndexInfo | None:
    keys = candidate.keys
    for index in indexes:
        if index.partial or index.hidden or len(index.keys) < len(keys):
            continue
        prefix = index.keys[: len(keys)]
        names_match = all(name == field for (name, _), (field, _) in zip(prefix, keys, strict=True))
        if not names_match or not all(isinstance(d, int) for _, d in prefix):
            continue
        # Equality keys work in either direction; sort and range keys must all match or all flip.
        suffix = list(zip(prefix[candidate.equality :], keys[candidate.equality :], strict=True))
        same = all(have == want for (_, have), (_, want) in suffix)
        flipped = all(_flip(have) == want for (_, have), (_, want) in suffix)
        if same or flipped:
            return index
    return None


def _flip(direction: Any) -> Any:
    return -direction if isinstance(direction, int) else None


def recommendation(ns: str, keys: Keys) -> Recommendation:
    coll = ns.split(".", 1)[1]
    digest = hashlib.sha1(json.dumps(keys).encode()).hexdigest()[:6]
    name = f"{coll}_esr_{digest}"
    statement = f'db.{coll}.createIndex({format_keys(keys)}, {{name: "{name}"}})'
    return Recommendation("create_index", ns, statement, keys)


def format_keys(keys: Keys) -> str:
    parts = []
    for field, direction in keys:
        label = field if _IDENTIFIER.match(field) else json.dumps(field)
        value = direction if isinstance(direction, int) else json.dumps(direction)
        parts.append(f"{label}: {value}")
    return "{" + ", ".join(parts) + "}"
