# Prior art

- **Atlas Performance Advisor**: the reference for what this tool does, but Atlas only.
- **[lusqua/mongo-advisor](https://github.com/lusqua/mongo-advisor)** (JavaScript, 2026): HTTP service over `system.profile` and `$indexStats`. Single node, no `explain`, no ESR ordering, no redundant or duplicate index detection, no license.
- **dex** (PyPI, 2013) and **mtools** (2022): dex is unmaintained; mtools parses and plots log files but recommends nothing.
- A few "index advisor" repos on GitHub are LLM wrappers or notebooks. None is a maintained, deterministic, read-only CLI.
