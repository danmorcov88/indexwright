from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rich.console import Console

from indexwright import __version__
from indexwright.analysis import Analysis
from indexwright.doctor import Check
from indexwright.model import Finding, IndexInfo, Recommendation, ShapeStats
from indexwright.report import Source, render_json, render_markdown, render_table
from indexwright.rules import run_rules
from indexwright.rules.esr import recommendation
from indexwright.shape import normalize

NOW = datetime(2026, 9, 22, 10, 0, tzinfo=UTC)
SINCE = datetime(2026, 9, 21, 10, 0, tzinfo=UTC)
SECRET = "user42@example.com"


class Catalog:
    known = True

    def __init__(self, *indexes: IndexInfo) -> None:
        self.indexes = list(indexes)

    def for_ns(self, ns: str) -> list[IndexInfo]:
        return [i for i in self.indexes if i.ns == ns]


def _stats(command: dict[str, Any], op: str = "find", **counters: int) -> ShapeStats:
    shape, meta = normalize("app.orders", op, command)
    docs = counters.get("docs", 5000)
    returned = counters.get("returned", 10)
    return ShapeStats(
        shape=shape,
        count=50,
        p50_ms=5,
        p99_ms=40,
        max_ms=60,
        total_ms=300,
        docs_examined=docs * 50,
        keys_examined=0,
        nreturned=returned * 50,
        has_sort_stage=False,
        plan_summaries=frozenset({"COLLSCAN"}),
        meta=meta,
        first_seen=SINCE,
        last_seen=NOW,
        sample=command,
    )


def _analysis() -> Analysis:
    regex = _stats({"find": "orders", "filter": {"email": {"$regex": SECRET}}})
    negation = _stats({"find": "orders", "filter": {"status": {"$ne": SECRET}}})
    large = _stats({"find": "orders", "filter": {"customer_id": {"$in": list(range(300))}}})
    findings: list[Finding] = []
    for stats in (regex, negation, large):
        findings.extend(run_rules(stats, None, Catalog()))
    findings.append(
        Finding(
            "low",
            "unused_index",
            "app.orders",
            "",
            "tags_1 {tags: 1} has not been used in 40 days",
            Recommendation("drop_index", "app.orders", 'db.orders.dropIndex("tags_1")'),
            {"index": "tags_1"},
        )
    )
    findings.append(
        Finding(
            "medium",
            "collscan",
            "app.orders",
            regex.shape.fingerprint,
            "same index wanted twice",
            recommendation("app.orders", (("email", 1),)),
            {},
        )
    )
    checks = [Check("server version", "ok", "7.0.1"), Check("profiler app", "warn", "level 0")]
    return Analysis(
        checks=checks,
        shapes=[regex, negation, large],
        findings=findings,
        entries_read=150,
        explains=3,
        members_total=3,
        members_reached=2,
        unreachable=["c:27017"],
    )


SOURCE = Source("profiler", SINCE, databases=["app"])


def test_json_report_structure_and_privacy() -> None:
    text = render_json(_analysis(), SOURCE, NOW)
    doc = json.loads(text)
    assert doc["schema_version"] == 1 and doc["tool_version"] == __version__
    assert doc["generated_at"] == "2026-09-22T10:00:00+00:00"
    assert doc["source"] == {
        "kind": "profiler",
        "since": "2026-09-21T10:00:00+00:00",
        "databases": ["app"],
        "files": None,
    }
    assert doc["summary"] == {
        "entries_read": 150,
        "shapes": 3,
        "explains": 3,
        "findings": 7,
        "offline": False,
        "members": {"total": 3, "reached": 2, "unreachable": ["c:27017"]},
    }
    assert [s["shape"] for s in doc["shapes"]] == [
        "{email:{$regex:?}}",
        "{status:{$ne:?}}",
        "{customer_id:{$in:[?]}}",
    ]
    assert doc["shapes"][2]["meta"]["in_size"] == 300
    assert doc["shapes"][0]["first_seen"] == "2026-09-21T10:00:00+00:00"
    assert SECRET not in text and "sample" not in text and "300)" not in text
    rules = [f["rule"] for f in doc["findings"]]
    assert rules == [
        "collscan",
        "unanchored_regex",
        "negation_predicate",
        "collscan",
        "large_in",
        "unused_index",
        "collscan",
    ]
    assert doc["findings"][0]["recommendation"] is None
    assert doc["findings"][1]["recommendation"]["kind"] == "rewrite"
    assert doc["findings"][6]["recommendation"]["keys"] == [["email", 1]]
    assert doc["findings"][5]["recommendation"]["keys"] == []
    assert doc["findings"][4]["evidence"] == {"in_size": 300}


def test_json_report_deduplicates_recommendations() -> None:
    doc = json.loads(render_json(_analysis(), SOURCE, NOW))
    creates = doc["recommendations"]["create_index"]
    assert [c["statement"].split(", {name")[0] for c in creates] == [
        "db.orders.createIndex({customer_id: 1}",
        "db.orders.createIndex({email: 1}",
    ]
    assert len(creates[0]["shape_ids"]) == 1 and len(creates[1]["shape_ids"]) == 1
    assert doc["recommendations"]["drop_index"] == [
        {"statement": 'db.orders.dropIndex("tags_1")', "ns": "app.orders"}
    ]
    assert [r["shape_ids"] for r in doc["recommendations"]["rewrite"]] == [
        [doc["shapes"][0]["fingerprint"]],
        [doc["shapes"][1]["fingerprint"]],
        [doc["shapes"][2]["fingerprint"]],
    ]


def test_markdown_snapshot() -> None:
    text = render_markdown(_analysis(), SOURCE, NOW)
    snapshot = Path(__file__).with_name("report_snapshot.md")
    assert text == snapshot.read_text(encoding="utf-8")
    assert SECRET not in text


def test_markdown_caps_rows() -> None:
    analysis = _analysis()
    finding = analysis.findings[6]
    analysis.findings = [finding] * 45
    text = render_markdown(analysis, SOURCE, NOW)
    assert text.count("| medium | collscan |") == 30
    assert "and 15 more." in text


def test_markdown_without_findings() -> None:
    analysis = Analysis(checks=[], offline=True)
    text = render_markdown(analysis, Source("log", SINCE, files=["mongod.log"]), NOW)
    assert "No findings." in text and "offline" in text and "from the log since" in text
    assert "## Recommended indexes" not in text


def test_table_renders_every_section() -> None:
    console = Console(record=True, width=160, force_terminal=False)
    with console.capture() as capture:
        for part in render_table(_analysis()):
            console.print(part)
    text = capture.get()
    assert "findings" in text and "collscan" in text
    assert "Recommended indexes" in text and "Indexes to drop" in text and "Query rewrites" in text
    assert SECRET not in text


def test_create_recommendations_collapse_prefixes() -> None:
    analysis = _analysis()
    wide = recommendation("app.orders", (("email", 1), ("created", 1)))
    analysis.findings.append(
        Finding("medium", "low_selectivity_index", "app.orders", "abc", "m", wide, {})
    )
    statements = analysis.create_index_statements()
    keys = [tuple(r.keys) for r, _ in statements]
    assert keys == [(("customer_id", 1),), (("email", 1), ("created", 1))]
    ids = dict((tuple(r.keys), ids) for r, ids in statements)
    assert "abc" in ids[(("email", 1), ("created", 1))]
    assert len(ids[(("email", 1), ("created", 1))]) == 2
