# JSON report schema

`indexwright analyze --format json` writes one JSON document. `schema_version` is `1`.
Within a schema version fields are only added, never renamed or removed. A rename or removal
bumps `schema_version`.

The report never contains query values: shapes are normalized, evidence holds numbers, plan
stage names, index names and field names.

## Top level

| field | type | meaning |
|---|---|---|
| `tool_version` | string | indexwright version that wrote the report |
| `schema_version` | integer | `1` |
| `generated_at` | string | ISO 8601, UTC |
| `source` | object | where the entries came from |
| `checks` | array | doctor checks that ran (empty when offline) |
| `summary` | object | counters for the run |
| `shapes` | array | every query shape seen, sorted by total time, most first |
| `findings` | array | every finding, sorted by severity then by the shape's total time |
| `recommendations` | object | the same recommendations, deduplicated |

## source

| field | type | meaning |
|---|---|---|
| `kind` | string | `profiler` or `log` |
| `since` | string | ISO 8601, entries older than this were ignored |
| `databases` | array or null | databases read from the profiler (`null` = all user databases) |
| `files` | array or null | log files read |

## checks[]

`name`, `status` (`ok`, `warn`, `fail`), `detail`. Same as the `doctor` table.

## summary

| field | type | meaning |
|---|---|---|
| `entries_read` | integer | profiler entries or log lines turned into entries |
| `shapes` | integer | number of shapes |
| `explains` | integer | explain calls made |
| `findings` | integer | number of findings |
| `offline` | boolean | true when run from a log without `--uri` |
| `members.total` | integer | replica set members (1 for a standalone) |
| `members.reached` | integer | members that answered `$indexStats` |
| `members.unreachable` | array | host:port of members that did not |

## shapes[]

| field | type | meaning |
|---|---|---|
| `fingerprint` | string | sha1 of the canonical shape, stable across runs and machines |
| `ns` | string | `database.collection` |
| `op` | string | `find`, `count`, `distinct`, `update`, `delete`, `findAndModify`, `aggregate` |
| `shape` | string | compact rendering, for example `{status:?} sort {created:-1}` |
| `count` | integer | executions (getMore batches are not counted) |
| `p50_ms`, `p99_ms`, `max_ms` | integer | latency percentiles over executions |
| `total_ms` | integer | total time including getMore batches |
| `docs_examined`, `keys_examined`, `nreturned` | integer | sums over all entries |
| `returned_known` | boolean | false when the server recorded no returned count (`distinct` on some versions) |
| `has_sort_stage` | boolean | any execution sorted in memory |
| `plan_summaries` | array | distinct `planSummary` values seen |
| `meta.in_size` | integer | largest `$in` list seen |
| `meta.unanchored_regex` | array | fields with a `$regex` not starting with `^` |
| `meta.projection_present` | boolean | any execution had a projection |
| `meta.output_reduced` | boolean | `count`, `distinct`, `$group`, `skip`: returned is not matched |
| `first_seen`, `last_seen` | string | ISO 8601 |

## findings[]

| field | type | meaning |
|---|---|---|
| `severity` | string | `critical`, `high`, `medium`, `low`, `info` |
| `rule` | string | rule name, see `rules.md` |
| `ns` | string | namespace the finding is about |
| `shape_id` | string | fingerprint of the shape, empty for index findings |
| `message` | string | plain explanation |
| `recommendation` | object or null | `kind` (`create_index`, `drop_index`, `rewrite`), `ns`, `statement`, `keys` (array of `[field, direction]`, empty unless `create_index`) |
| `evidence` | object | rule specific numbers and names |

## recommendations

| field | type | meaning |
|---|---|---|
| `create_index[]` | `statement`, `ns`, `keys`, `shape_ids` | one entry per distinct statement, with the shapes it serves |
| `drop_index[]` | `statement`, `ns` | one entry per index |
| `rewrite[]` | `statement`, `ns`, `shape_ids` | advice text with the shapes it applies to |

## Exit code

`analyze` exits `1` when any finding is `high` or `critical`, `0` otherwise, regardless of the
format. Connection and usage errors exit `2`, a user with write access exits `3`.
