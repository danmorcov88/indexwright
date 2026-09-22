from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest
from pymongo import MongoClient
from testcontainers.community.mongodb import MongoDbContainer

if TYPE_CHECKING:
    from collections.abc import Iterator

ROOT_USER = "root"
ROOT_PASSWORD = "r00t-secret"
RO_USER = "advisor"
RO_PASSWORD = "adv1sor-secret"
RW_USER = "writer"
RW_PASSWORD = "wr1ter-secret"
APP_DB = "app"


@dataclass(frozen=True)
class Mongo:
    host: str
    port: int
    root: MongoClient[dict[str, Any]]

    def uri(self, user: str, password: str) -> str:
        return f"mongodb://{user}:{password}@{self.host}:{self.port}/{APP_DB}?authSource=admin"

    @property
    def ro_uri(self) -> str:
        return self.uri(RO_USER, RO_PASSWORD)

    @property
    def rw_uri(self) -> str:
        return self.uri(RW_USER, RW_PASSWORD)

    def opcounters(self) -> dict[str, int]:
        counters = self.root.admin.command("serverStatus")["opcounters"]
        return {k: int(counters[k]) for k in ("insert", "update", "delete")}


@pytest.fixture(scope="session")
def mongo() -> Iterator[Mongo]:
    image = os.environ.get("MONGO_IMAGE", "mongo:7")
    with MongoDbContainer(image, username=ROOT_USER, password=ROOT_PASSWORD) as container:
        root: MongoClient[dict[str, Any]] = MongoClient(container.get_connection_url())
        root[APP_DB].orders.insert_many([{"status": "new", "n": i} for i in range(50)])
        root[APP_DB].command("profile", 1, slowms=0)
        root.admin.command(
            "createUser",
            RO_USER,
            pwd=RO_PASSWORD,
            roles=[{"role": "read", "db": APP_DB}, {"role": "clusterMonitor", "db": "admin"}],
        )
        root.admin.command(
            "createUser", RW_USER, pwd=RW_PASSWORD, roles=[{"role": "readWrite", "db": APP_DB}]
        )
        yield Mongo(container.get_container_host_ip(), int(container.get_exposed_port(27017)), root)
        root.close()


@pytest.fixture(scope="session")
def workload(mongo: Mongo) -> int:
    from tests.integration import workload as wl

    db = mongo.root[APP_DB]
    wl.seed(mongo.root)
    # The default 1 MB system.profile would evict most of the workload.
    db.command("profile", 0)
    db.system.profile.drop()
    db.create_collection("system.profile", capped=True, size=64 * 1024 * 1024)
    db.command("profile", 2)
    ops = wl.run(mongo.root)
    db.command("profile", 1, slowms=0)
    return ops
