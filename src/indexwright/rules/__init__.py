from __future__ import annotations

from typing import TYPE_CHECKING

from indexwright.model import SEVERITY_ORDER
from indexwright.rules import (
    collscan,
    docs_examined_ratio,
    index_rules,
    lookup,
    low_selectivity_index,
    predicates,
    sort_in_memory,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from indexwright.model import CollectionIndexes, ExplainResult, Finding, ShapeStats
    from indexwright.rules.base import Indexes, Rule

    IndexRule = Callable[[CollectionIndexes, set[str]], list[Finding]]

RULES: list[tuple[str, Rule]] = [
    ("collscan", collscan.check),
    ("sort_in_memory", sort_in_memory.check),
    ("docs_examined_ratio", docs_examined_ratio.check),
    ("low_selectivity_index", low_selectivity_index.check),
    ("unanchored_regex", predicates.unanchored_regex),
    ("negation_predicate", predicates.negation_predicate),
    ("large_in", predicates.large_in),
    ("lookup_no_index", lookup.lookup_no_index),
]


INDEX_RULES: list[tuple[str, IndexRule]] = [
    ("duplicate_index", index_rules.duplicate_index),
    ("redundant_index", index_rules.redundant_index),
    ("unused_index", index_rules.unused_index),
    ("too_many_indexes", index_rules.too_many_indexes),
]


def run_index_rules(coll: CollectionIndexes) -> list[Finding]:
    findings: list[Finding] = []
    dropped: set[str] = set()
    for _, rule in INDEX_RULES:
        new = rule(coll, dropped)
        findings.extend(new)
        dropped.update(str(f.evidence["index"]) for f in new if "index" in f.evidence)
    return findings


def run_rules(stats: ShapeStats, explain: ExplainResult | None, catalog: Indexes) -> list[Finding]:
    return [finding for _, rule in RULES for finding in rule(stats, explain, catalog)]


def sort_findings(findings: list[Finding], weight: dict[str, int]) -> list[Finding]:
    return sorted(findings, key=lambda f: (SEVERITY_ORDER[f.severity], -weight.get(f.shape_id, 0)))
