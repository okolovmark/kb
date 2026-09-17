"""kb command line: service and store (phase 0); records, tasks, standup, sync, migration."""

import contextlib
import datetime as dt
import errno
import functools
import json
import logging
import os
import subprocess
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, TypeVar

import click
from neo4j import Driver, ManagedTransaction
from neo4j.exceptions import DriverError, Neo4jError
from neo4j.graph import Node, Relationship
from neo4j.graph import Path as GraphPath
from neo4j.time import Date, DateTime, Duration, Time

from kb import (
    calendar,
    db,
    migrate,
    migrate_journal,
    migrate_nodes,
    records,
    service,
    sync_odoo,
    urgency,
)
from kb.config import Config, ConfigError, dump_toml, load, render_default, to_plain
from kb.paths import Paths, neo4j_package, neo4j_version
from kb.runner import Runner, SubprocessRunner, format_process_error

T = TypeVar("T")


def kb_version() -> str:
    """Installed kb version, ``0+unknown`` when kb is not installed as a package."""
    try:
        return version("kb")
    except PackageNotFoundError:
        return "0+unknown"


@dataclass
class App:
    """Per-invocation state shared by the commands: --json, config, runner, readiness waiter."""

    as_json: bool = False
    runner: Runner = field(default_factory=SubprocessRunner)
    home: Path = field(default_factory=Path.home)
    waiter: Callable[[], None] | None = None
    _cfg: Config | None = None

    @property
    def cfg(self) -> Config:
        """Config loaded once from the file; a ConfigError becomes a click error."""
        if self._cfg is None:
            try:
                self._cfg = load(home=self.home)
            except ConfigError as exc:
                raise click.ClickException(str(exc)) from exc
        return self._cfg

    @property
    def paths(self) -> Paths:
        """Filesystem layout for the loaded config."""
        return Paths.from_config(self.cfg, self.home)

    def driver(self) -> Driver:
        """A new driver for the configured bolt port; fails fast when nothing listens on it."""
        port = self.cfg.neo4j.bolt_port
        if not db.bolt_open(db.BOLT_HOST, port):
            raise click.ClickException(
                f"neo4j-kb is not reachable on {db.bolt_uri(port)}: kb status / kb service start"
            )
        return db.connect(self.cfg, auth_file=self.paths.auth_file)

    def package(self) -> Path:
        """The neo4j store path from KB_NEO4J_PACKAGE; a click error when it is unset."""
        try:
            return neo4j_package()
        except RuntimeError as exc:
            raise click.ClickException(str(exc)) from exc

    def wait(self) -> None:
        """Block until the database answers after a start; tests inject a fake."""
        if self.waiter is not None:
            self.waiter()
        else:
            service.wait_until_ready(self.cfg, self.paths.auth_file)

    def emit(self, data: dict[str, Any], text: str) -> None:
        """Print *data* as indented JSON under --json, else *text* (nothing when it is empty)."""
        if self.as_json:
            click.echo(json.dumps(data, ensure_ascii=False, indent=2))
        elif text:
            click.echo(text)

    @property
    def session(self) -> str | None:
        """``KB_SESSION`` from the environment: the session id events are tagged with."""
        return os.environ.get("KB_SESSION") or None

    def current_session(
        self, tx: ManagedTransaction, scopes: list[str] | None, at: dt.datetime
    ) -> records.Record | None:
        """The Session record this write belongs to (``KB_SESSION``, else the latest open one
        in the view); None when there is none."""
        return records.current_session(tx, scopes, at=at, env_id=self.session)

    def stamp_of(self, current: records.Record | None) -> str | None:
        """What ``Event.session`` gets: the resolved session's id, else the raw ``KB_SESSION``."""
        return current.session_id if current is not None else self.session

    def write(self, fn: Callable[[ManagedTransaction], T]) -> T:
        """Run *fn* in one managed write transaction on a fresh driver."""
        with self.driver() as driver, driver.session(database=db.DATABASE) as session:
            return session.execute_write(fn)

    def read(self, fn: Callable[[ManagedTransaction], T]) -> T:
        """Run *fn* in one managed read transaction on a fresh driver."""
        with self.driver() as driver, driver.session(database=db.DATABASE) as session:
            return session.execute_read(fn)


pass_app = click.make_pass_decorator(App)


@contextmanager
def cli_errors() -> Iterator[None]:
    """Turn subprocess, driver and file errors into one-line click errors."""
    try:
        yield
    except subprocess.CalledProcessError as exc:
        raise click.ClickException(format_process_error(exc)) from exc
    except FileNotFoundError as exc:
        raise click.ClickException(f"not found: {exc.filename or exc}") from exc
    # TimeoutError and PermissionError (chmod on a FUSE mount) are OSErrors too
    except (Neo4jError, DriverError, ValueError, OSError, service.RestoreError) as exc:
        raise click.ClickException(str(exc)) from exc


@click.group(help="kb: personal knowledge graph and todo on Neo4j.")
@click.version_option(kb_version(), prog_name="kb")
@click.option("--json", "as_json", is_flag=True, help="Structured output as JSON where available.")
@click.pass_context
def main(ctx: click.Context, as_json: bool) -> None:
    """Root group: creates the App unless a test injected one, records --json."""
    # the driver asks the server for no notifications; this catches any that slip through
    logging.getLogger("neo4j.notifications").setLevel(logging.ERROR)
    if ctx.obj is None:
        ctx.obj = App()
    ctx.obj.as_json = as_json


# --- status -----------------------------------------------------------------------------


@main.command(
    help="Service, bolt, schema, counter, versions; exit 1 when the service is down "
    "or the unit runs an older neo4j than the installed package."
)
@pass_app
def status(app: App) -> None:
    """Print unit, bolt, schema, counter, versions; exit 1 when unhealthy, incomplete or stale."""
    cfg = app.cfg
    with cli_errors():
        state = service.unit_state(app.runner)
    reachable = db.bolt_open(db.BOLT_HOST, cfg.neo4j.bolt_port)
    pkg = _neo4j_package_or_none()
    unit_pkg = service.unit_exec_package(app.paths.unit_file)
    # after `nix profile upgrade` the unit still runs the old store path until kb-setup
    upgrade_pending = pkg is not None and unit_pkg is not None and pkg != unit_pkg
    info: dict[str, Any] = {
        "unit": state,
        "bolt": db.bolt_uri(cfg.neo4j.bolt_port),
        "bolt_reachable": reachable,
        "schema_ok": None,
        "constraints": [],
        "counter": None,
        "neo4j": neo4j_version(pkg) if pkg else "unknown",
        "package": str(pkg) if pkg else None,
        "unit_package": str(unit_pkg) if unit_pkg else None,
        "upgrade_pending": upgrade_pending,
        "kb": kb_version(),
    }
    if reachable:
        _probe_schema(app, info)
    lines = [
        f"unit:         {info['unit']}",
        f"bolt:         {info['bolt']} {'reachable' if reachable else 'unreachable'}",
        f"schema:       {_schema_line(info)}",
        f"counter:      {info['counter']}",
        f"neo4j:        {info['neo4j']}",
        f"kb:           {info['kb']}",
    ]
    if upgrade_pending:
        lines.append(
            f"upgrade:      unit runs {neo4j_version(unit_pkg)}, package is "
            f"{neo4j_version(pkg)}: run kb-setup"
        )
    app.emit(info, "\n".join(lines))
    # an incomplete schema is as actionable as a pending upgrade: a release that adds a
    # constraint leaves the database short of it until kb-setup runs, and nothing else says so
    if state != "active" or not reachable or upgrade_pending or info["schema_ok"] is False:
        sys.exit(1)


def _probe_schema(app: App, info: dict[str, Any]) -> None:
    try:
        with app.driver() as driver:
            info["constraints"] = sorted(db.constraint_names(driver))
            info["schema_ok"] = db.schema_present(driver)
            info["counter"] = db.counter_value(driver)
    except (Neo4jError, DriverError, ValueError) as exc:
        info["error"] = str(exc)


def _schema_line(info: dict[str, Any]) -> str:
    if "error" in info:
        return f"error: {info['error']}"
    if info["schema_ok"] is None:
        return "unknown"
    return "ok" if info["schema_ok"] else "incomplete, run kb-setup"


def _neo4j_package_or_none() -> Path | None:
    try:
        return neo4j_package()
    except RuntimeError:
        return None


# --- config -----------------------------------------------------------------------------


@main.group(help="Show or initialise the config file.")
def config() -> None:
    """Group for the config subcommands."""
    pass


@config.command("show", help="Print the effective config (defaults merged with the file).")
@pass_app
def config_show(app: App) -> None:
    """Print the effective config as TOML, or as JSON under --json."""
    app.emit(to_plain(app.cfg), dump_toml(app.cfg).rstrip("\n"))


@config.command("init", help="Write the default config file if absent; never overwrites.")
@pass_app
def config_init(app: App) -> None:
    """Write the default config file; an existing file is left untouched."""
    path = app.paths.config_file
    if path.exists():
        click.echo(f"{path} exists, not overwritten")
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_default())
    click.echo(f"wrote {path}")


# --- service ----------------------------------------------------------------------------


@main.group("service", help="Control the neo4j-kb user service.")
def service_group() -> None:
    """Group for the neo4j-kb.service subcommands."""
    pass


