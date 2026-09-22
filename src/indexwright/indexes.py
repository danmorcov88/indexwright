from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from pymongo.errors import OperationFailure

from indexwright.model import CollectionIndexes, IndexInfo, IndexUsage
from indexwright.mongo import (
    ConnectError,
    Settings,
    connect,
    retry,
    user_collections,
    user_databases,
)

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from indexwright.mongo import Connection

log = logging.getLogger(__name__)

NAMESPACE_NOT_FOUND = 26
_URI = re.compile(r"^(mongodb(?:\+srv)?)://(?:([^@/]*)@)?([^/?]+)(/[^?]*)?(?:\?(.*))?$")


class IndexCatalog:
    known = True

    def __init__(self, conn: Connection) -> None:
        self.conn = conn
        self._cache: dict[str, list[IndexInfo]] = {}

    def for_ns(self, ns: str) -> list[IndexInfo]:
        if ns not in self._cache:
            self._cache[ns] = list_indexes(self.conn, ns)
        return self._cache[ns]


def list_indexes(conn: Connection, ns: str) -> list[IndexInfo]:
    db, coll = ns.split(".", 1)
    try:
        result = conn.command(db, {"listIndexes": coll})
    except OperationFailure as exc:
        if exc.code == NAMESPACE_NOT_FOUND:
            return []
        raise
    cursor = result["cursor"]
    specs: list[dict[str, Any]] = list(cursor["firstBatch"])
    while cursor.get("id"):
        result = conn.command(db, {"getMore": cursor["id"], "collection": coll})
        cursor = result["cursor"]
        specs.extend(cursor["nextBatch"])
    return [index_info(ns, spec) for spec in specs]


def index_info(ns: str, spec: dict[str, Any]) -> IndexInfo:
    return IndexInfo(
        ns=ns,
        name=str(spec["name"]),
        keys=tuple((str(k), v) for k, v in spec["key"].items()),
        unique=bool(spec.get("unique")),
        sparse=bool(spec.get("sparse")),
        partial="partialFilterExpression" in spec,
        hidden=bool(spec.get("hidden")),
        ttl="expireAfterSeconds" in spec,
        partial_filter=_canonical(spec.get("partialFilterExpression")),
        collation=_canonical(spec.get("collation", {}).get("locale")),
    )


def _canonical(value: Any) -> str | None:
    return None if value is None else json.dumps(value, sort_keys=True, default=str)


@dataclass
class UsageReport:
    usage: dict[str, dict[str, IndexUsage] | None] = field(default_factory=dict)
    members_total: int = 1
    members_reached: int = 1
    unreachable: list[str] = field(default_factory=list)


def member_uri(uri: str, host: str) -> str:
    match = _URI.match(uri)
    if match is None:
        raise ConnectError(f"cannot derive a member connection string for {host}")
    scheme, userinfo, _, path, query = match.groups()
    options = [
        option
        for option in (query or "").split("&")
        if option and not option.lower().startswith(("replicaset=", "directconnection="))
    ]
    if scheme == "mongodb+srv" and not any(
        option.lower().startswith(("tls=", "ssl=")) for option in options
    ):
        options.append("tls=true")
    options.append("directConnection=true")
    auth = f"{userinfo}@" if userinfo else ""
    return f"mongodb://{auth}{host}{path or '/'}?{'&'.join(options)}"


def members(hello: dict[str, Any]) -> list[str]:
    if "setName" not in hello:
        return []
    return list(hello.get("hosts", [])) + list(hello.get("passives", []))


def collect_usage(conn: Connection, namespaces: Iterable[str]) -> UsageReport:
    hello = conn.command("admin", {"hello": 1})
    hosts = members(hello)
    report = UsageReport(members_total=max(len(hosts), 1), members_reached=0)
    sources: list[tuple[str, Connection, bool]] = []
    for host in hosts or [str(hello.get("me", "self"))]:
        if not hosts or host == hello.get("me"):
            sources.append((host, conn, False))
            continue
        settings = Settings(member_uri(conn.settings.uri, host), conn.settings.timeout)
        try:
            sources.append((host, connect(settings), True))
        except ConnectError as exc:
            log.warning("member %s unreachable, its index usage is missing: %s", host, exc)
            report.unreachable.append(host)
    accumulators: dict[str, dict[str, _Usage] | None] = {}
    namespaces = list(namespaces)
    try:
        for host, member, _ in sources:
            report.members_reached += 1
            for ns in namespaces:
                _accumulate(member, ns, host, accumulators)
    finally:
        for _, member, owned in sources:
            if owned:
                member.close()
    for ns, per_index in accumulators.items():
        report.usage[ns] = (
            None
            if per_index is None
            else {name: acc.usage(ns, name) for name, acc in per_index.items()}
        )
    return report


class _Usage:
    def __init__(self) -> None:
        self.ops = 0
        self.since: datetime | None = None
        self.hosts: set[str] = set()

    def add(self, doc: dict[str, Any], host: str) -> None:
        accesses = doc.get("accesses", {})
        self.ops += int(accesses.get("ops", 0))
        since = accesses.get("since")
        if since is not None and (self.since is None or since < self.since):
            self.since = since
        self.hosts.add(str(doc.get("host", host)))

    def usage(self, ns: str, name: str) -> IndexUsage:
        assert self.since is not None
        return IndexUsage(ns, name, self.ops, self.since, len(self.hosts))


def _accumulate(
    member: Connection, ns: str, host: str, accumulators: dict[str, dict[str, _Usage] | None]
) -> None:
    if accumulators.get(ns, {}) is None:
        return
    db, coll = ns.split(".", 1)
    pipeline: list[dict[str, Any]] = [{"$indexStats": {}}]
    try:
        docs = retry(
            lambda: list(member.client[db][coll].aggregate(pipeline, maxTimeMS=member.max_time_ms))
        )
    except OperationFailure as exc:
        log.warning("$indexStats unavailable for %s on %s: code %s", ns, host, exc.code)
        accumulators[ns] = None
        return
    per_index = accumulators.setdefault(ns, {})
    assert per_index is not None
    for doc in docs:
        if doc.get("building") or "accesses" not in doc:
            continue
        per_index.setdefault(str(doc["name"]), _Usage()).add(doc, host)


def write_share(conn: Connection) -> dict[str, float] | None:
    try:
        totals = conn.command("admin", {"top": 1})["totals"]
    except OperationFailure as exc:
        log.info("top unavailable, write share unknown: code %s", exc.code)
        return None
    shares: dict[str, float] = {}
    for ns, counters in totals.items():
        if not isinstance(counters, dict) or "total" not in counters:
            continue
        total = int(counters["total"].get("count", 0))
        if total:
            writes = sum(
                int(counters.get(k, {}).get("count", 0)) for k in ("insert", "update", "remove")
            )
            shares[ns] = writes / total
    return shares


@dataclass
class Inventory:
    collections: list[CollectionIndexes]
    report: UsageReport


def inventory(conn: Connection, unused_days: int) -> Inventory:
    databases = [conn.settings.db] if conn.settings.db else user_databases(conn)
    namespaces = [f"{db}.{coll}" for db in databases for coll in user_collections(conn, db)]
    report = collect_usage(conn, namespaces)
    shares = write_share(conn)
    catalog = IndexCatalog(conn)
    collections = [
        CollectionIndexes(
            ns=ns,
            indexes=catalog.for_ns(ns),
            usage=report.usage.get(ns),
            members_total=report.members_total,
            members_reached=report.members_reached,
            write_share=shares.get(ns) if shares is not None else None,
            unused_days=unused_days,
        )
        for ns in namespaces
    ]
    return Inventory(collections, report)
