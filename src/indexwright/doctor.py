from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Literal

from pymongo.errors import OperationFailure

from indexwright.mongo import (
    UnsupportedVersionError,
    WriteAccessError,
    check_privileges,
    index_stats_available,
    profiler_status,
    server_version,
    topology,
    user_databases,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from indexwright.mongo import Client, ProfilerStatus, Settings

Status = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Check:
    name: str
    status: Status
    detail: str


def run_checks(client: Client, settings: Settings) -> list[Check]:
    checks = [_guard("server version", lambda: _version(client, settings))]
    privileges = check_privileges(client, settings)
    if privileges.write_grants:
        raise WriteAccessError(list(privileges.write_grants))
    if privileges.authenticated:
        detail = f"{', '.join(privileges.users)} with roles {', '.join(privileges.roles)}"
        checks.append(Check("privileges", "ok", detail))
    else:
        checks.append(Check("privileges", "warn", "not authenticated, cannot verify read-only"))
    checks.append(_guard("topology", lambda: _topology(client, settings)))

    databases = [settings.db] if settings.db else user_databases(client, settings)
    for db in databases:
        checks.append(_guard(f"profiler {db}", partial(_profiler, client, db, settings)))
    if databases:
        checks.append(_guard("$indexStats", lambda: _index_stats(client, databases[0], settings)))
    else:
        checks.append(Check("$indexStats", "warn", "no user databases found"))
    return checks


def _guard(name: str, fn: Callable[[], Check]) -> Check:
    try:
        return fn()
    except OperationFailure as exc:
        return Check(name, "fail", str(exc.details.get("errmsg", exc)) if exc.details else str(exc))


def _version(client: Client, settings: Settings) -> Check:
    try:
        version = server_version(client, settings)
    except UnsupportedVersionError as exc:
        return Check("server version", "fail", str(exc))
    return Check("server version", "ok", ".".join(map(str, version)))


def _topology(client: Client, settings: Settings) -> Check:
    topo = topology(client, settings)
    if topo.kind == "replica set":
        return Check("topology", "ok", f"replica set {topo.name}, {len(topo.hosts)} members")
    return Check("topology", "ok", topo.kind)


def _profiler(client: Client, db: str, settings: Settings) -> Check:
    status = profiler_status(client, db, settings)
    name = f"profiler {db}"
    if not status.profile_readable:
        return Check(name, "fail", f"cannot read {db}.system.profile, grant find on it")
    return Check(name, *_profiler_verdict(status))


def _profiler_verdict(status: ProfilerStatus) -> tuple[Status, str]:
    if status.level is None:
        exists = "exists" if status.profile_exists else "does not exist"
        return "warn", f"level unknown (needs enableProfiler action), system.profile {exists}"
    if status.level == 0:
        hint = "enable with db.setProfilingLevel(1, {slowms: 100})"
        if status.profile_exists:
            return "warn", f"level 0, system.profile has old entries; {hint}"
        return "warn", f"level 0, nothing recorded; {hint}"
    return "ok", f"level {status.level}, slowms {status.slow_ms}"


def _index_stats(client: Client, db: str, settings: Settings) -> Check:
    available = index_stats_available(client, db, settings)
    if available is None:
        return Check("$indexStats", "warn", f"no collections in {db} to test against")
    if not available:
        return Check("$indexStats", "warn", "unavailable, unused-index detection will be skipped")
    return Check("$indexStats", "ok", "available")