def _service_action(name: str, help_text: str) -> Callable[[App], None]:
    @service_group.command(name, help=help_text)
    @pass_app
    def action(app: App) -> None:
        with cli_errors():
            service.systemctl(app.runner, name, service.UNIT_NAME)
            if name != "stop":
                app.wait()
            click.echo(service.unit_state(app.runner))

    return action


_service_action("start", "Start the service.")
_service_action("stop", "Stop the service.")
_service_action("restart", "Restart the service.")


@service_group.command("logs", help="Show the service journal.")
@click.option("-n", "lines", default=200, show_default=True, help="Lines to show.")
@click.option("-f", "--follow", is_flag=True, help="Keep following the journal.")
@pass_app
def service_logs(app: App, lines: int, follow: bool) -> None:
    """Show the unit journal; Ctrl-C ends --follow without an error."""
    try:
        with cli_errors():
            service.journalctl(app.runner, lines, follow)
    except KeyboardInterrupt:
        # Ctrl-C is how -f ends
        return


# --- query ------------------------------------------------------------------------------


@main.command(help="Run raw Cypher (read or write); one JSON object per record on stdout.")
@click.argument("cypher")
@click.option(
    "-p",
    "--param",
    "params",
    multiple=True,
    metavar="K=V",
    help="Query parameter; V is parsed as JSON when it is valid JSON, else a string. Repeatable.",
)
@pass_app
def query(app: App, cypher: str, params: tuple[str, ...]) -> None:
    """Run Cypher: one JSON object per record on stdout, update counters on stderr when any."""
    parameters = parse_params(params)
    try:
        with app.driver() as driver, driver.session(database=db.DATABASE) as session:
            result = session.run(cypher, parameters)
            for record in result:
                row = dict(zip(record.keys(), map(to_jsonable, record.values()), strict=True))
                click.echo(json.dumps(row, ensure_ascii=False))
            counters = result.consume().counters
    except (Neo4jError, DriverError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    if counters.contains_updates:
        click.echo(_counters_line(counters), err=True)


def parse_params(pairs: tuple[str, ...]) -> dict[str, Any]:
    """``K=V`` pairs to a parameter dict; V is JSON when it parses, else the raw string."""
    parameters: dict[str, Any] = {}
    for pair in pairs:
        key, sep, raw = pair.partition("=")
        if not sep or not key:
            raise click.BadParameter(f"expected K=V, got {pair!r}", param_hint="--param")
        try:
            parameters[key] = json.loads(raw)
        except json.JSONDecodeError:
            parameters[key] = raw
    return parameters


def _counters_line(counters: Any) -> str:
    names = (
        "nodes_created",
        "nodes_deleted",
        "relationships_created",
        "relationships_deleted",
        "properties_set",
        "labels_added",
        "labels_removed",
    )
    parts = [
        f"{n.replace('_', ' ')}: {getattr(counters, n)}" for n in names if getattr(counters, n)
    ]
    return ", ".join(parts)


_TEMPORAL = (Date, DateTime, Time, Duration)


def to_jsonable(value: Any) -> Any:
    """Driver values (graph, temporal, containers) to plain JSON; unknown types via str()."""
    for types, convert in _CONVERTERS:
        if isinstance(value, types):
            return convert(value)
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _node(node: Node) -> dict[str, Any]:
    return {
        "labels": sorted(node.labels),
        "element_id": node.element_id,
        "properties": {k: to_jsonable(v) for k, v in node.items()},
    }


def _relationship(rel: Relationship) -> dict[str, Any]:
    return {
        "type": rel.type,
        "element_id": rel.element_id,
        "start": rel.start_node.element_id if rel.start_node else None,
        "end": rel.end_node.element_id if rel.end_node else None,
        "properties": {k: to_jsonable(v) for k, v in rel.items()},
    }


def _path(path: GraphPath) -> dict[str, Any]:
    return {
        "nodes": [_node(n) for n in path.nodes],
        "relationships": [_relationship(r) for r in path.relationships],
    }


_CONVERTERS: tuple[tuple[Any, Callable[[Any], Any]], ...] = (
    (Node, _node),
    (Relationship, _relationship),
    (GraphPath, _path),
    (_TEMPORAL, lambda v: v.iso_format()),
    (dict, lambda v: {k: to_jsonable(x) for k, x in v.items()}),
    ((list, tuple), lambda v: [to_jsonable(x) for x in v]),
)


# --- backup / restore -------------------------------------------------------------------


@main.command(
    help="Stop the service, dump the database into [backup].dir, start it; prints the file."
)
@pass_app
def backup(app: App) -> None:
    """Stop, dump, start and wait for readiness; print the dump path."""
    with cli_errors():
        target = service.backup(
            app.runner, app.package(), app.paths, dt.date.today(), wait=app.wait
        )
    app.emit({"file": str(target)}, str(target))


@main.command(
    help="Stop the service, dump the current database to [backup].dir/pre-restore-<date>.dump, "
    "load FILE over it, start it. A failed load puts the pre-restore dump back."
)
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@pass_app
def restore(app: App, file: Path) -> None:
    """Stop, safety dump, load FILE, start and wait for readiness; print what was restored."""
    with cli_errors():
        safety = service.restore(
            app.runner,
            app.package(),
            app.paths,
            file,
            today=dt.date.today(),
            wait=app.wait,
            report=lambda line: click.echo(line, err=True),
        )
    # the safety path was already reported on stderr before the load
    app.emit({"restored": str(file), "pre_restore_dump": str(safety)}, f"restored {file}")


# --- phase 1 helpers --------------------------------------------------------------------


def json_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """A per-command ``--json`` that turns on ``App.as_json`` (``kb --json <cmd>`` works too)."""

    @click.option("--json", "as_json", is_flag=True, help="Structured output as JSON.")
    @functools.wraps(f)
    def wrapper(*args: Any, as_json: bool, **kwargs: Any) -> Any:
        if as_json:
            click.get_current_context().find_object(App).as_json = True
        return f(*args, **kwargs)

    return wrapper


def scope_option(f: Callable[..., Any]) -> Callable[..., Any]:
    """``--scope S``: the view is S plus global; default the cwd scope, everything outside."""
    return click.option(
        "--scope", "scope_flag", help="View: S plus global; default: the cwd scope, else all."
    )(f)


def parse_date(text: str, flag: str) -> dt.date:
    """``YYYY-MM-DD`` only; natural language stays with the caller."""
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise click.BadParameter(f"expected YYYY-MM-DD, got {text!r}", param_hint=flag) from exc


def scope_names(cfg: Config) -> tuple[str, ...]:
    return ("global", "personal", *cfg.scopes)


def check_scope(cfg: Config, name: str) -> str:
    if name not in scope_names(cfg):
        raise click.BadParameter(
            f"unknown scope {name!r}; one of {', '.join(scope_names(cfg))}", param_hint="--scope"
        )
    return name


def cwd_scope(cfg: Config) -> str | None:
    """The ``[scopes]`` name whose path contains the cwd, else None."""
    cwd = Path.cwd().resolve()
    for name, path in cfg.scopes.items():
        root = path.resolve()
        if cwd == root or root in cwd.parents:
            return name
    return None


def resolve_scope(cfg: Config, flag: str | None) -> str | None:
    """``--scope`` wins over the cwd; None outside every configured path."""
    return check_scope(cfg, flag) if flag else cwd_scope(cfg)


def view_scopes(cfg: Config, flag: str | None) -> tuple[list[str] | None, str]:
    """The scope filter for today/next/index: ``(scopes or None for all, label)``."""
    scope = resolve_scope(cfg, flag)
    if scope is None:
        return None, "all"
    if scope == "global":
        return ["global"], "global"
    return [scope, "global"], scope


def write_scope(cfg: Config, flag: str | None) -> str:
    scope = resolve_scope(cfg, flag)
    if scope is None:
        raise click.UsageError("outside every configured scope, pass --scope")
    return scope


def read_body(body: str | None, body_file: Path | None) -> str | None:
    """``--body TEXT`` (``-`` = stdin) or ``--body-file F``; None when neither was given."""
    if body is not None and body_file is not None:
        raise click.UsageError("--body and --body-file are exclusive")
    if body_file is not None:
        return body_file.read_text()
    if body == "-":
        return sys.stdin.read()
    return body


def split_links(
    links: list[records.Link], scopes: list[str] | None
) -> tuple[list[records.Link], int]:
    """(links whose other record is in the view, count of the hidden ones)."""
    shown = [lk for lk in links if scopes is None or lk.other.scope in scopes]
    return shown, len(links) - len(shown)


def hidden_line(count: int) -> str:
    return f"{count} linked records in other scopes (hidden)"


def record_line(rec: records.Record, extra: str = "") -> str:
    slug = f"  ({rec.slug})" if rec.slug else ""
    return f"[{rec.id}] {rec.title}{slug}{extra}"


def emit_record(app: App, rec: records.Record, extra: str = "", **more: Any) -> None:
    app.emit({**rec.to_json(), **more}, record_line(rec, extra))


def stamp(at: dt.datetime | None) -> str:
    return f"{at.astimezone(dt.UTC):%Y-%m-%d %H:%M}Z" if at else "-"


def touch_session(
    tx: ManagedTransaction,
    current: records.Record | None,
    at: dt.datetime,
    *record_ids: int,
    created: bool = False,
) -> None:
    """Link the records to the current session: ``OPENED_IN`` for a record created now,
    ``TOUCHED`` otherwise; nothing without a session."""
    if current is None:
        return
    for rid in record_ids:
        records.attach(tx, current, rid, "OPENED_IN" if created else "TOUCHED", at)


def build_schedule(
    *,
    kind: str,
    scope: str,
    today: dt.date,
    due: str | None,
    plan: str | None,
    window: str | None,
    once: bool,
    repeat: str | None,
    anchor: str | None,
    days: str | None,
    escalation: str | None,
) -> dict[str, Any] | None:
    """Schedule properties from the add flags; None for a non-task kind (which allows none)."""
    given = [
        n
        for n, v in (("--due", due), ("--plan", plan), ("--window", window), ("--once", once))
        if v
    ]
    if kind != "task":
        if given or repeat or anchor or days or escalation:
            raise click.UsageError(f"schedule flags apply to tasks only, not to a {kind}")
        return None
    if len(given) > 1:
        raise click.UsageError(f"{given[0]} and {given[1]} are exclusive")
    if repeat and not (due or plan):
        raise click.UsageError("--repeat requires --due or --plan (a window recurs by its rrule)")
    sched: dict[str, Any] = {"days": days or records.default_days(scope)}
    if due:
        sched.update(sched="hard", due=parse_date(due, "--due"))
    elif plan:
        sched.update(sched="soft", due=parse_date(plan, "--plan"))
    elif window:
        # the rrule starts today in the kb timezone; created_at is UTC and may be a day off
        sched.update(sched="window", window_rrule=records.window_rrule(window), anchor=today)
    elif once:
        sched["sched"] = "once"
    else:
        sched.update(sched="age", anchor=parse_date(anchor, "--anchor") if anchor else today)
    if anchor and sched["sched"] != "age":
        raise click.UsageError("--anchor applies to age tasks (no --due/--plan/--window/--once)")
    if repeat:
        sched["repeat"] = records.check_repeat(repeat)
    if escalation:
        sched["escalation"] = records.check_escalation(escalation)
    return sched


def parse_link(spec: str) -> tuple[str, str]:
    rel, sep, ref = spec.partition(":")
    if not sep or not rel or not ref:
        raise click.BadParameter(f"expected REL:REF, got {spec!r}", param_hint="--link")
    return records.check_rel_type(rel), ref


# --- add / append / edit ----------------------------------------------------------------


ADD_KINDS = tuple(k for k in records.KINDS if k != "session")  # sessions: kb session open


@main.command(help="Create a record (a task unless --kind); prints [id] title (slug).")
@click.argument("title")
@click.option("--kind", "-k", type=click.Choice(ADD_KINDS), default="task", show_default=True)
@click.option("--scope", "scope_flag", help="global, personal or a [scopes] name; default: cwd.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--body", help="Body text; '-' reads stdin.")
@click.option("--summary", help="One line shown by index and show.")
@click.option("--slug", help="Non-task kinds only; derived from the title when omitted.")
@click.option("--pin/--no-pin", "pin", default=None, help="Default: pinned for feedback only.")
@click.option("--tag", "tags", multiple=True, help="Repeatable.")
@click.option("--link", "links", multiple=True, metavar="REL:REF", help="Repeatable.")
@click.option("--due", metavar="DATE", help="Hard deadline (screams on and after the date).")
@click.option("--plan", metavar="DATE", help="Soft date (asks to rebalance or close after it).")
@click.option("--window", metavar="W", help="weekend | sat,sun | RRULE (FREQ=...).")
@click.option("--once", is_flag=True, help="Tell me once: done after the first standup shows it.")
@click.option("--repeat", metavar="R", help="calendar:<rrule> | interval:<N>d, with --due/--plan.")
@click.option("--anchor", metavar="DATE", help="Age anchor (default today).")
@click.option("--days", type=click.Choice(records.DAYS), help="Default: work/off/any by scope.")
@click.option("--escalation", metavar="JSON", help='e.g. {"whisper": 3, "loud": 1}.')
@click.option("--source", type=click.Choice(records.SOURCES), default="manual", show_default=True)
@click.option("--external-id", help="e.g. KIO-1700 with --source odoo.")
@click.option("--journal-ref", metavar="R", help="e.g. journal/2026-09-10.")
@json_option
@pass_app
def add(
    app: App,
    *,
    title: str,
    kind: str,
    scope_flag: str | None,
    body_file: Path | None,
    body: str | None,
    summary: str | None,
    slug: str | None,
    pin: bool | None,
    tags: tuple[str, ...],
    links: tuple[str, ...],
    due: str | None,
    plan: str | None,
    window: str | None,
    once: bool,
    repeat: str | None,
    anchor: str | None,
    days: str | None,
    escalation: str | None,
    source: str,
    external_id: str | None,
    journal_ref: str | None,
) -> None:
    """Create the record, its created event and any --link edges in one transaction."""
    cfg = app.cfg
    scope = write_scope(cfg, scope_flag)
    scopes, _ = view_scopes(cfg, scope_flag)
    today = calendar.today(cfg)
    at = calendar.now(app.cfg)
    text = read_body(body, body_file) or ""
    with cli_errors():
        schedule = build_schedule(
            kind=kind,
            scope=scope,
            today=today,
            due=due,
            plan=plan,
            window=window,
            once=once,
            repeat=repeat,
            anchor=anchor,
            days=days,
            escalation=escalation,
        )
        link_specs = [parse_link(spec) for spec in links]
        if slug and kind == "task":
            raise click.UsageError("--slug applies to non-task kinds")
        if pin is not None and kind == "task":
            raise click.UsageError("--pin/--no-pin apply to non-task kinds")
        unique_tags = list(dict.fromkeys(tags))

        def work(tx: ManagedTransaction) -> records.Record:
            current = app.current_session(tx, scopes, at)
            rec = records.create(
                tx,
                kind=kind,
                scope=scope,
                title=title,
                at=at,
                body=text,
                summary=records.check_summary(summary),
                slug=slug,
                pinned=pin,
                tags=unique_tags,
                source=source,
                external_id=external_id,
                journal_ref=journal_ref,
                schedule=schedule,
                session=app.stamp_of(current),
            )
            for rel, ref in link_specs:
                target = records.resolve(tx, ref, scopes)
                records.link(tx, rec, rel, target, at)
                records.add_event(
                    tx,
                    rec.id,
                    "linked",
                    at,
                    payload={"type": rel, "target": target.id},
                    session=app.stamp_of(current),
                )
                # linking from add touches the target, exactly as kb link does
                touch_session(tx, current, at, target.id)
            touch_session(tx, current, at, rec.id, created=True)
            return rec

        rec = app.write(work)
    emit_record(app, rec)


@main.command(help="Append a dated paragraph to the body.")
@click.argument("ref")
@click.argument("text")
@scope_option
@json_option
@pass_app
def append(app: App, ref: str, text: str, scope_flag: str | None) -> None:
    """body += ``\\n\\n**<today>:** TEXT``; edited event."""
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)
    at = calendar.now(cfg)

    def work(tx: ManagedTransaction) -> records.Record:
        rec = records.resolve(tx, ref, scopes)
        current = app.current_session(tx, scopes, at)
        records.add_event(
            tx,
            rec.id,
            "edited",
            at,
            payload={"fields": ["body"], "append": True, "before": {"body": rec.body}},
            session=app.stamp_of(current),
        )
        touch_session(tx, current, at, rec.id)
        # a paragraph is a summary: appending to a session clears its no-summary flag
        extra = {"no_summary": False} if rec.kind == "session" else {}
        return records.touch(tx, rec.id, at, body=append_paragraph(rec.body, today, text), **extra)

    with cli_errors():
        rec = app.write(work)
    emit_record(app, rec)


