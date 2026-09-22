from __future__ import annotations

import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from pymongo import MongoClient
from pymongo.errors import (
    AutoReconnect,
    ConfigurationError,
    OperationFailure,
    ServerSelectionTimeoutError,
)

from indexwright import __version__

if TYPE_CHECKING:
    from collections.abc import Callable

log = logging.getLogger(__name__)

T = TypeVar("T")
Client = MongoClient[dict[str, Any]]

MIN_VERSION = (5, 0, 0)
UNAUTHORIZED = 13
AUTHENTICATION_FAILED = 18

WRITE_ACTIONS = frozenset(
    {
        "anyAction",
        "insert",
        "update",
        "remove",
        "createCollection",
        "dropCollection",
        "dropDatabase",
        "createIndex",
        "dropIndex",
        "collMod",
        "renameCollectionSameDB",
        "convertToCapped",
        "compact",
    }
)

_CREDENTIALS = re.compile(r"://([^:/@]+):([^@]*)@")


class ConnectError(Exception):
    pass


class UnsupportedVersionError(Exception):
    pass


class WriteAccessError(Exception):
    def __init__(self, grants: list[str]) -> None:
        super().__init__("user has write access: " + ", ".join(grants))
        self.grants = grants


@dataclass(frozen=True)
class Settings:
    uri: str
    timeout: float = 5.0
    db: str | None = None

    @property
    def max_time_ms(self) -> int:
        return int(self.timeout * 1000)


@dataclass(frozen=True)
class Privileges:
    users: tuple[str, ...]
    roles: tuple[str, ...]
    write_grants: tuple[str, ...]

    @property
    def authenticated(self) -> bool:
        return bool(self.users)


@dataclass(frozen=True)
class Topology:
    kind: str
    name: str | None = None
    hosts: tuple[str, ...] = ()


@dataclass(frozen=True)
class ProfilerStatus:
    db: str
    level: int | None
    slow_ms: int | None
    profile_exists: bool
    profile_readable: bool


def mask_uri(uri: str) -> str:
    return _CREDENTIALS.sub(r"://\1:***@", uri)


