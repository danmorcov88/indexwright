# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- `shapes` command: query shapes from `system.profile` with count, p50, p99 and documents examined per document returned.
- Query shape normalization: literals masked, keys sorted, `$or` clause order ignored, pipelines reduced to their indexable `$match`/`$sort`, stable sha1 fingerprint. 74 golden cases.
- `doctor` command: checks connectivity, server version, user privileges, topology, profiler status and `$indexStats` availability. Refuses to run with a user that has write access.
