# indexwright

Read-only performance advisor for self-hosted MongoDB.

It reads the profiler (or a `mongod` log), groups slow operations into query shapes, runs
`explain` on each shape, and tells you which indexes to create (following the ESR rule), which
existing indexes are unused, redundant or duplicated, and which queries no index can help.
It never writes to your database. Output is a short report plus `createIndex` and `dropIndex`
statements you run yourself.

MongoDB Atlas ships this as Performance Advisor. Self-hosted MongoDB does not. This fills the gap.

Works with MongoDB 5.0 and newer: standalone, replica set, and the read path of a sharded cluster.

## What it found on a real run

A single-node replica set (`mongo:7`), one collection of 300 000 orders with three indexes, and a
workload of 585 operations in 20 query shapes. `indexwright analyze` reported 13 findings and 5
`createIndex` statements. After creating those five indexes and running the same workload again
it reported 5 findings. Latency per shape, from the profiler:

| shape | finding before | p99 before | p99 after |
|---|---|---:|---:|
| `findAndModify {status:?} sort {total:1}` | sort_in_memory | 111 ms | 0 ms |
| `update {status:?, total:{$lt:?}}` | docs_examined_ratio | 81 ms | 0 ms |
| `find {status:?} sort {total:-1}` | sort_in_memory | 58 ms | 0 ms |
| `aggregate [$match customer_id, $lookup customers on email]` | lookup_no_index | 22 ms | 5 ms |
| `find {status:?, total:{$gt:?}}` | docs_examined_ratio | 15 ms | 2 ms |
| `find {created:{$gte:?, $lt:?}, email:?}` | low_selectivity_index | 6 ms | 1 ms |
| `aggregate [$match status, $sort total, $group customer_id]` | sort_in_memory (rewrite) | 172 ms | 183 ms |
| `find {email:{$regex:?}}` | collscan, unanchored_regex (rewrite) | 33 ms | 135 ms |
| `find {status:{$ne:?}}` | negation_predicate (rewrite) | 0 ms | 0 ms |
| `find {customer_id:{$in:[?]}}` | large_in (rewrite) | 1 ms | 0 ms |

The last four rows are the honest part. The tool marks them as query rewrites, not index
problems: a sort that feeds `$group` gains nothing from an index, and an unanchored `$regex`
cannot use index bounds. It got slower after the run because the planner now walks the new
`{email: 1, created: 1}` index instead of scanning the collection. The finding says exactly that:
anchor the pattern with `^`, or use a text index.

Run on 2026-09-22 with the workload in `tests/integration/workload.py`. Unchanged shapes are
omitted from the table. Below, the same workload on the 5 000 document test collection, output
pasted as is:

