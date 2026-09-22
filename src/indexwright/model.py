from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from datetime import datetime

Op = str
OPS = frozenset({"find", "count", "distinct", "update", "delete", "findAndModify", "aggregate"})

Severity = Literal["critical", "high", "medium", "low", "info"]
SEVERITY_ORDER: dict[str, int] = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}


@dataclass(frozen=True)
class Entry:
    ns: str
    op: Op
    ts: datetime
    millis: int
    docs_examined: int
    keys_examined: int
    nreturned: int | None
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
    # count, distinct, $group, skip: returned documents are not the matched documents, so
    # ratios over nreturned say nothing about the index.
    output_reduced: bool = False

    def merge(self, other: ShapeMeta) -> ShapeMeta:
        return ShapeMeta(
            max(self.in_size, other.in_size),
            self.unanchored_regex | other.unanchored_regex,
            self.projection_present or other.projection_present,
            self.output_reduced or other.output_reduced,
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
    # distinct entries carry no returned count, so a ratio over nreturned would be meaningless.
    returned_known: bool = True
    # One real command of this shape, with its values. Only used to run explain; never reported.
    sample: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def docs_per_returned(self) -> float:
        return self.docs_examined / max(self.nreturned, 1)

    @property
    def keys_per_returned(self) -> float:
        return self.keys_examined / max(self.nreturned, 1)


@dataclass(frozen=True)
class IndexInfo:
    ns: str
    name: str
    keys: tuple[tuple[str, Any], ...]
    unique: bool = False
    sparse: bool = False
    partial: bool = False
    hidden: bool = False
    ttl: bool = False
    partial_filter: str | None = None
    collation: str | None = None


@dataclass(frozen=True)
class IndexUsage:
    ns: str
    name: str
    ops: int
    since: datetime
    members_reported: int


@dataclass(frozen=True)
class CollectionIndexes:
    ns: str
    indexes: list[IndexInfo]
    usage: dict[str, IndexUsage] | None
    members_total: int
    members_reached: int
    write_share: float | None
    unused_days: int


@dataclass(frozen=True)
class ExplainResult:
    stages: tuple[str, ...] = ()
    index_names: tuple[str, ...] = ()
    fetch_filter_fields: frozenset[str] = frozenset()
    error: str | None = None

    def has(self, stage: str) -> bool:
        return stage in self.stages


@dataclass(frozen=True)
class Recommendation:
    kind: Literal["create_index", "drop_index", "rewrite"]
    ns: str
    statement: str
    keys: tuple[tuple[str, Any], ...] = ()


@dataclass(frozen=True)
class Finding:
    severity: Severity
    rule: str
    ns: str
    shape_id: str
    message: str
    recommendation: Recommendation | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