def append_paragraph(body: str, today: dt.date, text: str) -> str:
    """body += ``\\n\\n**<today>:** TEXT``."""
    paragraph = f"**{today}:** {text}"
    return f"{body}\n\n{paragraph}" if body else paragraph


@main.command(help="Change the title, summary or body; with no flag, open $EDITOR on the body.")
@click.argument("ref")
@click.option("--title")
@click.option("--summary", help="One line; an empty string clears it.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--body", help="Body text; '-' reads stdin.")
@scope_option
@json_option
@pass_app
def edit(
    app: App,
    ref: str,
    *,
    title: str | None,
    summary: str | None,
    body_file: Path | None,
    body: str | None,
    scope_flag: str | None,
) -> None:
    """Write only what changed; an edited event only when something did."""
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)
    new_body = read_body(body, body_file)
    with cli_errors():
        rec = app.read(lambda tx: records.resolve(tx, ref, scopes))
        if title is None and summary is None and new_body is None:
            edited = click.edit(rec.body, extension=".md")
            new_body = edited if edited is not None else rec.body
            if new_body.endswith("\n") and not rec.body.endswith("\n"):
                new_body = new_body.rstrip("\n")
        changes: dict[str, Any] = {}
        if title is not None and records.check_title(title) != rec.title:
            changes["title"] = records.check_title(title)
        if summary is not None and records.check_summary(summary) != rec.summary:
            changes["summary"] = records.check_summary(summary)
        if new_body is not None and new_body != rec.body:
            changes["body"] = new_body
        if not changes:
            emit_record(app, rec, "  unchanged")
            return

        before = {field_name: getattr(rec, field_name) for field_name in changes}
        if rec.kind == "task":
            # an edit is a sign of life: the window count starts over, like done and snooze
            changes.update(skips=0, backlog=False)
        if rec.kind == "session" and "body" in changes:
            # the summary flag follows the body however the body was written
            changes["no_summary"] = not changes["body"].strip()

        def work(tx: ManagedTransaction) -> records.Record:
            current = app.current_session(tx, scopes, at)
            records.add_event(
                tx,
                rec.id,
                "edited",
                at,
                payload={"fields": sorted(before), "before": before},
                session=app.stamp_of(current),
            )
            touch_session(tx, current, at, rec.id)
            return records.touch(tx, rec.id, at, **changes)

        rec = app.write(work)
    emit_record(app, rec)


