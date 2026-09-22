from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from indexwright.model import ShapeMeta, ShapeStats
from indexwright.shape import normalize

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from indexwright.model import Entry, Shape


def group(entries: Iterable[Entry]) -> list[ShapeStats]:
    buckets: dict[tuple[str, str], _Bucket] = {}
    for entry in entries:
        shape, meta = normalize(entry.ns, entry.op, entry.command)
        key = (shape.ns, shape.fingerprint)
        if key not in buckets:
            buckets[key] = _Bucket(shape, entry.ts)
        buckets[key].add(entry, meta)
    return sorted((b.stats() for b in buckets.values()), key=lambda s: s.total_ms, reverse=True)


def percentile(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = math.ceil(p / 100 * len(ordered))
    return ordered[max(rank, 1) - 1]


class _Bucket:
    def __init__(self, shape: Shape, ts: datetime) -> None:
        self.shape = shape
        self.sample: dict[str, Any] = {}
        self.millis: list[int] = []
        self.total_ms = 0
        self.docs_examined = 0
        self.keys_examined = 0
        self.nreturned = 0
        self.has_sort_stage = False
        self.plan_summaries: set[str] = set()
        self.meta = ShapeMeta()
        self.first_seen = ts
        self.last_seen = ts

    def add(self, entry: Entry, meta: ShapeMeta) -> None:
        # A getMore is a continuation of the same query: it adds work, not another execution.
        if not entry.getmore:
            self.millis.append(entry.millis)
        if not self.sample:
            self.sample = entry.command
        self.total_ms += entry.millis
        self.docs_examined += entry.docs_examined
        self.keys_examined += entry.keys_examined
        self.nreturned += entry.nreturned
        self.has_sort_stage = self.has_sort_stage or entry.has_sort_stage
        if entry.plan_summary:
            self.plan_summaries.add(entry.plan_summary)
        self.meta = self.meta.merge(meta)
        self.first_seen = min(self.first_seen, entry.ts)
        self.last_seen = max(self.last_seen, entry.ts)

    def stats(self) -> ShapeStats:
        return ShapeStats(
            shape=self.shape,
            count=len(self.millis),
            p50_ms=percentile(self.millis, 50),
            p99_ms=percentile(self.millis, 99),
            max_ms=max(self.millis, default=0),
            total_ms=self.total_ms,
            docs_examined=self.docs_examined,
            keys_examined=self.keys_examined,
            nreturned=self.nreturned,
            has_sort_stage=self.has_sort_stage,
            plan_summaries=frozenset(self.plan_summaries),
            meta=self.meta,
            first_seen=self.first_seen,
            last_seen=self.last_seen,
            sample=self.sample,
        )
