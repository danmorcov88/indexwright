from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from indexwright.model import CollectionIndexes, IndexInfo, IndexUsage
from indexwright.rules import INDEX_RULES, run_index_rules

NS = "app.orders"
OLD = datetime.now(UTC) - timedelta(days=90)
FRESH = datetime.now(UTC) - timedelta(days=2)


def _index(name: str, *keys: tuple[str, Any], **flags: bool) -> IndexInfo:
    return IndexInfo(NS, name, keys, **flags)


def _coll(
    *indexes: IndexInfo,
    usage: dict[str, tuple[int, datetime]] | None = None,
    members: tuple[int, int] = (1, 1),
    write_share: float | None = 0.1,
    unused_days: int = 30,
) -> CollectionIndexes:
    stats = None
    if usage is not None:
        stats = {
            name: IndexUsage(NS, name, ops, since, members[1])
            for name, (ops, since) in usage.items()
        }
    return CollectionIndexes(
        NS, list(indexes), stats, members[0], members[1], write_share, unused_days
    )


def _rules(coll: CollectionIndexes) -> list[tuple[str, str | None]]:
    return [(f.rule, f.evidence.get("index")) for f in run_index_rules(coll)]


ID = _index("_id_", ("_id", 1))


def test_registry_order() -> None:
    assert [n for n, _ in INDEX_RULES] == [
        "duplicate_index",
        "redundant_index",
        "unused_index",
        "too_many_indexes",
    ]


def test_duplicate_keeps_unique_then_most_used() -> None:
    coll = _coll(
        ID,
        _index("a_1", ("a", 1)),
        _index("a_dup", ("a", 1)),
        _index("a_uniq", ("a", 1), unique=True),
        usage={"a_1": (50, OLD), "a_dup": (0, OLD), "a_uniq": (0, OLD)},
    )
    findings = run_index_rules(coll)
    assert [(f.rule, f.evidence["index"], f.evidence["duplicate_of"]) for f in findings] == [
        ("duplicate_index", "a_1", "a_uniq"),
        ("duplicate_index", "a_dup", "a_uniq"),
    ]
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.statement == 'db.orders.dropIndex("a_1")'
    assert findings[0].recommendation.kind == "drop_index"
    assert all(f.severity == "medium" and f.shape_id == "" for f in findings)


def test_duplicate_ignores_partial_versus_full() -> None:
    coll = _coll(ID, _index("a_1", ("a", 1)), _index("a_part", ("a", 1), partial=True))
    assert _rules(coll) == []


def test_redundant_prefix() -> None:
    coll = _coll(
        ID,
        _index("a_1", ("a", 1)),
        _index("a_1_b_1", ("a", 1), ("b", 1)),
        _index("a_1_b_1_c_-1", ("a", 1), ("b", 1), ("c", -1)),
    )
    findings = run_index_rules(coll)
    assert [(f.evidence["index"], f.evidence["prefix_of"]) for f in findings] == [
        ("a_1", "a_1_b_1"),
        ("a_1_b_1", "a_1_b_1_c_-1"),
    ]
    assert "is a prefix of" in findings[0].message


def test_redundant_single_key_in_either_direction() -> None:
    coll = _coll(ID, _index("a_-1", ("a", -1)), _index("a_1_b_1", ("a", 1), ("b", 1)))
    assert _rules(coll) == [("redundant_index", "a_-1")]
    coll = _coll(
        ID,
        _index("a_1_b_-1", ("a", 1), ("b", -1)),
        _index("a_1_b_1_c", ("a", 1), ("b", 1), ("c", 1)),
    )
    assert _rules(coll) == []


def test_redundant_exemptions() -> None:
    wide = _index("a_1_b_1", ("a", 1), ("b", 1))
    assert _rules(_coll(ID, _index("a_u", ("a", 1), unique=True), wide)) == []
    assert _rules(_coll(ID, _index("a_p", ("a", 1), partial=True), wide)) == []
    assert _rules(_coll(ID, _index("a_s", ("a", 1), sparse=True), wide)) == []
    assert _rules(_coll(ID, _index("a_t", ("a", 1), ttl=True), wide)) == []
    partial_wide = _index("a_1_b_1", ("a", 1), ("b", 1), partial=True)
    assert _rules(_coll(ID, _index("a_1", ("a", 1)), partial_wide)) == []
    id_wide = _index("_id_1_x_1", ("_id", 1), ("x", 1))
    assert _rules(_coll(ID, id_wide)) == []


def test_unused_index() -> None:
    coll = _coll(
        ID,
        _index("old", ("o", 1)),
        _index("fresh", ("f", 1)),
        _index("used", ("u", 1)),
        _index("uniq", ("q", 1), unique=True),
        _index("ttl", ("t", 1), ttl=True),
        usage={
            "_id_": (0, OLD),
            "old": (0, OLD),
            "fresh": (0, FRESH),
            "used": (3, OLD),
            "uniq": (0, OLD),
            "ttl": (0, OLD),
        },
    )
    findings = run_index_rules(coll)
    assert [(f.rule, f.evidence["index"], f.severity) for f in findings] == [
        ("unused_index", "old", "low")
    ]
    assert "has not been used in 90 days" in findings[0].message
    assert findings[0].recommendation is not None
    assert findings[0].recommendation.statement == 'db.orders.dropIndex("old")'
    assert (
        _rules(_coll(ID, _index("old", ("o", 1)), usage={"old": (0, OLD)}, unused_days=100)) == []
    )


def test_unused_index_warns_about_missing_members() -> None:
    coll = _coll(ID, _index("old", ("o", 1)), usage={"old": (0, OLD)}, members=(3, 2))
    finding = run_index_rules(coll)[0]
    assert "only 2 of 3 members reported" in finding.message
    assert finding.evidence["members_total"] == 3


def test_unused_needs_usage_data() -> None:
    assert _rules(_coll(ID, _index("old", ("o", 1)), usage=None)) == []
    assert _rules(_coll(ID, _index("old", ("o", 1)), usage={})) == []


def test_one_index_gets_one_finding() -> None:
    coll = _coll(
        ID,
        _index("a_1", ("a", 1)),
        _index("a_1_b_1", ("a", 1), ("b", 1)),
        _index("a_copy", ("a", 1)),
        usage={"a_1": (0, OLD), "a_1_b_1": (0, OLD), "a_copy": (0, OLD)},
    )
    assert _rules(coll) == [
        ("duplicate_index", "a_copy"),
        ("redundant_index", "a_1"),
        ("unused_index", "a_1_b_1"),
    ]


def test_too_many_indexes() -> None:
    many = [_index(f"f{i}_1", (f"f{i}", 1)) for i in range(21)]
    assert _rules(_coll(ID, *many[:19])) == []
    low = run_index_rules(_coll(ID, *many, write_share=0.05))[0]
    assert (low.rule, low.severity, low.recommendation) == ("too_many_indexes", "low", None)
    assert (
        "22 indexes on app.orders" in low.message and "5% of operations are writes" in low.message
    )
    medium = run_index_rules(_coll(ID, *many, write_share=0.5))[0]
    assert medium.severity == "medium"
    unknown = run_index_rules(_coll(ID, *many, write_share=None))[0]
    assert unknown.severity == "low" and "write share unknown" in unknown.message
