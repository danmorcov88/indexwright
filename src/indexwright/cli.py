from __future__ import annotations

import logging
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Annotated, Generic, NoReturn, TypeVar

import typer
from pymongo.errors import PyMongoError
from rich.console import Console
from rich.table import Table

from indexwright import __version__
from indexwright.aggregate import group
from indexwright.analysis import analyze as run_analysis
from indexwright.doctor import run_checks
from indexwright.indexes import inventory
from indexwright.mongo import ConnectError, Settings, WriteAccessError, connect, mask_uri
from indexwright.rules.esr import format_keys
from indexwright.shape import compact
from indexwright.source import LogFormatError, read_all, read_log

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Iterator

    from indexwright.analysis import Analysis
    from indexwright.doctor import Check
    from indexwright.model import CollectionIndexes, Entry, Finding, Shape, ShapeStats
    from indexwright.mongo import Connection

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_USAGE = 2
EXIT_WRITE_ACCESS = 3
EXIT_INTERRUPTED = 130

T = TypeVar("T")

app = typer.Typer(add_completion=False, no_args_is_help=True)
stdout = Console()
stderr = Console(stderr=True)

STATUS_STYLE = {"ok": "green", "warn": "yellow", "fail": "red"}
SEVERITY_STYLE = {
    "critical": "bold red",
    "high": "red",
    "medium": "yellow",
    "low": "cyan",
    "info": "dim",
}


