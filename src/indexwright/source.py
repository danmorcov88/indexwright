from __future__ import annotations

import gzip
import logging
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bson import json_util
from bson.codec_options import DatetimeConversion
from bson.errors import InvalidBSON

from indexwright.model import Entry
from indexwright.mongo import user_databases

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator

    from indexwright.mongo import Connection

log = logging.getLogger(__name__)

PROFILE_OPS = {"query": "find", "update": "update", "remove": "delete"}
# Writes arrive as op update/remove with q and u. The log also records the wrapping update
# and delete commands without a filter; those are not shapes.
COMMAND_OPS = frozenset({"find", "count", "distinct", "aggregate", "findAndModify"})
# Logs contain sentinel dates outside the datetime range; keep them as DatetimeMS, do not fail.
JSON_OPTIONS = json_util.JSONOptions(datetime_conversion=DatetimeConversion.DATETIME_AUTO)


class LogFormatError(Exception):
    pass


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


def read_log(
    paths: Iterable[str], since: datetime | None, limit: int, db: str | None
) -> Iterator[Entry]:
    read = kept = skipped = 0
    for path in _expand(paths):
        parsed = 0
        for line in _lines(path):
            read += 1
            doc = _slow_query(line)
            if doc is None:
                skipped += 1
                continue
            parsed += 1
            if kept >= limit:
                break
            entry = to_entry(doc)
            if (
                entry is None
                or (since and entry.ts < since)
                or (db and not entry.ns.startswith(f"{db}."))
            ):
                continue
            kept += 1
            yield entry
        if parsed == 0 and read > 0:
            raise LogFormatError(f"{path}: no JSON log lines found (MongoDB 4.4+ logs are JSON)")
    log.info("log files: %d lines read, %d entries kept, %d lines skipped", read, kept, skipped)


def _expand(paths: Iterable[str]) -> list[Path]:
    files: list[Path] = []
    for pattern in paths:
        candidate = Path(pattern)
        if "*" in pattern or "?" in pattern:
            files.extend(sorted(candidate.parent.glob(candidate.name)))
        else:
            files.append(candidate)
    return files


def _lines(path: Path) -> Iterator[str]:
    if path.suffix == ".gz":
        with gzip.open(path, "rt", encoding="utf-8", errors="replace") as compressed:
            yield from compressed
        return
    with path.open(encoding="utf-8", errors="replace") as plain:
        yield from plain


def _slow_query(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        record = json_util.loads(line, json_options=JSON_OPTIONS)
    except (ValueError, InvalidBSON):
        return None
    if not isinstance(record, dict) or record.get("msg") != "Slow query":
        return None
    attr, ts = record.get("attr"), record.get("t")
    if not isinstance(attr, dict) or not isinstance(ts, datetime):
        return None
    return {**attr, "op": attr.get("type"), "ts": ts, "millis": attr.get("durationMillis", 0)}


def to_entry(doc: dict[str, Any]) -> Entry | None:
    profile_op = doc.get("op")
    raw = doc.get("command")
    # The profiler marks a getMore with op=getmore; the log marks it with the command itself.
    getmore = profile_op == "getmore" or (isinstance(raw, dict) and "getMore" in raw)
    command = doc.get("originatingCommand") if getmore else raw
    if not isinstance(command, dict) or "$truncated" in command:
        return None
    op = _resolve_op(profile_op, command)
    ns = doc.get("ns", "")
    own = str(doc.get("appName", "")).startswith("indexwright")
    if op is None or ".system." in ns or own:
        return None
    nreturned = _first_present(doc, "nreturned", "nMatched", "ndeleted")
    ts = doc["ts"]
    return Entry(
        ns=ns,
        op=op,
        ts=ts if ts.tzinfo else ts.replace(tzinfo=UTC),
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
        return first if first in COMMAND_OPS else None
    return None