def retry(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 0.5,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        # Server selection already waits for serverSelectionTimeoutMS, retrying it triples the wait.
        except ServerSelectionTimeoutError:
            raise
        # NetworkTimeout and NotPrimaryError are subclasses of AutoReconnect.
        except AutoReconnect as exc:
            if attempt == attempts:
                raise
            delay = base_delay * 2 ** (attempt - 1) * (1 + random.random() / 4)
            log.warning(
                "%s, retry %d/%d in %.1fs", type(exc).__name__, attempt, attempts - 1, delay
            )
            sleep(delay)
    raise AssertionError("unreachable")


@dataclass
class Connection:
    client: Client
    settings: Settings
    privileges: Privileges = field(default_factory=lambda: Privileges((), (), ()))

    def command(self, db: str, spec: dict[str, Any]) -> dict[str, Any]:
        full = {**spec, "maxTimeMS": self.settings.max_time_ms}
        return retry(lambda: self.client[db].command(full))

    @property
    def max_time_ms(self) -> int:
        return self.settings.max_time_ms

    def close(self) -> None:
        self.client.close()


def connect(settings: Settings) -> Connection:
    ms = settings.max_time_ms
    try:
        client: Client = MongoClient(
            settings.uri,
            serverSelectionTimeoutMS=ms,
            connectTimeoutMS=ms,
            socketTimeoutMS=30_000,
            maxPoolSize=4,
            appname=f"indexwright/{__version__}",
        )
    except ConfigurationError as exc:
        raise ConnectError(f"invalid connection string: {exc}") from exc
    conn = Connection(client, settings)
    try:
        conn.command("admin", {"ping": 1})
        conn.privileges = check_privileges(conn)
    except ServerSelectionTimeoutError as exc:
        conn.close()
        raise ConnectError(f"cannot reach {mask_uri(settings.uri)}: {exc}") from exc
    except OperationFailure as exc:
        conn.close()
        if exc.code == AUTHENTICATION_FAILED:
            raise ConnectError(f"authentication failed for {mask_uri(settings.uri)}") from exc
        raise ConnectError(f"{exc.details.get('errmsg', exc) if exc.details else exc}") from exc
    if conn.privileges.write_grants:
        conn.close()
        raise WriteAccessError(list(conn.privileges.write_grants))
    return conn


def server_version(conn: Connection) -> tuple[int, int, int]:
    info = conn.command("admin", {"buildInfo": 1})
    major, minor, patch = info["versionArray"][:3]
    version = (int(major), int(minor), int(patch))
    if version < MIN_VERSION:
        raise UnsupportedVersionError(
            f"MongoDB {info['version']} is not supported, need 5.0 or newer"
        )
    return version


def check_privileges(conn: Connection) -> Privileges:
    status = conn.command("admin", {"connectionStatus": 1, "showPrivileges": True})
    info = status["authInfo"]
    users = tuple(f"{u['user']}@{u['db']}" for u in info["authenticatedUsers"])
    roles = tuple(f"{r['role']}@{r['db']}" for r in info["authenticatedUserRoles"])
    grants = tuple(
        f"{action} on {_describe(p['resource'])}"
        for p in info.get("authenticatedUserPrivileges", [])
        if _touches_target(p["resource"], conn.settings.db)
        for action in sorted(WRITE_ACTIONS.intersection(p["actions"]))
    )
    return Privileges(users, roles, grants)


def _touches_target(resource: dict[str, Any], target_db: str | None) -> bool:
    db = resource.get("db")
    if db is None or db == "":
        return True
    return target_db is None or db == target_db


def _describe(resource: dict[str, Any]) -> str:
    if "db" not in resource:
        return "anyResource" if resource.get("anyResource") else "cluster"
    return f"{resource['db'] or '*'}.{resource.get('collection') or '*'}"


def topology(conn: Connection) -> Topology:
    hello = conn.command("admin", {"hello": 1})
    if hello.get("msg") == "isdbgrid":
        return Topology("sharded")
    if "setName" in hello:
        return Topology("replica set", hello["setName"], tuple(hello.get("hosts", [])))
    return Topology("standalone")


def user_databases(conn: Connection) -> list[str]:
    result = conn.command("admin", {"listDatabases": 1, "nameOnly": True})
    return [d["name"] for d in result["databases"] if d["name"] not in ("admin", "local", "config")]


def profiler_status(conn: Connection, db: str) -> ProfilerStatus:
    level = slow_ms = None
    try:
        result = conn.command(db, {"profile": -1})
        level, slow_ms = int(result["was"]), int(result["slowms"])
    except OperationFailure as exc:
        if exc.code != UNAUTHORIZED:
            raise
    database = conn.client[db]
    names = retry(
        lambda: database.list_collection_names(
            filter={"name": "system.profile"}, maxTimeMS=conn.max_time_ms
        )
    )
    try:
        retry(lambda: database["system.profile"].find_one({}, max_time_ms=conn.max_time_ms))
        readable = True
    except OperationFailure as exc:
        if exc.code != UNAUTHORIZED:
            raise
        readable = False
    return ProfilerStatus(db, level, slow_ms, bool(names), readable)


def index_stats_available(conn: Connection, db: str) -> bool | None:
    database = conn.client[db]
    names = retry(
        lambda: database.list_collection_names(
            filter={"name": {"$not": {"$regex": r"^system\."}}}, maxTimeMS=conn.max_time_ms
        )
    )
    if not names:
        return None
    pipeline: list[dict[str, Any]] = [{"$indexStats": {}}, {"$limit": 1}]
    try:
        retry(lambda: list(database[names[0]].aggregate(pipeline, maxTimeMS=conn.max_time_ms)))
    except OperationFailure:
        return False
    return True


def user_collections(conn: Connection, db: str) -> list[str]:
    database = conn.client[db]
    names = retry(
        lambda: database.list_collection_names(
            filter={"type": "collection", "name": {"$not": {"$regex": r"^system\."}}},
            maxTimeMS=conn.max_time_ms,
        )
    )
    return sorted(names)
