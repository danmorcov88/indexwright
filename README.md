# indexwright

Read-only performance advisor for self-hosted MongoDB.

It reads the profiler (or a `mongod` log), groups slow operations into query shapes, runs `explain` on each shape, and tells you which indexes to create (following the ESR rule) and which existing indexes are unused, redundant or duplicated. It never writes to your database. Output is a short report plus `createIndex` statements you run yourself.

MongoDB Atlas ships this as Performance Advisor. Self-hosted MongoDB does not. This fills the gap.

**Work in progress.** Nothing is released yet. Follow the [changelog](CHANGELOG.md).

## Read-only user

The tool refuses to run with a user that can write. Create one that can only read:

```js
use admin
db.createUser({
  user: "indexwright",
  pwd: passwordPrompt(),
  roles: [
    { role: "read", db: "app" },            // one entry per database to analyze, or readAnyDatabase
    { role: "clusterMonitor", db: "admin" }
  ]
})
```

## Commands

```sh
export MONGODB_URI="mongodb://indexwright:<password>@host:27017/app?authSource=admin"
indexwright doctor                       # connectivity, version, privileges, profiler, $indexStats
indexwright shapes  --db app --since 24h # query shapes with count, p50, p99, docs examined per returned
indexwright analyze --db app --since 24h # findings, createIndex and dropIndex statements
indexwright indexes --db app             # index inventory with usage from every replica set member
indexwright analyze --log /var/log/mongodb/mongod.log --db app   # from the mongod log, offline
```

Exit codes: `0` ok, `1` a check failed or a high/critical finding exists, `2` cannot connect,
`3` the user has write access.

`analyze` output on the test workload (before creating the recommended indexes):

```
 severity   rule                    ns           shape                           message
 medium     collscan                app.orders   find {email:{$regex:?}}         collection scan, 5000 documents examined per execution, 31 per document returned
 medium     sort_in_memory          app.orders   find {status:?} sort {total:-1} sort on {total:-1} is done in memory, no index supplies that order
 medium     docs_examined_ratio     app.orders   update {status:?, total:{$lt:?}} status_1 is used but 1917 documents are examined per document returned, the filter on total runs after fetching each document
 medium     low_selectivity_index   app.orders   find {created:{$gte:?, $lt:?}, email:?} created_1_email_1 examines 560 index keys per document returned, its key order does not match the query
 ...

Recommended indexes
  db.orders.createIndex({email: 1}, {name: "orders_esr_d67a46"})
  db.orders.createIndex({status: 1, total: -1}, {name: "orders_esr_ef4269"})
  db.orders.createIndex({total: 1}, {name: "orders_esr_0441be"})
  db.orders.createIndex({status: 1, total: 1}, {name: "orders_esr_0fce15"})
  db.orders.createIndex({email: 1, created: 1}, {name: "orders_esr_dadf97"})
```

After creating those five indexes and running the same workload again, `analyze` reports one
finding instead of nine. The rules are described in [docs/rules.md](docs/rules.md).

The profiler must be on: `db.setProfilingLevel(1, {slowms: 100})`. See [how it works](docs/how-it-works.md).