# --- show / search / links / graph / log ------------------------------------------------


@main.command(help="Header, schedule, urgency, body, links and the last 10 events.")
@click.argument("ref")
@scope_option
@json_option
@pass_app
def show(app: App, ref: str, scope_flag: str | None) -> None:
    """Print one record; a task's window bookkeeping runs on the way."""
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)
    at = calendar.now(cfg)

    def work(
        tx: ManagedTransaction,
    ) -> tuple[records.Record, list[records.Event], list[records.Link]]:
        rec = records.resolve(tx, ref, scopes)
        events = records.events_of(tx, rec.id)
        if rec.kind == "task":
            rec, events = records.settle_window(
                tx, rec, events=events, today=today, cfg=cfg, at=at, session=app.session
            )
        return rec, events, records.links_of(tx, rec.id)

    with cli_errors():
        rec, events, links = app.write(work)
    links, hidden = split_links(links, scopes)
    urgency_info = urgency.as_json(rec, today, cfg, events) if rec.kind == "task" else None
    if app.as_json:
        app.emit(
            {
                **rec.to_json(),
                "urgency": urgency_info,
                "links": [
                    {
                        "type": lk.type,
                        "direction": lk.direction,
                        "id": lk.other.id,
                        "title": lk.other.title,
                    }
                    for lk in links
                ],
                "hidden_links": hidden,
                "events": [e.to_json() for e in events[:10]],
            },
            "",
        )
        return
    lines = [
        f"[{rec.id}] {rec.title}",
        *([rec.summary] if rec.summary else []),
        f"kind: {rec.kind}  scope: {rec.scope}  slug: {rec.slug or '-'}  "
        f"tags: {', '.join(rec.tags) or '-'}  pinned: {'yes' if rec.pinned else 'no'}  "
        f"source: {rec.source}"
        + (f"  external: {rec.external_id}" if rec.external_id else "")
        + (f"  stage: {rec.stage}" if rec.stage else ""),
    ]
    if rec.kind == "task":
        lines.append(f"schedule: {urgency.schedule_line(rec)}")
        if urgency_info and rec.done_at is None:
            lines.append(
                f"urgency: {urgency.describe(urgency_info['level'], urgency_info['reason'])}"
            )
    if rec.kind == "session":
        lines.append(session_state_line(rec))
    meta = f"created: {stamp(rec.created_at)}  updated: {stamp(rec.updated_at)}"
    if rec.archived_at:
        meta += f"  archived: {stamp(rec.archived_at)}"
    if rec.journal_ref:
        meta += f"  journal: {rec.journal_ref}"
    lines.append(meta)
    if rec.body:
        lines += ["", rec.body.rstrip("\n")]
    lines += link_section(rec, links, hidden)
    if events:
        lines += ["", "events:"]
        lines += [f"  {event_line(e)}" for e in events[:10]]
    click.echo("\n".join(lines))


def session_state_line(rec: records.Record) -> str:
    state = f"closed: {stamp(rec.closed_at)}" if rec.closed_at else "open"
    line = f"session: {rec.session_id}  opened: {stamp(rec.opened_at)}  {state}"
    if rec.reason:
        line += f"  reason: {rec.reason}"
    if rec.no_summary:
        line += "  no summary"
    return line


def link_section(rec: records.Record, links: list[records.Link], hidden: int) -> list[str]:
    """The ``links:`` lines; a session's OPENED_IN / TOUCHED neighbours come first, grouped as
    ``created here`` / ``touched here``."""
    out: list[str] = []
    rest = links
    if rec.kind == "session":
        created = [lk for lk in links if lk.type == "OPENED_IN" and lk.direction == "<-"]
        touched = [lk for lk in links if lk.type == "TOUCHED" and lk.direction == "->"]
        rest = [lk for lk in links if lk not in created and lk not in touched]
        for title, group in (("created here:", created), ("touched here:", touched)):
            if group:
                out += ["", title, *(f"  {record_line(lk.other)}" for lk in group)]
    if rest or hidden:
        out += [
            "",
            "links:",
            *(f"  {lk.type} {lk.direction} {record_line(lk.other)}" for lk in rest),
        ]
        if hidden:
            out.append(f"  {hidden_line(hidden)}")
    return out


def event_line(event: records.Event) -> str:
    parts = [stamp(event.at), event.kind]
    if event.note:
        parts.append(event.note)
    if event.payload:
        parts.append(event.payload)
    if event.session:
        parts.append(f"session={event.session}")
    return "  ".join(parts)


@main.command(help="Full-text search over title and body (Lucene syntax).")
@click.argument("query_text", metavar="QUERY")
@click.option("--scope", "scope_flag")
@click.option("--kind", type=click.Choice(records.KINDS))
@click.option("--all", "include_archived", is_flag=True, help="Include archived records.")
@click.option("--limit", default=20, show_default=True)
@json_option
@pass_app
def search(
    app: App,
    *,
    query_text: str,
    scope_flag: str | None,
    kind: str | None,
    include_archived: bool,
    limit: int,
) -> None:
    """Lines ``[id] kind slug title  score``; the scope filter of today/index applies."""
    cfg = app.cfg
    scopes, _label = view_scopes(cfg, scope_flag)
    with cli_errors():
        hits = app.read(
            lambda tx: records.search(
                tx,
                query_text,
                scopes=scopes,
                kind=kind,
                include_archived=include_archived,
                limit=limit,
            )
        )
    app.emit(
        {"hits": [{**r.to_json(), "score": s} for r, s in hits]},
        "\n".join(f"[{r.id}] {r.kind} {r.slug or '-'} {r.title}  {s:.2f}" for r, s in hits),
    )