def _print_version(value: bool) -> None:
    if value:
        stdout.print(f"indexwright {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    version: Annotated[
        bool, typer.Option("--version", callback=_print_version, is_eager=True)
    ] = False,
    log_level: Annotated[
        str, typer.Option("--log-level", help="debug, info, warning, error")
    ] = "warning",
    timeout: Annotated[float, typer.Option("--timeout", help="Seconds per server operation")] = 5.0,
) -> None:
    """Read-only performance advisor for self-hosted MongoDB."""
    logging.basicConfig(
        level=log_level.upper(), stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    ctx.obj = timeout


Uri = Annotated[str, typer.Option("--uri", envvar="MONGODB_URI", show_default=False)]
OptionalUri = Annotated[str | None, typer.Option("--uri", envvar="MONGODB_URI", show_default=False)]
Log = Annotated[
    list[str] | None,
    typer.Option("--log", help="Read mongod JSON log files instead of the profiler (.gz ok)"),
]
Db = Annotated[str | None, typer.Option("--db", help="Limit to one database")]
Since = Annotated[
    str, typer.Option("--since", help="Only profile entries newer than: 30m, 24h, 7d")
]
Limit = Annotated[int, typer.Option("--limit", help="Max profile entries to read per database")]
MaxExplains = Annotated[int, typer.Option("--max-explains", help="Max explain calls per run")]
UnusedDays = Annotated[
    int, typer.Option("--unused-days", help="Report indexes with no use for this many days")
]


@app.command()
def doctor(ctx: typer.Context, uri: Uri, db: Db = None) -> None:
    """Check connectivity, version, privileges, profiler and $indexStats."""
    settings = Settings(uri=uri, timeout=ctx.obj, db=db)
    started = time.monotonic()
    checks = _with_connection(settings, lambda conn: run_checks(_required(conn)))
    stdout.print(_checks_table(checks))
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    stderr.print(
        f"doctor: {len(checks)} checks, {failed} failed, {warned} warnings, "
        f"{time.monotonic() - started:.1f}s"
    )
    raise typer.Exit(EXIT_FINDINGS if failed else EXIT_OK)


@app.command()
def shapes(
    ctx: typer.Context,
    uri: OptionalUri = None,
    log: Log = None,
    db: Db = None,
    since: Since = "24h",
    limit: Limit = 50_000,
) -> None:
    """List query shapes from the profiler or a log file with counts and latencies. No rules."""
    settings = _settings(ctx, uri, db, log)
    cutoff = datetime.now(UTC) - _parse_since(since)
    started = time.monotonic()
    entries: _Counted[Entry] = _Counted()

    def collect(conn: Connection | None) -> list[ShapeStats]:
        return group(entries.wrap(_entries(conn, log, cutoff, limit, db)))

    stats = _with_connection(settings, collect)
    stdout.print(_shapes_table(stats))
    stderr.print(
        f"shapes: {entries.n} entries read, {len(stats)} shapes, {time.monotonic() - started:.1f}s"
    )


class _Counted(Generic[T]):
    def __init__(self) -> None:
        self.n = 0

    def wrap(self, items: Iterable[T]) -> Iterator[T]:
        for item in items:
            self.n += 1
            yield item


def _settings(
    ctx: typer.Context, uri: str | None, db: str | None, log: list[str] | None
) -> Settings | None:
    if uri:
        return Settings(uri=uri, timeout=ctx.obj, db=db)
    if log:
        return None
    raise typer.BadParameter("give --uri (or MONGODB_URI), --log, or both")


def _required(conn: Connection | None) -> Connection:
    assert conn is not None
    return conn


def _entries(
    conn: Connection | None, log: list[str] | None, cutoff: datetime, limit: int, db: str | None
) -> Iterable[Entry]:
    if log:
        return read_log(log, cutoff, limit, db)
    return read_all(_required(conn), cutoff, limit)


def _parse_since(text: str) -> timedelta:
    match = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not match:
        raise typer.BadParameter("use a number followed by m, h or d, for example 24h")
    amount, unit = int(match.group(1)), match.group(2)
    return timedelta(**{{"m": "minutes", "h": "hours", "d": "days"}[unit]: amount})


@app.command()
def analyze(
    ctx: typer.Context,
    uri: OptionalUri = None,
    log: Log = None,
    db: Db = None,
    since: Since = "24h",
    limit: Limit = 50_000,
    max_explains: MaxExplains = 200,
    unused_days: UnusedDays = 30,
) -> None:
    """Find slow query shapes and recommend indexes. Exit 1 when high findings exist."""
    settings = _settings(ctx, uri, db, log)
    cutoff = datetime.now(UTC) - _parse_since(since)
    started = time.monotonic()

    def run(conn: Connection | None) -> Analysis:
        entries = _entries(conn, log, cutoff, limit, db)
        return run_analysis(conn, entries, max_explains, unused_days)

    result = _with_connection(settings, run)
    if result.offline:
        stderr.print("offline: no explain, existing indexes unknown, index rules skipped")
    if result.failed_checks:
        stdout.print(_checks_table(result.checks))
        stderr.print("analyze: doctor checks failed, fix them first")
        raise typer.Exit(EXIT_FINDINGS)
    for check in result.checks:
        if check.status == "warn":
            stderr.print(f"warning: {check.name}: {check.detail}", soft_wrap=True)
    shapes = {s.shape.fingerprint: s.shape for s in result.shapes}
    stdout.print(_findings_table(result.findings, shapes))
    statements = result.create_index_statements()
    if statements:
        stdout.print()
        stdout.print("[bold]Recommended indexes[/bold]")
        for rec, shape_ids in statements:
            served = ", ".join(sid[:10] for sid in shape_ids)
            stdout.print(f"  {rec.statement}   [dim]# shapes {served}[/dim]", soft_wrap=True)
    drops = result.drop_index_statements()
    if drops:
        stdout.print()
        stdout.print("[bold]Indexes to drop[/bold] (verify usage on every member first)")
        for rec in drops:
            stdout.print(f"  {rec.statement}", soft_wrap=True)
    stderr.print(
        f"analyze: {result.entries_read} entries read, {len(result.shapes)} shapes, "
        f"{result.explains} explains, {len(result.findings)} findings, "
        f"{_members_note(result.members_reached, result.members_total, result.unreachable)}"
        f"{time.monotonic() - started:.1f}s"
    )
    raise typer.Exit(EXIT_FINDINGS if result.actionable else EXIT_OK)


def _members_note(reached: int, total: int, unreachable: list[str]) -> str:
    if total <= 1:
        return ""
    note = f"index usage from {reached}/{total} members"
    if unreachable:
        note += f" (unreachable: {', '.join(unreachable)})"
    return note + ", "


@app.command()
def indexes(ctx: typer.Context, uri: Uri, db: Db = None, unused_days: UnusedDays = 30) -> None:
    """Index inventory with usage from every replica set member. No rules."""
    settings = Settings(uri=uri, timeout=ctx.obj, db=db)
    started = time.monotonic()
    result = _with_connection(settings, lambda conn: inventory(_required(conn), unused_days))
    stdout.print(_indexes_table(result.collections))
    total = sum(len(c.indexes) for c in result.collections)
    report = result.report
    stderr.print(
        f"indexes: {total} indexes in {len(result.collections)} collections, "
        f"{_members_note(report.members_reached, report.members_total, report.unreachable)}"
        f"{time.monotonic() - started:.1f}s"
    )


def _indexes_table(collections: list[CollectionIndexes]) -> Table:
    table = Table(title="indexes", expand=True)
    table.add_column("ns", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("keys", ratio=1, overflow="fold")
    table.add_column("flags")
    table.add_column("ops", justify="right")
    table.add_column("since", no_wrap=True)
    for coll in collections:
        for index in coll.indexes:
            usage = coll.usage.get(index.name) if coll.usage else None
            flags = " ".join(
                name
                for name, on in (
                    ("unique", index.unique),
                    ("sparse", index.sparse),
                    ("partial", index.partial),
                    ("ttl", index.ttl),
                    ("hidden", index.hidden),
                )
                if on
            )
            ops = str(usage.ops) if usage else "n/a"
            since = usage.since.strftime("%Y-%m-%d") if usage else ""
            table.add_row(coll.ns, index.name, format_keys(index.keys), flags, ops, since)
    return table


def _findings_table(findings: list[Finding], shapes: dict[str, Shape]) -> Table:
    table = Table(title="findings", expand=True)
    table.add_column("severity", no_wrap=True)
    table.add_column("rule", no_wrap=True)
    table.add_column("ns", no_wrap=True)
    table.add_column("shape", ratio=1, overflow="fold")
    table.add_column("message", ratio=2, overflow="fold")
    for f in findings:
        style = SEVERITY_STYLE[f.severity]
        shape = shapes.get(f.shape_id)
        label = f"{shape.op} {compact(shape)}" if shape else f"index {f.evidence.get('index', '')}"
        table.add_row(f"[{style}]{f.severity}[/{style}]", f.rule, f.ns, label, f.message)
    if not findings:
        table.add_row("", "", "", "", "no findings")
    return table


def _shapes_table(stats: list[ShapeStats]) -> Table:
    table = Table(title="query shapes", expand=True)
    table.add_column("ns", no_wrap=True)
    table.add_column("op", no_wrap=True)
    table.add_column("shape", ratio=1, overflow="fold")
    table.add_column("n", justify="right")
    table.add_column("p50", justify="right")
    table.add_column("p99", justify="right")
    table.add_column("docs/ret", justify="right")
    for s in stats:
        table.add_row(
            s.shape.ns,
            s.shape.op,
            compact(s.shape),
            str(s.count),
            str(s.p50_ms),
            str(s.p99_ms),
            f"{s.docs_per_returned:.0f}",
        )
    return table


def _with_connection(settings: Settings | None, fn: Callable[[Connection | None], T]) -> T:
    conn = None
    if settings is not None:
        try:
            conn = connect(settings)
        except ConnectError as exc:
            _fail(str(exc), EXIT_USAGE)
        except WriteAccessError as exc:
            _fail(f"refusing to continue, {exc}", EXIT_WRITE_ACCESS)
    try:
        return fn(conn)
    except (PyMongoError, LogFormatError, OSError) as exc:
        _fail(f"{type(exc).__name__}: {exc}", EXIT_USAGE)
    except KeyboardInterrupt:
        _fail("interrupted", EXIT_INTERRUPTED)
    finally:
        if conn is not None:
            conn.close()


def _fail(message: str, code: int) -> NoReturn:
    stderr.print(f"[red]error:[/red] {mask_uri(message)}", highlight=False, soft_wrap=True)
    raise typer.Exit(code)


def _checks_table(checks: list[Check]) -> Table:
    table = Table(title="indexwright doctor", show_lines=False)
    table.add_column("check")
    table.add_column("status")
    table.add_column("detail")
    for check in checks:
        style = STATUS_STYLE[check.status]
        table.add_row(check.name, f"[{style}]{check.status.upper()}[/{style}]", check.detail)
    return table


if __name__ == "__main__":
    app()
