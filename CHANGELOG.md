# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Query shape normalization: literals masked, keys sorted, `$or` clause order ignored, pipelines reduced to their indexable `$match`/`$sort`, stable sha1 fingerprint. 74 golden cases.
- `doctor` command: checks connectivity, server version, user privileges, topology, profiler status and `$indexStats` availability. Refuses to run with a user that has write access.
