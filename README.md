# indexwright

Read-only performance advisor for self-hosted MongoDB.

It reads the profiler (or a `mongod` log), groups slow operations into query shapes, runs `explain` on each shape, and tells you which indexes to create (following the ESR rule) and which existing indexes are unused, redundant or duplicated. It never writes to your database. Output is a short report plus `createIndex` statements you run yourself.

MongoDB Atlas ships this as Performance Advisor. Self-hosted MongoDB does not. This fills the gap.

**Work in progress.** Nothing is released yet. Follow the [changelog](CHANGELOG.md).

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

## Check the setup

```sh
export MONGODB_URI="mongodb://indexwright:<password>@host:27017/app?authSource=admin"
indexwright doctor
```

Exit codes: `0` ok, `1` a check failed, `2` cannot connect, `3` the user has write access.
