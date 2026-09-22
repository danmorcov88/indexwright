from __future__ import annotations

import logging
from collections import Counter
from typing import TYPE_CHECKING, Any

from indexwright.model import OPS, Entry
from indexwright.mongo import user_databases

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from indexwright.mongo import Connection

log = logging.getLogger(__name__)

PROFILE_OPS = {"query": "find", "update": "update", "remove": "delete"}


def read_all(conn: Connection, since: datetime | None, limit: int) -> Iterator[Entry]:
    databases = [conn.settings.db] if conn.settings.db else user_databases(conn)
    for db in databases:
        yield from read_profile(conn, db, since, limit)


def read_profile(conn: Connection, db: str, since: datetime | None, limit: int) -> Iterator[Entry]:
    query = {"ts": {"$gte": since}} if since else {}
    # system.profile is capped, so natural order is insertion order. Sorting on ts instead
    # would need an in-memory sort of the whole collection.
    cursor = conn.client[db]["system.profile"].find(
        query,
        sort=[("$natural", -1)],
        batch_size=500,
        limit=limit,
        max_time_ms=conn.max_time_ms,
    )
    skipped: Counter[str] = Counter()
    read = 0
    for doc in cursor:
        read += 1
        entry = to_entry(doc)
        if entry is None:
            skipped[str(doc.get("op"))] += 1
            continue
        yield entry
    log.info("%s.system.profile: %d read, skipped %s", db, read, dict(skipped) or "none")


def to_entry(doc: dict[str, Any]) -> Entry | None:
    profile_op = doc.get("op")
    getmore = profile_op == "getmore"
    command = doc.get("originatingCommand") if getmore else doc.get("command")
    if not isinstance(command, dict) or "$truncated" in command:
        return None
    op = _resolve_op(profile_op, command)
    ns = doc.get("ns", "")
    own = str(doc.get("appName", "")).startswith("indexwright")
    if op is None or ".system." in ns or own:
        return None
    nreturned = _first_present(doc, "nreturned", "nMatched", "ndeleted")
    return Entry(
        ns=ns,
        op=op,
        ts=doc["ts"],
        millis=int(doc.get("millis", 0)),
        docs_examined=int(doc.get("docsExamined", 0)),
        keys_examined=int(doc.get("keysExamined", 0)),
        nreturned=None if nreturned is None else int(nreturned),
        has_sort_stage=bool(doc.get("hasSortStage")),
        plan_summary=str(doc.get("planSummary", "")),
        command=command,
        getmore=getmore,
    )


def _first_present(doc: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in doc:
            return doc[key]
    return None


def _resolve_op(profile_op: Any, command: dict[str, Any]) -> str | None:
    if profile_op in PROFILE_OPS:
        return PROFILE_OPS[profile_op]
    if profile_op in ("command", "getmore"):
        first = next(iter(command), None)
        return first if first in OPS else None
    return None
