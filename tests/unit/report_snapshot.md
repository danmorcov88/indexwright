# indexwright report

Generated 2026-09-22 10:00 UTC by indexwright 0.1.0, from the profiler since 2026-09-21 10:00 UTC.

150 entries, 3 shapes, 6 findings.

## Doctor

- WARN profiler app: level 0

## Findings

| severity | rule | ns | shape | message |
|---|---|---|---|---|
| medium | collscan | app.orders | find {email:{$regex:?}} | collection scan, 5000 documents examined per execution, 500 per document returned |
| medium | negation_predicate | app.orders | find {status:{$ne:?}} | the filter on status only excludes values, an index cannot narrow a negation |
| medium | collscan | app.orders | find {customer_id:{$in:[?]}} | collection scan, 5000 documents examined per execution, 500 per document returned |
| medium | large_in | app.orders | find {customer_id:{$in:[?]}} | $in with up to 300 values, one index bound per value |
| low | unused_index | app.orders | index tags_1 | tags_1 {tags: 1} has not been used in 40 days |
| medium | collscan | app.orders | find {email:{$regex:?}} | same index wanted twice |

## Recommended indexes

```js
db.orders.createIndex({email: 1}, {name: "orders_esr_d67a46"})
db.orders.createIndex({customer_id: 1}, {name: "orders_esr_386569"})
```

## Indexes to drop

Verify usage on every replica set member first.

```js
db.orders.dropIndex("tags_1")
```

## Query rewrites

- app.orders `find {status:{$ne:?}}`: add a positive predicate (equality or range), or model the state so the common case is an equality
- app.orders `find {customer_id:{$in:[?]}}`: split the list into batches, or store the relationship on the other side