@main.command(help="Relationships of a record, grouped by type, with direction.")
@click.argument("ref")
@scope_option
@json_option
@pass_app
def links(app: App, ref: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    with cli_errors():
        rec, found = app.read(lambda tx: _with_links(tx, ref, scopes))
    shown, hidden = split_links(found, scopes)
    lines = [f"{lk.type} {lk.direction} {record_line(lk.other)}" for lk in shown]
    if hidden:
        lines.append(hidden_line(hidden))
    app.emit(
        {"id": rec.id, "links": [link_json(lk) for lk in shown], "hidden_links": hidden},
        "\n".join(lines),
    )


def _with_links(
    tx: ManagedTransaction, ref: str, scopes: list[str] | None
) -> tuple[records.Record, list[records.Link]]:
    rec = records.resolve(tx, ref, scopes)
    return rec, records.links_of(tx, rec.id)


def link_json(link: records.Link) -> dict[str, Any]:
    return {
        "type": link.type,
        "direction": link.direction,
        "hop": link.hop,
        "id": link.other.id,
        "slug": link.other.slug,
        "title": link.other.title,
    }


@main.command(help="Records within N hops over the vocabulary, grouped by hop and type.")
@click.argument("ref")
@click.option("--depth", default=2, show_default=True, type=click.IntRange(1, 6))
@scope_option
@json_option
@pass_app
def graph(app: App, ref: str, depth: int, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)

    def work(tx: ManagedTransaction) -> tuple[records.Record, list[records.Link]]:
        rec = records.resolve(tx, ref, scopes)
        return rec, records.graph_of(tx, rec.id, depth)

    with cli_errors():
        rec, found = app.read(work)
    shown, hidden = split_links(found, scopes)
    lines: list[str] = []
    for hop in sorted({lk.hop for lk in shown}):
        lines.append(f"hop {hop}:")
        lines += [
            f"  {lk.type} {lk.direction} {record_line(lk.other)}" for lk in shown if lk.hop == hop
        ]
    if hidden:
        lines.append(hidden_line(hidden))
    app.emit(
        {
            "id": rec.id,
            "depth": depth,
            "nodes": [link_json(lk) for lk in shown],
            "hidden_links": hidden,
        },
        "\n".join(lines),
    )


@main.command(help="Events of a record, newest first.")
@click.argument("ref")
@click.option("--limit", default=20, show_default=True)
@scope_option
@json_option
@pass_app
def log(app: App, ref: str, limit: int, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)

    def work(tx: ManagedTransaction) -> tuple[records.Record, list[records.Event]]:
        rec = records.resolve(tx, ref, scopes)
        return rec, records.events_of(tx, rec.id, limit)

    with cli_errors():
        rec, events = app.read(work)
    app.emit(
        {"id": rec.id, "events": [e.to_json() for e in events]},
        "\n".join(event_line(e) for e in events),
    )


# --- done / skip / snooze ---------------------------------------------------------------


@main.command(help="Close a task (recurring and window tasks advance and stay open).")
@click.argument("ref")
@click.argument("comment", required=False)
@scope_option
@json_option
@pass_app
def done(app: App, ref: str, comment: str | None, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)
    at = calendar.now(cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, dt.date | None, bool]:
        rec = records.resolve(tx, ref, scopes)
        if rec.kind != "task":
            raise records.RecordError(f"[{rec.id}] is a {rec.kind}; done applies to tasks")
        current = app.current_session(tx, scopes, at)
        rec, nxt, written = records.mark_done(
            tx, rec, comment=comment, today=today, cfg=cfg, at=at, session=app.stamp_of(current)
        )
        if written:
            touch_session(tx, current, at, rec.id)
        return rec, nxt, written

    with cli_errors():
        rec, nxt, written = app.write(work)
    if not written:
        emit_record(app, rec, "  already done", next=None, written=False)
        return
    extra = f"  · next {nxt}" if nxt else "  · done"
    emit_record(app, rec, extra, next=nxt.isoformat() if nxt else None, written=True)


@main.command(help="Skip the current (or last) window of a window task.")
@click.argument("ref")
@scope_option
@json_option
@pass_app
def skip(app: App, ref: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)
    at = calendar.now(cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, dt.date | None]:
        rec = records.resolve(tx, ref, scopes)
        current = app.current_session(tx, scopes, at)
        rec, end = records.skip_window(
            tx, rec, today=today, cfg=cfg, at=at, session=app.stamp_of(current)
        )
        if end is not None:
            touch_session(tx, current, at, rec.id)
        return rec, end

    with cli_errors():
        rec, end = app.write(work)
    if end is None:
        emit_record(app, rec, "  window already closed", skipped=None)
        return
    state = "  · backlog" if rec.backlog else ""
    emit_record(
        app,
        rec,
        f"  · skipped window ending {end}, skips {rec.skips}{state}",
        skipped=end.isoformat(),
    )


@main.command(help="Silence a task until DATE (default tomorrow); never past a hard deadline.")
@click.argument("ref")
@click.argument("until", required=False)
@scope_option
@json_option
@pass_app
def snooze(app: App, ref: str, until: str | None, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)
    at = calendar.now(cfg)
    if until is None or until == "tomorrow":
        target = today + dt.timedelta(days=1)
    else:
        target = parse_date(until, "UNTIL")

    def work(tx: ManagedTransaction) -> records.Record:
        rec = records.resolve(tx, ref, scopes)
        current = app.current_session(tx, scopes, at)
        rec = records.snooze(tx, rec, target, today=today, at=at, session=app.stamp_of(current))
        touch_session(tx, current, at, rec.id)
        return rec

    with cli_errors():
        rec = app.write(work)
    emit_record(app, rec, f"  · snoozed until {target}")


# --- today / next / why -----------------------------------------------------------------


NO_SUMMARY_DAYS = 7
NO_SUMMARY_SLUGS = 3


def no_summary_line(pending: list[records.Record]) -> str:
    """``N sessions without summary: <slugs> — write them: kb session close ...``.

    At most ``NO_SUMMARY_SLUGS`` slugs: a backlog of twenty must not push the standup's tasks
    off the screen.
    """
    noun = "session" if len(pending) == 1 else "sessions"
    sid = pending[0].session_id if len(pending) == 1 else "<session_id>"
    named = pending[:NO_SUMMARY_SLUGS]
    slugs = ", ".join(s.slug or "-" for s in named)
    if len(pending) > len(named):
        slugs += f" … and {len(pending) - len(named)} more"
    return (
        f"{len(pending)} {noun} without summary: {slugs} — "
        f"write them: kb session close --id {sid} --body-file <f>"
    )


def standup(
    app: App,
    scope_flag: str | None,
    *,
    min_level: int,
    record_shown: bool,
    name: str,
    sessions_line: bool = False,
) -> None:
    """Shared body of ``today`` and ``next``; never exits non-zero (the hook depends on it)."""
    try:
        cfg = app.cfg
        scopes, label = view_scopes(cfg, scope_flag)
        today = calendar.today(cfg)
        at = calendar.now(cfg)

        def work(
            tx: ManagedTransaction,
        ) -> tuple[dict[str, Any] | None, list[records.Assessment], list[records.Record]]:
            meta = records.get_meta(tx, sync_odoo.META_NAME)
            rows = records.assess(
                tx, records.open_tasks(tx, scopes), today=today, cfg=cfg, at=at, session=app.session
            )
            if record_shown:
                for row in rows:
                    if row.level >= urgency.NORMAL:
                        records.add_event(tx, row.task.id, "shown", at, session=app.session)
                        if row.task.sched == "once":
                            records.mark_done(
                                tx,
                                row.task,
                                comment="shown",
                                today=today,
                                cfg=cfg,
                                at=at,
                                session=app.session,
                            )
            pending = (
                records.sessions_without_summary(
                    tx, scopes, at - dt.timedelta(days=NO_SUMMARY_DAYS)
                )
                if sessions_line
                else []
            )
            return meta, rows, pending

        meta, rows, pending = app.write(work)
    except (click.ClickException, Neo4jError, DriverError, OSError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        if app.as_json:
            click.echo(json.dumps({"error": message, "tasks": []}))
        else:
            click.echo(f"kb {name}: {message}", err=True)
        return
    shown = [r for r in rows if r.level >= min_level]
    quiet = sum(1 for r in rows if r.level == urgency.WHISPER) if min_level > urgency.WHISPER else 0
    failed = meta is not None and meta.get("ok") is False
    if app.as_json:
        app.emit(
            {
                "date": today.isoformat(),
                "scope": label,
                "odoo": {**meta, "at": stamp(meta.get("at"))} if meta else None,
                "tasks": [
                    {
                        "id": r.task.id,
                        "level": r.level,
                        "name": r.name,
                        "reason": r.reason,
                        "title": r.task.title,
                        "scope": r.task.scope,
                        "sched": r.task.sched,
                        "due": r.task.due.isoformat() if r.task.due else None,
                    }
                    for r in shown
                ],
                "quiet": quiet,
                "no_summary": [
                    {
                        "id": s.id,
                        "slug": s.slug,
                        "session_id": s.session_id,
                        "closed_at": stamp(s.closed_at),
                    }
                    for s in pending
                ],
            },
            "",
        )
        return
    lines = [f"kb {name} {today} ({label})"]
    if failed:
        lines.append(f"Odoo: sync failed, data as of {stamp(meta.get('at'))}: {meta.get('error')}")
    lines += [f"[{r.task.id}] L{r.level} {r.name}  {r.task.title}  · {r.reason}" for r in shown]
    if quiet:
        lines.append(f"+{quiet} quiet (whisper), kb next")
    if pending:
        lines.append(no_summary_line(pending))
    click.echo("\n".join(lines))


@main.command(help="The standup: every open task at level normal or above; exit 0 always.")
@click.option("--scope", "scope_flag")
@click.option("--record-shown", is_flag=True, help="Write a shown event per printed task.")
@json_option
@pass_app
def today(app: App, scope_flag: str | None, record_shown: bool) -> None:
    standup(
        app,
        scope_flag,
        min_level=urgency.NORMAL,
        record_shown=record_shown,
        name="today",
        sessions_line=True,
    )


@main.command("next", help="Like today, down to whisper level; writes no shown events.")
@click.option("--scope", "scope_flag")
@json_option
@pass_app
def next_(app: App, scope_flag: str | None) -> None:
    standup(app, scope_flag, min_level=urgency.WHISPER, record_shown=False, name="next")


@main.command(help="Spell out how a task's level is computed today.")
@click.argument("ref")
@scope_option
@json_option
@pass_app
def why(app: App, ref: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    cfg = app.cfg
    today = calendar.today(cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, list[records.Event]]:
        rec = records.resolve(tx, ref, scopes)
        if rec.kind != "task":
            raise records.RecordError(f"[{rec.id}] is a {rec.kind}; why applies to tasks")
        return rec, records.events_of(tx, rec.id)

    with cli_errors():
        rec, events = app.read(work)
    lines = urgency.explain(rec, today, cfg, events)
    app.emit(
        {**urgency.as_json(rec, today, cfg, events), "id": rec.id, "explanation": lines},
        "\n".join([record_line(rec), *lines]),
    )


# --- archive / supersede / merge / link / unlink / promote ------------------------------


@main.command(help="Hide a record from today, next, search and index (found with --all).")
@click.argument("ref")
@click.option("--reason", required=True)
@scope_option
@json_option
@pass_app
def archive(app: App, ref: str, reason: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, bool]:
        rec = records.resolve(tx, ref, scopes)
        if rec.archived_at is not None:
            return rec, False
        current = app.current_session(tx, scopes, at)
        records.add_event(tx, rec.id, "archived", at, note=reason, session=app.stamp_of(current))
        touch_session(tx, current, at, rec.id)
        return records.touch(tx, rec.id, at, archived_at=at), True

    with cli_errors():
        rec, written = app.write(work)
    emit_record(app, rec, "  · archived" if written else "  already archived", written=written)


@main.command(help="NEW supersedes OLD: SUPERSEDES edge new->old, OLD archived.")
@click.argument("old")
@click.argument("new")
@scope_option
@json_option
@pass_app
def supersede(app: App, old: str, new: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, records.Record]:
        old_rec, new_rec = records.resolve(tx, old, scopes), records.resolve(tx, new, scopes)
        current = app.current_session(tx, scopes, at)
        sid = app.stamp_of(current)
        records.link(tx, new_rec, "SUPERSEDES", old_rec, at)
        records.add_event(
            tx, old_rec.id, "superseded", at, note=f"superseded by [{new_rec.id}]", session=sid
        )
        records.add_event(
            tx, new_rec.id, "superseded", at, note=f"supersedes [{old_rec.id}]", session=sid
        )
        touch_session(tx, current, at, old_rec.id, new_rec.id)
        return records.touch(tx, old_rec.id, at, archived_at=at), records.touch(tx, new_rec.id, at)

    with cli_errors():
        old_rec, new_rec = app.write(work)
    app.emit(
        {"old": old_rec.to_json(), "new": new_rec.to_json()},
        f"{record_line(new_rec)}  supersedes  {record_line(old_rec)}",
    )


@main.command(help="Move DUP's events and edges onto KEEP; DUP is archived.")
@click.argument("dup")
@click.argument("keep")
@scope_option
@json_option
@pass_app
def merge(app: App, dup: str, keep: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, records.Record, int]:
        dup_rec, keep_rec = records.resolve(tx, dup, scopes), records.resolve(tx, keep, scopes)
        if dup_rec.id == keep_rec.id:
            raise records.RecordError("DUP and KEEP are the same record")
        current = app.current_session(tx, scopes, at)
        sid = app.stamp_of(current)
        moved = records.move_edges_and_events(tx, dup_rec, keep_rec)
        records.add_event(tx, keep_rec.id, "merged", at, note=f"merged [{dup_rec.id}]", session=sid)
        records.add_event(
            tx, dup_rec.id, "archived", at, note=f"merged into [{keep_rec.id}]", session=sid
        )
        touch_session(tx, current, at, dup_rec.id, keep_rec.id)
        return (
            records.touch(tx, dup_rec.id, at, archived_at=at),
            records.touch(tx, keep_rec.id, at),
            moved,
        )

    with cli_errors():
        dup_rec, keep_rec, moved = app.write(work)
    app.emit(
        {"dup": dup_rec.to_json(), "keep": keep_rec.to_json(), "edges_moved": moved},
        f"{record_line(dup_rec)}  merged into  {record_line(keep_rec)}  ({moved} edges moved)",
    )


@main.command(
    help="Create A -[TYPE]-> B (RELATED, SUPERSEDES, BLOCKS, OPENED_IN, TOUCHED, PART_OF)."
)
@click.argument("a")
@click.argument("rel_type", metavar="TYPE")
@click.argument("b")
@scope_option
@json_option
@pass_app
def link(app: App, a: str, rel_type: str, b: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, records.Record, str, bool, list[int]]:
        rel = records.check_rel_type(rel_type)
        rec_a, rec_b = records.resolve(tx, a, scopes), records.resolve(tx, b, scopes)
        created, displaced = records.link(tx, rec_a, rel, rec_b, at)
        if created:
            current = app.current_session(tx, scopes, at)
            records.add_event(
                tx,
                rec_a.id,
                "linked",
                at,
                payload={"type": rel, "target": rec_b.id},
                session=app.stamp_of(current),
            )
            touch_session(tx, current, at, rec_a.id, rec_b.id)
        return rec_a, rec_b, rel, created, displaced

    with cli_errors():
        rec_a, rec_b, rel, created, displaced = app.write(work)
    state = "" if created else "  (already linked)"
    # OPENED_IN is single-valued, so naming a new session MOVED the record out of its old one -
    # say which, or the repair looks like it did nothing to the edge that was wrong.
    if displaced:
        state += "  (moved from " + ", ".join(f"[{sid}]" for sid in displaced) + ")"
    app.emit(
        {"a": rec_a.id, "type": rel, "b": rec_b.id, "created": created, "displaced": displaced},
        f"{record_line(rec_a)} -[{rel}]-> {record_line(rec_b)}{state}",
    )


@main.command(help="Remove A -[TYPE]-> B.")
@click.argument("a")
@click.argument("rel_type", metavar="TYPE")
@click.argument("b")
@scope_option
@json_option
@pass_app
def unlink(app: App, a: str, rel_type: str, b: str, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, records.Record, str, int]:
        rel = records.check_rel_type(rel_type)
        rec_a, rec_b = records.resolve(tx, a, scopes), records.resolve(tx, b, scopes)
        removed = records.unlink(tx, rec_a, rel, rec_b)
        if removed:
            current = app.current_session(tx, scopes, at)
            records.add_event(
                tx,
                rec_a.id,
                "linked",
                at,
                payload={"type": rel, "target": rec_b.id, "removed": True},
                session=app.stamp_of(current),
            )
            touch_session(tx, current, at, rec_a.id, rec_b.id)
        return rec_a, rec_b, rel, removed

    with cli_errors():
        rec_a, rec_b, rel, removed = app.write(work)
    state = "" if removed else "  (no such link)"
    app.emit(
        {"a": rec_a.id, "type": rel, "b": rec_b.id, "removed": removed},
        f"{record_line(rec_a)} -[{rel}]-> {record_line(rec_b)} removed{state}",
    )


@main.command(help="Mark a task as tracked in Odoo: source=odoo, external_id=KEY.")
@click.argument("ref")
@click.argument("key")
@scope_option
@json_option
@pass_app
def promote(app: App, ref: str, key: str, scope_flag: str | None) -> None:
    """Tasks only: the sync matches odoo records by the Task label, a promoted note would be
    duplicated as a Task on the next run."""
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)

    def work(tx: ManagedTransaction) -> records.Record:
        rec = records.resolve(tx, ref, scopes)
        if rec.kind != "task":
            raise records.RecordError(
                f"[{rec.id}] is a {rec.kind}; promote is for tasks, notes/howtos link with kb link"
            )
        current = app.current_session(tx, scopes, at)
        records.add_event(tx, rec.id, "promoted", at, note=key, session=app.stamp_of(current))
        touch_session(tx, current, at, rec.id)
        return records.touch(tx, rec.id, at, source="odoo", external_id=key)

    with cli_errors():
        rec = app.write(work)
    emit_record(app, rec, f"  · odoo {key}")


# --- session ----------------------------------------------------------------------------


HOOK_SOURCES = tuple(s for s in records.SESSION_SOURCES if s != "migrated")


@main.group(
    "session", help="Session records: the hooks open and close them, Claude writes the body."
)
def session_group() -> None:
    """Group for the session subcommands."""
    pass


def own_session_scopes(
    app: App, session_id: str | None, scopes: list[str] | None
) -> list[str] | None:
    """The view an id is looked up in: None (every scope) for this process's own session.

    A session whose id is the caller's ``KB_SESSION`` is addressable wherever the cwd sits —
    it is this process's own, and the hook that opened it knew the scope better than the
    directory does. Anything else obeys the view of ADR 0003. The rule has to be the same for
    open, close, note and current, or a session resumed from another cwd reopens but cannot be
    closed, and then stays open forever winning the fallback.
    """
    return None if session_id is not None and session_id == app.session else scopes


def resolve_session(
    tx: ManagedTransaction,
    app: App,
    session_id: str | None,
    scopes: list[str] | None,
    at: dt.datetime,
) -> records.Record:
    """``--id`` names the session; without it the current one; an error when there is none.

    An id is a REF like any other, except this process's own session: see
    :func:`own_session_scopes`.
    """
    if session_id:
        rec = records.session_by_id(tx, session_id, own_session_scopes(app, session_id, scopes))
        if rec is None:
            raise records.RecordError(f"no session {session_id}")
        return rec
    rec = app.current_session(tx, scopes, at)
    if rec is None:
        raise records.RecordError("no open session")
    return rec


def session_line(rec: records.Record) -> str:
    return f"[{rec.id}] {rec.slug}"


def placeholder_title(slug: str | None, source: str | None) -> str:
    """``session <date> <n> (<source>)``: the title a session carries until Claude writes one."""
    date, _, n = (slug or "").rpartition("-")
    return f"session {date} {n} ({source})"


@session_group.command(
    "open", help="Open the session record for Claude's session id (re-open a closed one)."
)
@scope_option
@click.option("--id", "session_id", required=True, help="Claude's session id.")
@click.option(
    "--source", type=click.Choice(HOOK_SOURCES), required=True, help="The SessionStart matcher."
)
@json_option
@pass_app
def session_open(app: App, *, scope_flag: str | None, session_id: str, source: str) -> None:
    """Idempotent on the id: an open record is left alone, a closed one loses its closed_at."""
    cfg = app.cfg
    scope = write_scope(cfg, scope_flag)
    scopes, _ = view_scopes(cfg, scope_flag)
    today = calendar.today(cfg)
    at = calendar.now(cfg)

    def work(tx: ManagedTransaction) -> tuple[records.Record, str]:
        rec = records.session_by_id(tx, session_id, own_session_scopes(app, session_id, scopes))
        if rec is not None and rec.closed_at is None:
            return rec, "open"
        if rec is not None:
            records.add_event(tx, rec.id, "opened", at, note=source, session=session_id)
            changes: dict[str, Any] = {"closed_at": None, "reason": None, "source": source}
            # the placeholder names the source, so a reopen from another matcher must rewrite
            # it; a title Claude has written is the session's own and survives
            if rec.title == placeholder_title(rec.slug, rec.source):
                changes["title"] = placeholder_title(rec.slug, source)
            rec = records.touch(tx, rec.id, at, **changes)
            return rec, "reopened"
        slug = records.next_session_slug(tx, today)
        rec = records.create(
            tx,
            kind="session",
            scope=scope,
            title=placeholder_title(slug, source),
            at=at,
            slug=slug,
            source=source,
            session=session_id,
            extra={"session_id": session_id, "opened_at": at, "no_summary": False},
        )
        records.add_event(tx, rec.id, "opened", at, note=source, session=session_id)
        return rec, "created"

    with cli_errors():
        rec, state = app.write(work)
    app.emit({**rec.to_json(), "state": state}, session_line(rec))


@session_group.command(
    "close", help="Close the session (idempotent); --body/--title write the summary, now or later."
)
@click.option("--id", "session_id", help="Claude's session id; default: the current session.")
@click.option("--reason", help="The SessionEnd reason.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--body", help="Body text; '-' reads stdin.")
@click.option("--title", help="The intent, 120 characters.")
@scope_option
@json_option
@pass_app
def session_close(
    app: App,
    *,
    session_id: str | None,
    reason: str | None,
    body_file: Path | None,
    body: str | None,
    title: str | None,
    scope_flag: str | None,
) -> None:
    """First close: closed_at, reason, a closed event; later calls only update body/title/reason."""
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)
    text = read_body(body, body_file)

    def work(tx: ManagedTransaction) -> tuple[records.Record, str]:
        rec = resolve_session(tx, app, session_id, scopes, at)
        # the session doing the closing, which is null when a person closes one by hand
        sid = app.stamp_of(app.current_session(tx, scopes, at))
        changes: dict[str, Any] = {}
        if title is not None and records.check_title(title) != rec.title:
            changes["title"] = records.check_title(title)
        if text is not None and text != rec.body:
            changes["body"] = text
        no_summary = not changes.get("body", rec.body).strip()
        if rec.closed_at is None:
            records.add_event(tx, rec.id, "closed", at, note=reason, session=sid)
            rec = records.touch(
                tx, rec.id, at, closed_at=at, reason=reason, no_summary=no_summary, **changes
            )
            return rec, "closed"
        if reason is not None and reason != rec.reason:
            changes["reason"] = reason
        if not changes:
            return rec, "already closed"
        before = {name: getattr(rec, name) for name in changes}
        records.add_event(
            tx,
            rec.id,
            "edited",
            at,
            payload={"fields": sorted(before), "before": before},
            session=sid,
        )
        return records.touch(tx, rec.id, at, no_summary=no_summary, **changes), "updated"

    with cli_errors():
        rec, state = app.write(work)
    extra = {"closed": "  · closed", "updated": "  · updated", "already closed": "  already closed"}
    text_state = extra[state] + (", no summary" if state == "closed" and rec.no_summary else "")
    app.emit({**rec.to_json(), "state": state}, session_line(rec) + text_state)


@session_group.command("note", help="Append a dated paragraph to the session body.")
@click.argument("text", required=False)
@click.option("--id", "session_id", help="Claude's session id; default: the current session.")
@click.option("--body-file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@scope_option
@json_option
@pass_app
def session_note(
    app: App,
    *,
    text: str | None,
    session_id: str | None,
    body_file: Path | None,
    scope_flag: str | None,
) -> None:
    """body += ``**<today>:** TEXT``; an edited event; the session is no longer without summary."""
    if (text is None) == (body_file is None):
        raise click.UsageError("pass TEXT or --body-file, one of them")
    cfg = app.cfg
    scopes, _ = view_scopes(cfg, scope_flag)
    today = calendar.today(cfg)
    at = calendar.now(cfg)
    note = text if text is not None else body_file.read_text().rstrip("\n")  # type: ignore[union-attr]
    # an empty note would date a paragraph that says nothing and clear no_summary with it
    if not note.strip():
        raise click.UsageError("the note is empty")

    def work(tx: ManagedTransaction) -> records.Record:
        rec = resolve_session(tx, app, session_id, scopes, at)
        records.add_event(
            tx,
            rec.id,
            "edited",
            at,
            payload={"fields": ["body"], "append": True, "before": {"body": rec.body}},
            session=app.stamp_of(app.current_session(tx, scopes, at)),
        )
        return records.touch(
            tx, rec.id, at, body=append_paragraph(rec.body, today, note), no_summary=False
        )

    with cli_errors():
        rec = app.write(work)
    app.emit(rec.to_json(), session_line(rec))


@session_group.command(
    "current", help="[id] slug (open since ...) of the current session; exit 1 when none."
)
@scope_option
@json_option
@pass_app
def session_current(app: App, scope_flag: str | None) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)
    at = calendar.now(app.cfg)
    with cli_errors():
        rec = app.read(lambda tx: app.current_session(tx, scopes, at))
    if rec is None:
        raise click.ClickException("no open session")
    state = (
        f"closed {stamp(rec.closed_at)}" if rec.closed_at else f"open since {stamp(rec.opened_at)}"
    )
    app.emit(rec.to_json(), f"{session_line(rec)} ({state})")


@session_group.command(
    "list", help="Sessions in the view, newest first: slug, title, opened/closed, link counts."
)
@scope_option
@click.option("--limit", default=20, show_default=True, type=click.IntRange(min=1))
@click.option("--open", "open_only", is_flag=True, help="Open sessions only.")
@json_option
@pass_app
def session_list(app: App, *, scope_flag: str | None, limit: int, open_only: bool) -> None:
    scopes, _ = view_scopes(app.cfg, scope_flag)

    def work(
        tx: ManagedTransaction,
    ) -> tuple[list[records.Record], dict[int, tuple[int, int]]]:
        rows = records.sessions(tx, scopes, limit=limit, open_only=open_only)
        return rows, records.session_link_counts(tx, [r.id for r in rows])

    with cli_errors():
        rows, counts = app.read(work)
    lines = []
    for rec in rows:
        opened, touched = counts[rec.id]
        state = f"closed {stamp(rec.closed_at)}" if rec.closed_at else "open"
        lines.append(
            f"[{rec.id}] {rec.slug}  {rec.title}  · opened {stamp(rec.opened_at)}  {state}  "
            f"· {opened} created / {touched} touched"
        )
    app.emit(
        {
            "sessions": [
                {**r.to_json(), "opened_in": counts[r.id][0], "touched": counts[r.id][1]}
                for r in rows
            ]
        },
        "\n".join(lines),
    )


# --- sync -------------------------------------------------------------------------------


@main.group(help="Pull records from external sources.")
def sync() -> None:
    """Group for the sync subcommands."""
    pass


@sync.command("odoo", help="Upsert my open Odoo tasks as Task records; exit 0 on any failure.")
@json_option
@pass_app
def sync_odoo_cmd(app: App) -> None:
    """[sources.odoo] disabled: silent. Failure: Meta ok=false, one stderr line, exit 0."""
    at = calendar.now(app.cfg)
    rows: list[dict[str, Any]] | None = None
    try:
        cfg = app.cfg
        odoo = cfg.sources.odoo
        if not odoo.enabled:
            return
        password = sync_odoo.read_password(odoo.password_file)
        rows = sync_odoo.fetch_tasks(odoo, password)
        fetched = rows

        def work(tx: ManagedTransaction) -> tuple[sync_odoo.SyncResult, int | None]:
            previous = records.get_meta(tx, sync_odoo.META_NAME) or {}
            result = sync_odoo.upsert(tx, fetched, cfg, at, app.session)
            sync_odoo.write_meta(tx, at, True, None, len(fetched), untracked=len(result.untracked))
            return result, previous.get("untracked")

        result, previous_untracked = app.write(work)
    except (
        sync_odoo.SyncError,
        click.ClickException,
        Neo4jError,
        DriverError,
        OSError,
        ValueError,
    ) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        _record_sync_failure(app, at, message, len(rows) if rows is not None else None)
        click.echo(f"kb sync odoo: {message}", err=True)
        return
    app.emit(result.to_json(), "\n".join(result.summary(previous_untracked=previous_untracked)))


def _record_sync_failure(app: App, at: dt.datetime, error: str, count: int | None) -> None:
    """Meta ok=false with the error; ``count`` only when the rows were fetched. Best effort."""
    with contextlib.suppress(click.ClickException, Neo4jError, DriverError, OSError, ValueError):
        app.write(lambda tx: sync_odoo.write_meta(tx, at, False, error, count))


# --- index ------------------------------------------------------------------------------


INDEX_KINDS = "feedback,note,howto,reference,decision"
INDEX_SUMMARY_MAX = 160


def parse_kinds(text: str) -> list[str]:
    """``--kinds a,b,c`` -> distinct valid kinds, in order."""
    kinds = list(dict.fromkeys(k.strip() for k in text.split(",") if k.strip()))
    if not kinds:
        raise click.BadParameter("at least one kind", param_hint="--kinds")
    for kind in kinds:
        if kind not in records.KINDS:
            raise click.BadParameter(
                f"unknown kind {kind!r}; one of {', '.join(records.KINDS)}", param_hint="--kinds"
            )
    return kinds


def index_line(rec: records.Record) -> str:
    """``- [id] slug — summary`` (cut at 160 characters with ``…``), the title without one."""
    text = rec.summary or rec.title
    if len(text) > INDEX_SUMMARY_MAX:
        text = text[:INDEX_SUMMARY_MAX].rstrip() + "…"
    return f"- [{rec.id}] {rec.slug or '-'} — {text}"


@main.command(help="The memory index a session hook prints: pinned records, then recent ones.")
@click.option("--scope", "scope_flag")
@click.option(
    "--budget", type=click.IntRange(min=1), help="Max lines; default [index].budget_lines."
)
@click.option("--kinds", default=INDEX_KINDS, show_default=True, help="Comma-separated kinds.")
@pass_app
def index(app: App, scope_flag: str | None, budget: int | None, kinds: str) -> None:
    """Header, ``## Pinned``, ``## Recent``, at most *budget* lines in total; exit 0 always."""
    kind_list = parse_kinds(kinds)
    try:
        cfg = app.cfg
        budget = budget or cfg.index.budget_lines
        scopes, label = view_scopes(cfg, scope_flag)
        today = calendar.today(cfg)
    except click.ClickException as exc:
        click.echo(f"kb index: {exc.message}", err=True)
        return
    header = (
        f"# kb index {today} ({label}) — generated, do not edit; write with kb add/append, "
        "read with kb show <id|slug>, search with kb search"
    )

    def work(tx: ManagedTransaction) -> tuple[int, list[records.Record], list[records.Record]]:
        return (
            records.count_records(tx),
            records.pinned(tx, scopes, kind_list),
            records.recent(tx, scopes, kind_list, budget),
        )

    try:
        total, pins, recents = app.read(work)
    except (click.ClickException, Neo4jError, DriverError, OSError, ValueError) as exc:
        message = exc.message if isinstance(exc, click.ClickException) else str(exc)
        click.echo(header)
        click.echo(f"kb index: {message}", err=True)
        return
    lines = [header]
    if total and pins:
        lines += ["", "## Pinned", *(index_line(r) for r in pins)]
    if total and recents and len(lines) + 3 <= budget:
        lines += ["", "## Recent", *(index_line(r) for r in recents)]
    click.echo("\n".join(lines[:budget]))


# --- migrate ----------------------------------------------------------------------------


@main.group(help="One-off imports from the old memory files.")
def migrate_group() -> None:
    """Group for the migrate subcommands."""
    pass


main.add_command(migrate_group, "migrate")


@migrate_group.command("state", help="state.md items -> Task records with their ids kept.")
@click.argument("file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option("--scope", "scope_flag", required=True)
@click.option("--dry-run", is_flag=True, help="Print the plan, write nothing.")
@click.option(
    "--counter",
    type=click.IntRange(min=0),
    help="Set the Counter to N afterwards (N >= the highest item id); default: raise only.",
)
@json_option
@pass_app
def migrate_state(
    app: App, file: Path, scope_flag: str | None, dry_run: bool, counter: int | None
) -> None:
    cfg = app.cfg
    scope = check_scope(cfg, scope_flag or "")
    items = migrate.parse(file.read_text())
    if not items:
        click.echo(f"{file}: no state items found")
        return
    with cli_errors():
        if dry_run:

            def plan_existing(tx: ManagedTransaction) -> set[int]:
                migrate.check_counter(tx, items, counter)
                return {i.id for i in items if records.exists(tx, i.id)}

            existing = app.read(plan_existing)
            plan = [
                migrate.plan_line(i) + ("  (exists, skip)" if i.id in existing else "")
                for i in items
            ]
            closed = sum(1 for i in items if i.closed_on and i.id not in existing)
            fresh = sum(1 for i in items if i.id not in existing)
            summary = f"would create {fresh} tasks ({closed} closed), skip {len(existing)}"
            if counter is not None:
                summary += f"; counter set to {counter}"
            app.emit(
                {"plan": plan, "would_create": fresh, "closed": closed, "skip": sorted(existing)},
                "\n".join([*plan, summary]),
            )
            return
        report = app.write(
            lambda tx: migrate.apply(tx, items, scope, cfg, session=app.session, counter=counter)
        )
    app.emit(report.to_json(), report.summary())


@migrate_group.command("nodes", help="Markdown memory nodes -> records; [[wikilinks]] -> RELATED.")
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--scope", "scope_flag", required=True, help="Default scope of every node.")
@click.option(
    "--scope-file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Per-node scopes: lines 'slug scope', # comments.",
)
@click.option("--dry-run", is_flag=True, help="Print the plan and the report, write nothing.")
@click.option(
    "--report",
    "report_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the markdown report here instead of stdout.",
)
@json_option
@pass_app
def migrate_nodes_cmd(
    app: App,
    *,
    directory: Path,
    scope_flag: str,
    scope_file: Path | None,
    dry_run: bool,
    report_file: Path | None,
) -> None:
    """Every scope is checked before anything is written; one write transaction for the run."""
    cfg = app.cfg
    default_scope = check_scope(cfg, scope_flag)
    at = calendar.now(cfg)
    with cli_errors():
        if report_file is not None and not report_file.parent.is_dir():
            raise FileNotFoundError(errno.ENOENT, "no such directory", str(report_file.parent))
        nodes, no_frontmatter = migrate_nodes.load_dir(directory, cfg, at)
        overrides = migrate_nodes.read_scope_file(scope_file) if scope_file else {}
    for slug, scope in overrides.items():
        if scope not in scope_names(cfg):
            raise click.BadParameter(
                f"{slug}: unknown scope {scope!r}; one of {', '.join(scope_names(cfg))}",
                param_hint="--scope-file",
            )
    unknown = sorted(set(overrides) - {n.slug for n in nodes})
    if unknown:
        click.echo(
            f"scope file: {len(unknown)} slugs not in {directory}: {', '.join(unknown)}", err=True
        )
    if not nodes and not no_frontmatter:
        click.echo(f"{directory}: no node files found")
        return
    scopes = {n.slug: overrides.get(n.slug, default_scope) for n in nodes}
    with cli_errors():
        if dry_run:
            plan = app.read(lambda tx: migrate_nodes.resolve(tx, nodes, scopes, no_frontmatter))
            report = plan.report()
            text = [*(plan.line(n) for n in plan.nodes), "", report.markdown(), ""]
            app.emit(
                {"plan": [plan.line(n) for n in plan.nodes], **report.to_json()},
                "\n".join([*text, report.summary(dry_run=True)]),
            )
            return

        def work(tx: ManagedTransaction) -> migrate_nodes.Report:
            plan = migrate_nodes.resolve(tx, nodes, scopes, no_frontmatter)
            return migrate_nodes.apply(tx, plan, session=app.session, at=at)

        report = app.write(work)
    emit_report(app, report, report_file)


def emit_report(
    app: App, report: migrate_nodes.Report | migrate_journal.Report, report_file: Path | None
) -> None:
    """The markdown report into ``--report FILE`` (the summary on stdout), else both on stdout."""
    if report_file is not None:
        try:
            report_file.write_text(report.markdown() + "\n")
        except OSError as exc:
            # the records are in; say so before failing on the file
            app.emit(report.to_json(), report.summary())
            raise click.ClickException(f"report not written: {exc}") from exc
        app.emit(report.to_json(), report.summary())
    else:
        app.emit(report.to_json(), f"{report.markdown()}\n\n{report.summary()}")


@migrate_group.command(
    "journal", help="Journal day files -> Session records; [ID] and [[slug]] mentions -> edges."
)
@click.argument("directory", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--scope", "scope_flag", required=True, help="Scope of every session.")
@click.option("--dry-run", is_flag=True, help="Print the plan and the report, write nothing.")
@click.option(
    "--report",
    "report_file",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Write the markdown report here instead of stdout.",
)
@json_option
@pass_app
def migrate_journal_cmd(
    app: App, *, directory: Path, scope_flag: str, dry_run: bool, report_file: Path | None
) -> None:
    """One write transaction for the run; the link pass covers sessions migrated earlier."""
    cfg = app.cfg
    scope = check_scope(cfg, scope_flag)
    with cli_errors():
        if report_file is not None and not report_file.parent.is_dir():
            raise FileNotFoundError(errno.ENOENT, "no such directory", str(report_file.parent))
        blocks, skipped_files = migrate_journal.load_dir(directory, cfg)
    if not blocks:
        click.echo(f"{directory}: no session blocks found")
        return
    with cli_errors():
        if dry_run:
            plan = app.read(lambda tx: migrate_journal.resolve(tx, blocks, skipped_files, cfg))
            report = plan.report()
            text = [*(plan.line(b) for b in blocks), "", report.markdown(), ""]
            app.emit(
                {"plan": [plan.line(b) for b in blocks], **report.to_json()},
                "\n".join([*text, report.summary(dry_run=True)]),
            )
            return

        def work(tx: ManagedTransaction) -> migrate_journal.Report:
            plan = migrate_journal.resolve(tx, blocks, skipped_files, cfg)
            return migrate_journal.apply(tx, plan, scope=scope, session=app.session)

        report = app.write(work)
    emit_report(app, report, report_file)
