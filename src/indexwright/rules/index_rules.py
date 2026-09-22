from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from indexwright.model import Finding, Recommendation
from indexwright.rules.esr import format_keys

if TYPE_CHECKING:
    from indexwright.model import CollectionIndexes, IndexInfo, Severity

MAX_INDEXES = 20
WRITE_HEAVY = 0.3


def duplicate_index(coll: CollectionIndexes, dropped: set[str]) -> list[Finding]:
    groups: dict[tuple[Any, ...], list[IndexInfo]] = {}
    for index in coll.indexes:
        groups.setdefault((index.keys, index.partial), []).append(index)
    findings = []
    for group in groups.values():
        if len(group) < 2:
            continue
        keep, *extra = sorted(group, key=lambda i: (not i.unique, -_ops(coll, i.name), i.name))
        for index in extra:
            message = f"{index.name} duplicates {keep.name} on {format_keys(index.keys)}"
            evidence = {
                "index": index.name,
                "duplicate_of": keep.name,
                "ops": _ops(coll, index.name),
            }
            findings.append(_drop("duplicate_index", coll, index, "medium", message, evidence))
    return findings


def redundant_index(coll: CollectionIndexes, dropped: set[str]) -> list[Finding]:
    findings = []
    for index in coll.indexes:
        if index.name in dropped or index.name == "_id_" or index.unique:
            continue
        if index.partial or index.sparse or index.ttl:
            continue
        wider = next((o for o in coll.indexes if _is_prefix_of(index, o)), None)
        if wider is None:
            continue
        message = (
            f"{index.name} {format_keys(index.keys)} is a prefix of "
            f"{wider.name} {format_keys(wider.keys)}, the wider index serves the same queries"
        )
        evidence = {"index": index.name, "prefix_of": wider.name, "ops": _ops(coll, index.name)}
        findings.append(_drop("redundant_index", coll, index, "medium", message, evidence))
    return findings


def _is_prefix_of(short: IndexInfo, long: IndexInfo) -> bool:
    if long.name == short.name or len(long.keys) <= len(short.keys):
        return False
    if long.partial or long.sparse or long.hidden:
        return False
    prefix = long.keys[: len(short.keys)]
    if prefix == short.keys:
        return True
    # A single key serves either direction.
    return len(short.keys) == 1 and prefix[0][0] == short.keys[0][0]


def unused_index(coll: CollectionIndexes, dropped: set[str]) -> list[Finding]:
    if coll.usage is None:
        return []
    cutoff = datetime.now(UTC) - timedelta(days=coll.unused_days)
    findings = []
    for index in coll.indexes:
        if index.name in dropped or index.name == "_id_" or index.unique or index.ttl:
            continue
        usage = coll.usage.get(index.name)
        if usage is None or usage.ops > 0:
            continue
        since = usage.since if usage.since.tzinfo else usage.since.replace(tzinfo=UTC)
        if since > cutoff:
            continue
        days = (datetime.now(UTC) - since).days
        message = f"{index.name} {format_keys(index.keys)} has not been used in {days} days"
        if coll.members_reached < coll.members_total:
            message += (
                f", but only {coll.members_reached} of {coll.members_total} members reported;"
                " check the others before dropping"
            )
        evidence = {
            "index": index.name,
            "since": since.isoformat(),
            "members_reported": usage.members_reported,
            "members_total": coll.members_total,
        }
        findings.append(_drop("unused_index", coll, index, "low", message, evidence))
    return findings


def too_many_indexes(coll: CollectionIndexes, dropped: set[str]) -> list[Finding]:
    count = len(coll.indexes)
    if count <= MAX_INDEXES:
        return []
    if coll.write_share is None:
        detail = "write share unknown"
        level: Severity = "low"
    else:
        detail = f"{coll.write_share:.0%} of operations are writes"
        level = "medium" if coll.write_share >= WRITE_HEAVY else "low"
    message = f"{count} indexes on {coll.ns}, every write maintains all of them; {detail}"
    evidence = {"indexes": count, "write_share": coll.write_share}
    return [Finding(level, "too_many_indexes", coll.ns, "", message, None, evidence)]


def _ops(coll: CollectionIndexes, name: str) -> int:
    if coll.usage is None or name not in coll.usage:
        return 0
    return coll.usage[name].ops


def _drop(
    rule: str,
    coll: CollectionIndexes,
    index: IndexInfo,
    level: Severity,
    message: str,
    evidence: dict[str, Any],
) -> Finding:
    collection = coll.ns.split(".", 1)[1]
    statement = f'db.{collection}.dropIndex("{index.name}")'
    recommendation = Recommendation("drop_index", coll.ns, statement)
    return Finding(level, rule, coll.ns, "", message, recommendation, evidence)
