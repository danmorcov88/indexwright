# How it works

```
system.profile ──▶ source ──▶ shape ──▶ aggregate ──▶ rules ──▶ report
```

## Source

`indexwright` reads the database profiler (`<db>.system.profile`). Enable it with
`db.setProfilingLevel(1, {slowms: 100})` to record only slow operations, or level 2 to record everything.

The reader walks the collection in reverse natural order (newest first) with a cursor,
stops at `--limit` entries per database and ignores entries older than `--since`.

It keeps `find`, `count`, `distinct`, `update`, `delete`, `findAndModify` and `aggregate`.
It skips inserts, admin commands, truncated commands, operations on `system.*` collections
and the tool's own reads (recognised by the `indexwright` application name).

`system.profile` is capped at 1 MB by default. On a busy server that holds seconds of history.
Make it bigger before relying on it:

```js
db.setProfilingLevel(0)
db.system.profile.drop()
db.createCollection("system.profile", {capped: true, size: 256 * 1024 * 1024})
db.setProfilingLevel(1, {slowms: 100})
```

## Shapes

A shape is a query with every value replaced by `?`, so that `{status: "new", n: {$gt: 5}}` and
`{n: {$gt: 9}, status: "done"}` are the same shape. Keys are sorted, `$or`/`$and` clause order is
ignored, sort order is kept because it matters for indexes.

A few things are recorded next to the shape instead of inside it, because they change per
execution: the largest `$in` list seen, which `$regex` fields were not anchored with `^`, whether a
projection was present, and whether the operation reduces its output (`count`, `distinct`,
`$group`, `skip`).

Aggregation pipelines keep the first `$match` and `$sort` that come before any stage that changes
documents (`$group`, `$project`, `$unwind`, `$lookup`, ...). Only those can use an index.

The fingerprint is the sha1 of the canonical JSON of the shape. It is stable across runs and
machines, so two reports can be compared.

## Counting

Entries are grouped by `(namespace, fingerprint)`. Latency percentiles use nearest-rank.

A `getMore` is attributed to the query that opened the cursor. It adds to documents examined and
returned, and to total time, but it is not counted as another execution and does not enter the
latency percentiles: the first batch's latency is the query's latency.

## Index usage

`$indexStats` counters live on each node and reset when it restarts. The tool asks `hello` for
the replica set members and queries every one of them through a direct connection built from
the same connection string (same credentials, `directConnection=true`). Operations are summed
and the earliest `since` is kept. A member that cannot be reached is named in the output rather
than silently skipped, and unused-index findings say how many members reported. Behind a
`mongos`, `$indexStats` already returns one document per shard; those are summed the same way.
