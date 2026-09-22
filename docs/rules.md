# Rules

Every rule looks at one query shape with its profiler statistics, the `explain` plan for a
sample of that shape, and the indexes that exist on the collection. A rule returns findings;
a finding has a severity, a message and, when an index would help, a `createIndex` statement.

Severity comes from the profiler numbers: `critical` when p99 is above 1 s and the shape ran
more than 100 times, `high` when p99 is above 100 ms or the shape ran more than 1000 times,
`medium` otherwise. A rule can lower it, never raise it.

Rules own shapes in this order, so one bad shape gets one finding: a collection scan is always
reported as `collscan`; an in-memory sort as `sort_in_memory`; the two ratio rules only look at
what is left. The ratio rules also skip shapes whose output is not the set of matched documents
(`count`, `distinct`, `$group`, `$count`, `skip`, ...): there, "documents examined per document
returned" says nothing about the index.

## The ESR builder

Recommended indexes follow Equality, Sort, Range: equality fields first (`field: value`, `$eq`,
`$in`, `$all`), then the sort fields in sort order and direction, then range fields
(`$gt`, `$gte`, `$lt`, `$lte`, anchored `$regex`, `$exists: true`). Negations (`$ne`, `$nin`,
`$exists: false`, `$not`), unanchored regexes and operators an index cannot narrow are left out.
Recommended indexes that are a prefix of another recommended index are folded into the wider one. `$and` clauses are
merged; a top-level `$or` gets one index per branch. When an existing index already starts with
the recommended keys (same directions, or all flipped after the equality keys), nothing is
recommended and the finding says which index covers it.

## collscan

**Detects:** the winning plan contains `COLLSCAN`. Falls back to the profiler's `planSummary`
when the shape could not be explained.

**Why it matters:** every execution reads the whole collection. It gets slower as the
collection grows and evicts useful data from the cache.

**Recommends:** the ESR index for the shape. Severity is lowered to `low` when fewer than 100
documents are examined per execution, because a scan of a tiny collection is fine.

## sort_in_memory

**Detects:** a `SORT` stage in the plan (or `hasSortStage` in the profiler), and no collection
scan. `SORT_MERGE` does not count: it merges index-ordered streams.

**Why it matters:** the server loads every matching document, sorts it in memory (100 MB limit,
then it fails or spills to disk) and only then applies the limit.

**Recommends:** the ESR index, which puts the sort fields right after the equality fields so
the index returns documents already in order. When the `$sort` feeds a `$group` directly, an
index would only trade the sort for random fetches, so the advice is to remove the `$sort`
unless a `$first` or `$last` accumulator depends on the order.

## docs_examined_ratio

**Detects:** more than 10 documents examined per document returned, and the plan has a `FETCH`
stage with a residual filter, meaning the index found candidates but the server had to load
each document to apply the rest of the filter.

**Why it matters:** the index does not contain every filter field, so most fetched documents
are thrown away.

**Recommends:** the ESR index, which includes the missing fields.

## low_selectivity_index

**Detects:** more than 10 index keys examined per document returned, an index is used, and
there is no residual filter (otherwise `docs_examined_ratio` owns it).

**Why it matters:** the index is used but its bounds are wide: a range field comes before an
equality field, or a `$regex` is not anchored, so a large part of the index is walked.

**Recommends:** the ESR index. When the ideal index already exists, the finding says so and the
cause is in the message (for example an unanchored regex).

## unanchored_regex

**Detects:** a `$regex` whose pattern does not start with `^`. Reported next to any other
finding on the shape, because the fix is different.

**Why it matters:** only an anchored pattern gives the planner index bounds. Anything else walks
every key of the index, or every document. The ESR builder leaves such fields out, so no index
is recommended for them: on a real run an index on the field made the query slower, the planner
preferred walking the index over scanning the collection.

**Recommends:** anchor the pattern, or use a text index for free-text search.

## negation_predicate

**Detects:** a filter made only of negations: `$ne`, `$nin`, `$not`, `$exists: false`. It
owns the shape; `collscan` and the ratio rules stay quiet (`sort_in_memory` does not, an index on
the sort keys still helps).

**Why it matters:** an index can find what equals a value, not what does not. A negation
matches almost everything and cannot be narrowed.

**Recommends:** add a positive predicate, or model the state so the common case is an equality.

## large_in

**Detects:** `$in` with more than 200 values. This finding is added next to any other finding
on the shape, because the fix is different.

**Why it matters:** each value is one index bound; a huge list means a huge plan and a slow
first execution.

**Recommends:** batch the list, or store the relationship on the other side.

## lookup_no_index

**Detects:** a `$lookup` whose `foreignField` has no index on the foreign collection (`_id`
always has one). Pipeline-form lookups are skipped, their join key is not visible.

**Why it matters:** the foreign collection is scanned once per input document.

**Recommends:** `createIndex` on the foreign field of the foreign collection.

## Index hygiene rules

These run per collection over `listIndexes` and the merged `$indexStats` usage, not per query
shape. They run in this order and an index one rule proposes to drop is skipped by the next,
so each index gets at most one finding: `duplicate_index`, `redundant_index`, `unused_index`,
`too_many_indexes`. `_id_`, unique and TTL indexes are never proposed for dropping.

## duplicate_index

**Detects:** two indexes with the same key pattern, partial filter and collation.

**Why it matters:** every write maintains both, and only one is ever used. MongoDB refuses to
create such a pair since 4.2, so these are leftovers from before an upgrade.

**Recommends:** `dropIndex` on the extra one; a unique index is kept first, then the one with
the most recorded use.

## redundant_index

**Detects:** index A whose keys are a strict prefix of index B's keys, with the same
directions (a single key counts in either direction). Skipped when A is unique, partial,
sparse or TTL, or B is partial or sparse, because those mean different things.

**Why it matters:** B answers every query A can answer, so A only costs write time and memory.

**Recommends:** `dropIndex` on A.

## unused_index

**Detects:** zero operations on every replica set member that could be reached, and the
counters started more than `--unused-days` ago (default 30). Usage counters reset when a node
restarts, so the window matters.

**Why it matters:** an index nobody reads still costs every insert, update and delete.

**Recommends:** `dropIndex`, at severity `low`, because the counters are per node: secondaries
that serve reads have their own numbers. When a member could not be reached the message says
so. Verify on every member before dropping.

## too_many_indexes

**Detects:** more than 20 indexes on a collection.

**Why it matters:** each write maintains every index. Severity is `medium` when writes are at
least 30% of the collection's operations (from the `top` command), `low` otherwise.

**Recommends:** review; no statement.
