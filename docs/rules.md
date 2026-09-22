# Rules

Every rule looks at one query shape with its profiler statistics, the `explain` plan for a
sample of that shape, and the indexes that exist on the collection. A rule returns findings;
a finding has a severity, a message and, when an index would help, a `createIndex` statement.

Severity comes from the profiler numbers: `critical` when p99 is above 1 s and the shape ran
more than 100 times, `high` when p99 is above 100 ms or the shape ran more than 1000 times,
`medium` otherwise. A rule can lower it, never raise it.

Rules own shapes in this order, so one bad shape gets one finding: a collection scan is always
reported as `collscan`; an in-memory sort as `sort_in_memory`; the two ratio rules only look at
what is left.

## The ESR builder

Recommended indexes follow Equality, Sort, Range: equality fields first (`field: value`, `$eq`,
`$in`, `$all`), then the sort fields in sort order and direction, then range fields
(`$gt`, `$gte`, `$lt`, `$lte`, anchored `$regex`, `$exists: true`). Negations (`$ne`, `$nin`,
`$exists: false`, `$not`) and operators an index cannot narrow are left out. `$and` clauses are
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
the index returns documents already in order.

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
