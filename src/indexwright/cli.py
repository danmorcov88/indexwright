from __future__ import annotations

import logging
import sys
import time
from typing import TYPE_CHECKING, Annotated, NoReturn, TypeVar

import typer
from pymongo.errors import PyMongoError
from rich.console import Console
from rich.table import Table

from indexwright import __version__
from indexwright.doctor import run_checks
from indexwright.mongo import ConnectError, Settings, WriteAccessError, connect, mask_uri

if TYPE_CHECKING:
    from collections.abc import Callable

    from indexwright.doctor import Check
    from indexwright.mongo import Client

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
Db = Annotated[str | None, typer.Option("--db", help="Limit to one database")]


@app.command()
def doctor(ctx: typer.Context, uri: Uri, db: Db = None) -> None:
    """Check connectivity, version, privileges, profiler and $indexStats."""
    settings = Settings(uri=uri, timeout=ctx.obj, db=db)
    started = time.monotonic()
    checks = _with_client(settings, lambda client: run_checks(client, settings))
    stdout.print(_checks_table(checks))
    failed = sum(c.status == "fail" for c in checks)
    warned = sum(c.status == "warn" for c in checks)
    stderr.print(
        f"doctor: {len(checks)} checks, {failed} failed, {warned} warnings, "
        f"{time.monotonic() - started:.1f}s"
    )
    raise typer.Exit(EXIT_FINDINGS if failed else EXIT_OK)


def _with_client(settings: Settings, fn: Callable[[Client], T]) -> T:
    try:
        client = connect(settings)
    except ConnectError as exc:
        _fail(str(exc), EXIT_USAGE)
    try:
        return fn(client)
    except WriteAccessError as exc:
        _fail(f"refusing to continue, {exc}", EXIT_WRITE_ACCESS)
    except PyMongoError as exc:
        _fail(f"{type(exc).__name__}: {exc}", EXIT_USAGE)
    except KeyboardInterrupt:
        _fail("interrupted", EXIT_INTERRUPTED)
    finally:
        client.close()


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