```
$ indexwright analyze --db app --since 1h
findings
┌──────────┬───────────────────────┬───────────────┬─────────────────────┬──────────────────────────────────────────┐
│ severity │ rule                  │ ns            │ shape               │ message                                  │
├──────────┼───────────────────────┼───────────────┼─────────────────────┼──────────────────────────────────────────┤
│ medium   │ collscan              │ app.orders    │ find                │ collection scan, 5000 documents examined │
│          │                       │               │ {email:{$regex:?}}  │ per execution, 31 per document returned; │
│          │                       │               │                     │ no indexable predicate to build an index │
│          │                       │               │                     │ from                                     │
│ medium   │ unanchored_regex      │ app.orders    │ find                │ $regex on email is not anchored with ^,  │
│          │                       │               │ {email:{$regex:?}}  │ no index can narrow it: every key or     │
│          │                       │               │                     │ every document is scanned                │
│ medium   │ lookup_no_index       │ app.orders    │ aggregate           │ $lookup joins customers on email, which  │
│          │                       │               │ [{$match:{customer_ │ has no index, so every input document    │
│          │                       │               │ id:?}},             │ scans customers                          │
│          │                       │               │ {$lookup:{foreignFi │                                          │
│          │                       │               │ eld:email,          │                                          │
│          │                       │               │ from:customers,     │                                          │
│          │                       │               │ localField:email}}] │                                          │
│ medium   │ sort_in_memory        │ app.orders    │ aggregate           │ sort on {total:-1} is done in memory, no │
│          │                       │               │ [{$match:{status:?} │ index supplies that order; the sorted    │
│          │                       │               │ },                  │ documents go straight into $group        │
│          │                       │               │ {$sort:{total:-1}}, │                                          │
│          │                       │               │ {$group:{_id:$custo │                                          │
│          │                       │               │ mer_id}}]           │                                          │
│ medium   │ collscan              │ app.orders    │ find                │ collection scan, 5000 documents examined │
│          │                       │               │ {$or:[{status:?},   │ per execution, 5 per document returned   │
│          │                       │               │ {total:{$gt:?}}]}   │                                          │
│ medium   │ sort_in_memory        │ app.orders    │ find {status:?}     │ sort on {total:-1} is done in memory, no │
│          │                       │               │ sort {total:-1}     │ index supplies that order                │
│ medium   │ large_in              │ app.orders    │ find                │ $in with up to 250 values, one index     │
│          │                       │               │ {customer_id:{$in:[ │ bound per value                          │
│          │                       │               │ ?]}}                │                                          │
│ medium   │ sort_in_memory        │ app.orders    │ findAndModify       │ sort on {total:1} is done in memory, no  │
│          │                       │               │ {status:?} sort     │ index supplies that order                │
│          │                       │               │ {total:1}           │                                          │
│ medium   │ docs_examined_ratio   │ app.orders    │ update {status:?,   │ status_1 is used but 1917 documents are  │
│          │                       │               │ total:{$lt:?}}      │ examined per document returned, the      │
│          │                       │               │                     │ filter on total runs after fetching each │
│          │                       │               │                     │ document                                 │
│ medium   │ low_selectivity_index │ app.orders    │ find                │ created_1_email_1 examines 560 index     │
│          │                       │               │ {created:{$gte:?,   │ keys per document returned, its key      │
│          │                       │               │ $lt:?}, email:?}    │ order does not match the query           │
│ medium   │ docs_examined_ratio   │ app.orders    │ delete {status:?,   │ status_1 is used but 559 documents are   │
│          │                       │               │ total:{$lt:?}}      │ examined per document returned, the      │
│          │                       │               │                     │ filter on total runs after fetching each │
│          │                       │               │                     │ document                                 │
│ medium   │ negation_predicate    │ app.orders    │ find                │ the filter on status only excludes       │
│          │                       │               │ {status:{$ne:?}}    │ values, an index cannot narrow a         │
│          │                       │               │                     │ negation                                 │
│ medium   │ docs_examined_ratio   │ app.orders    │ find {status:?,     │ status_1 is used but 81 documents are    │
│          │                       │               │ total:{$gt:?}}      │ examined per document returned, the      │
│          │                       │               │                     │ filter on total runs after fetching each │
│          │                       │               │                     │ document                                 │
│ medium   │ redundant_index       │ app.orders    │ index created_1     │ created_1 {created: 1} is a prefix of    │
│          │                       │               │                     │ created_1_email_1 {created: 1, email:    │
│          │                       │               │                     │ 1}, the wider index serves the same      │
│          │                       │               │                     │ queries                                  │
│ low      │ too_many_indexes      │ app.customers │ collection          │ 22 indexes on app.customers, every write │
│          │                       │               │                     │ maintains all of them; 0% of operations  │
│          │                       │               │                     │ are writes                               │
└──────────┴───────────────────────┴───────────────┴─────────────────────┴──────────────────────────────────────────┘

Recommended indexes
  db.customers.createIndex({email: 1}, {name: "customers_esr_d67a46"})   # shapes 10a62af587
  db.orders.createIndex({total: 1}, {name: "orders_esr_0441be"})   # shapes a24a01eede
  db.orders.createIndex({status: 1, total: -1}, {name: "orders_esr_ef4269"})   # shapes 84cc04d69e
  db.orders.createIndex({status: 1, total: 1}, {name: "orders_esr_0fce15"})   # shapes f0f8e6ccf1, 7285cd76fd, 830669cb0c, 11e881b6ac
  db.orders.createIndex({email: 1, created: 1}, {name: "orders_esr_dadf97"})   # shapes ffcf70b5cd

Indexes to drop (verify usage on every member first)
  db.orders.dropIndex("created_1")

Query rewrites
  find {email:{$regex:?}}: anchor the pattern on email with ^, or use a text index for free-text search
  aggregate [{$match:{status:?}}, {$sort:{total:-1}}, {$group:{_id:$customer_id}}]: remove the $sort unless a $first or $last accumulator depends on that order; if it does, sort after the $group on the smaller result
  find {customer_id:{$in:[?]}}: split the list into batches, or store the relationship on the other side
  find {status:{$ne:?}}: add a positive predicate (equality or range), or model the state so the common case is an equality
analyze: 788 entries read, 20 shapes, 20 explains, 15 findings, 3.9s
```

