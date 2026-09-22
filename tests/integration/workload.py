from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pymongo import MongoClient

STATUSES = ["new", "paid", "shipped", "done", "cancelled"]
START = datetime(2026, 6, 1, tzinfo=UTC)

# (op, compact shape) on app.orders that the shapes command must find after run().
EXPECTED_SHAPES: set[tuple[str, str]] = {
    ("find", "{status:?}"),
    ("find", "{status:?} sort {created:-1}"),
    ("find", "{created:{$gte:?, $lt:?}}"),
    ("find", "{customer_id:{$in:[?]}}"),
    ("find", "{email:{$regex:?}}"),
    ("find", "{status:{$ne:?}}"),
    ("find", "{$or:[{status:?}, {total:{$gt:?}}]}"),
    ("find", "{_id:?}"),
    ("find", "{customer_id:?}"),
    ("aggregate", "[{$match:{status:?}}, {$group:{_id:?}}]"),
    ("distinct", "{created:{$gte:?}}"),
    ("update", "{_id:?}"),
    ("update", "{created:{$lt:?}, status:?}"),
    ("delete", "{status:?, total:{$lt:?}}"),
    ("findAndModify", "{status:?} sort {created:1}"),
    (
        "aggregate",
        "[{$match:{status:?}}, {$sort:{created:-1}}, {$group:{_id:$customer_id}}]",
    ),
    (
        "aggregate",
        "[{$match:{customer_id:?}}, "
        "{$lookup:{foreignField:_id, from:customers, localField:customer_id}}]",
    ),
}


def seed(client: MongoClient[dict[str, Any]]) -> None:
    rng = random.Random(42)
    db = client.app
    db.orders.drop()
    db.customers.drop()
    db.customers.insert_many(
        [
            {"_id": i, "name": f"customer {i}", "email": f"user{i}@example.com", "country": "RO"}
            for i in range(1, 501)
        ]
    )
    db.orders.insert_many(
        [
            {
                "_id": i,
                "status": rng.choice(STATUSES),
                "customer_id": rng.randint(1, 500),
                "created": START + timedelta(minutes=rng.randint(0, 90 * 24 * 60)),
                "total": round(rng.uniform(1, 1000), 2),
                "tags": rng.sample(["gift", "bulk", "promo", "vip"], k=rng.randint(0, 2)),
                "email": f"user{rng.randint(1, 500)}@example.com",
            }
            for i in range(1, 5001)
        ]
    )
    db.orders.create_index("customer_id")


def run(client: MongoClient[dict[str, Any]]) -> int:
    rng = random.Random(7)
    orders = client.app.orders
    day = timedelta(days=1)
    ops = 0

    def status() -> str:
        return rng.choice(STATUSES)

    def when() -> datetime:
        return START + day * rng.randint(0, 80)

    for _ in range(60):
        list(orders.find({"status": status()}))
        ops += 1
    for _ in range(10):
        list(orders.find({"status": "new"}, batch_size=20, limit=100))
        ops += 1
    for _ in range(60):
        list(orders.find({"status": status()}).sort("created", -1).limit(20))
        ops += 1
    for _ in range(40):
        start = when()
        list(orders.find({"created": {"$gte": start, "$lt": start + 3 * day}}))
        ops += 1
    for _ in range(40):
        list(orders.find({"customer_id": {"$in": rng.sample(range(1, 501), k=3)}}))
        ops += 1
    for _ in range(5):
        list(orders.find({"customer_id": {"$in": rng.sample(range(1, 501), k=250)}}))
        ops += 1
    for _ in range(20):
        list(orders.find({"email": {"$regex": f"user{rng.randint(1, 50)}"}}))
        ops += 1
    for _ in range(20):
        list(orders.find({"email": {"$regex": f"^user{rng.randint(1, 50)}@"}}))
        ops += 1
    for _ in range(20):
        list(orders.find({"status": {"$ne": "done"}}).limit(50))
        ops += 1
    for _ in range(20):
        list(orders.find({"$or": [{"status": "new"}, {"total": {"$gt": 990}}]}))
        ops += 1
    for _ in range(40):
        orders.find_one({"_id": rng.randint(1, 5000)})
        ops += 1
    for _ in range(30):
        list(orders.find({"customer_id": rng.randint(1, 500)}))
        ops += 1
    for _ in range(20):
        orders.count_documents({"status": status()})
        ops += 1
    for _ in range(20):
        orders.distinct("status", {"created": {"$gte": when()}})
        ops += 1
    for _ in range(40):
        orders.update_one({"_id": rng.randint(1, 5000)}, {"$set": {"touched": True}})
        ops += 1
    for _ in range(10):
        orders.update_many(
            {"status": "new", "created": {"$lt": START + day}}, {"$set": {"status": "cancelled"}}
        )
        ops += 1
    for _ in range(10):
        orders.delete_one({"status": "cancelled", "total": {"$lt": 5}})
        ops += 1
    for _ in range(20):
        orders.find_one_and_update(
            {"status": "new"}, {"$set": {"locked": True}}, sort=[("created", 1)]
        )
        ops += 1
    for _ in range(20):
        list(
            orders.aggregate(
                [
                    {"$match": {"status": status()}},
                    {"$sort": {"created": -1}},
                    {"$group": {"_id": "$customer_id", "total": {"$sum": "$total"}}},
                ]
            )
        )
        ops += 1
    for _ in range(20):
        list(
            orders.aggregate(
                [
                    {"$match": {"customer_id": rng.randint(1, 500)}},
                    {
                        "$lookup": {
                            "from": "customers",
                            "localField": "customer_id",
                            "foreignField": "_id",
                            "as": "customer",
                        }
                    },
                ]
            )
        )
        ops += 1
    return ops
