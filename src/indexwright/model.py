from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from datetime import datetime

Op = str
OPS = frozenset({"find", "count", "distinct", "update", "delete", "findAndModify", "aggregate"})


@dataclass(frozen=True)
class Entry:
    ns: str
    op: Op
    ts: datetime
    millis: int
    docs_examined: int
    keys_examined: int
    nreturned: int
    has_sort_stage: bool
    plan_summary: str
    command: dict[str, Any]
    getmore: bool = False


@dataclass(frozen=True)
class Shape:
    ns: str
    op: Op
    filter: dict[str, Any]
    sort: list[list[Any]]
    pipeline: list[dict[str, Any]] | None
    fingerprint: str


@dataclass(frozen=True)
class ShapeMeta:
    in_size: int = 0
    unanchored_regex: frozenset[str] = frozenset()
    projection_present: bool = False

    def merge(self, other: ShapeMeta) -> ShapeMeta:
        return ShapeMeta(
            max(self.in_size, other.in_size),
            self.unanchored_regex | other.unanchored_regex,
            self.projection_present or other.projection_present,
        )


@dataclass(frozen=True)
class ShapeStats:
    shape: Shape
    count: int
    p50_ms: int
    p99_ms: int
    max_ms: int
    total_ms: int
    docs_examined: int
    keys_examined: int
    nreturned: int
    has_sort_stage: bool
    plan_summaries: frozenset[str]
    meta: ShapeMeta
    first_seen: datetime
    last_seen: datetime

    @property
    def docs_per_returned(self) -> float:
        return self.docs_examined / max(self.nreturned, 1)

    @property
    def keys_per_returned(self) -> float:
        return self.keys_examined / max(self.nreturned, 1)