## Install

```sh
pipx install indexwright
```

Or with Docker (distroless, runs as a non-root user):

```sh
docker build -t indexwright .
docker run --rm -e MONGODB_URI="mongodb://indexwright:<password>@host:27017/app?authSource=admin" \
  indexwright analyze --db app
```

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

`read` covers `system.profile`, `explain` and `listIndexes`; `clusterMonitor` covers
`$indexStats`, `top` and `listDatabases`. Nothing else is needed.

## Commands

```sh
export MONGODB_URI="mongodb://indexwright:<password>@host:27017/app?authSource=admin"

indexwright doctor                         # connectivity, version, privileges, profiler, $indexStats
indexwright shapes  --db app --since 24h   # query shapes with count, p50, p99, docs examined per returned
indexwright analyze --db app --since 24h   # findings, createIndex and dropIndex statements
indexwright indexes --db app               # index inventory with usage from every replica set member

indexwright analyze --log /var/log/mongodb/mongod.log --db app      # from the log, with the URI
indexwright analyze --log /var/log/mongodb/mongod.log.*.gz --db app # offline, no URI needed
indexwright analyze --db app --format json --out report.json        # machine readable
indexwright analyze --db app --format md   --out report.md          # short prioritized report
```

The profiler must be on: `db.setProfilingLevel(1, {slowms: 100})`. Its collection is capped at
1 MB by default; see [how it works](docs/how-it-works.md) for making it bigger.

Exit codes: `0` ok, `1` a doctor check failed or a `high`/`critical` finding exists, `2` cannot
connect or bad usage, `3` the user has write access.

### In cron

```sh
0 6 * * * indexwright analyze --db app --since 24h --format json --out /var/reports/indexwright-$(date +\%F).json || echo "indexwright: findings or error, exit $?"
```

Runs are idempotent and read-only: the tool's own reads are excluded from the profile it
analyzes. The JSON schema is documented in [docs/report-schema.md](docs/report-schema.md).

## What it does not do

- It does not create or drop anything. Statements are printed for you to run.
- No sharded cluster awareness yet: findings on a `mongos` are correct but the shard key is not
  considered when recommending an index.
- `explain` runs with the values from one real execution of each shape. A plan that changes with
  the values is explained once.
- An unanchored `$regex` cannot be fixed with an index; the tool tells you to rewrite it.
- `distinct` and `count` over a range are not covered by a rule yet.
- Logs must be JSON (MongoDB 4.4+). Older text logs are refused.
- `$indexStats` counters reset when a node restarts. `unused_index` says how many members reported
  and asks you to verify before dropping.

## More

- [How it works](docs/how-it-works.md): sources, shapes, counting, index usage across members.
- [Rules](docs/rules.md): what each of the twelve rules detects, why it matters, what it recommends.
- [Report schema](docs/report-schema.md): the JSON output.
- [Changelog](CHANGELOG.md).

MIT license.
