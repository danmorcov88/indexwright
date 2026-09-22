# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Index hygiene rules `duplicate_index`, `redundant_index`, `unused_index`, `too_many_indexes`; `_id`, unique and TTL indexes are never proposed for dropping.
- Index usage read from every replica set member through `$indexStats` (ops summed, earliest `since`), with unreachable members reported instead of silently ignored.
- `analyze` command: doctor checks, then shapes, explain, rules, a findings table and deduplicated `createIndex` statements. Exit 1 when a `high` or `critical` finding exists.
- Rules `collscan`, `sort_in_memory`, `docs_examined_ratio`, `low_selectivity_index`. Each shape gets at most one owner rule, so one bad query yields one recommendation.
- ESR index builder: equality fields, then sort fields in sort order, then range fields; one candidate per `$or` branch; skipped when an existing index already covers the key sequence.
- `explain` runner: one `queryPlanner` explain per shape, throttled to 5 per second and 200 per run, parsed into plan stages, index names and residual filter fields. Understands classic, SBE and sharded plans.
- `shapes` command: query shapes from `system.profile` with count, p50, p99 and documents examined per document returned.
- Query shape normalization: literals masked, keys sorted, `$or` clause order ignored, pipelines reduced to their indexable `$match`/`$sort`, stable sha1 fingerprint. 74 golden cases.
- `doctor` command: checks connectivity, server version, user privileges, topology, profiler status and `$indexStats` availability. Refuses to run with a user that has write access.
