"""kb sync odoo: pull my open project.task rows over JSON-RPC (stdlib urllib) into Task records."""

import datetime as dt
import json
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neo4j import ManagedTransaction

from kb import records
from kb.calendar import local_date
from kb.config import Config, OdooSource

META_NAME = "odoo_sync"
UNTRACKED_SHOWN = 8
TASK_FIELDS = [
    "id",
    "key",
    "name",
    "stage_id",
    "date_assign",
    "date_deadline",
    "create_date",
]


class SyncError(Exception):
    """Anything that stops a sync: config, auth file, network, RPC fault, bad payload."""


@dataclass
class SyncResult:
    """Counts of one successful sync."""

    count: int = 0
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    untracked: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "created": self.created,
            "updated": self.updated,
            "closed": self.closed,
            "untracked": self.untracked,
            "skipped": self.skipped,
        }

    def summary(self, *, previous_untracked: int | None) -> list[str]:
        """One line of counts; the untracked list (capped) only when its size changed."""
        lines = [
            f"odoo: {self.count} open tasks, {len(self.created)} new, "
            f"{len(self.updated)} updated, {len(self.closed)} closed"
        ]
        if self.skipped:
            lines[0] += f", skipped rows: {'; '.join(self.skipped)}"
        if self.untracked and len(self.untracked) != previous_untracked:
            shown = ", ".join(str(i) for i in self.untracked[:UNTRACKED_SHOWN])
            more = len(self.untracked) - UNTRACKED_SHOWN
            if more > 0:
                shown += f", … and {more} more (kb show <id>)"
            lines.append(
                f"{len(self.untracked)} untracked odoo records (not in the result set): {shown}"
            )
        return lines


def read_password(path: Path) -> str:
    """The one-line password file, mode 0600 or 0400; missing, open or empty is a SyncError."""
    if not path.is_file():
        raise SyncError(f"password file {path} not found")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode not in (0o600, 0o400):
        raise SyncError(f"password file {path} must be mode 0600 or 0400, is {mode:04o}")
    password = path.read_text().strip().splitlines()
    if not password or not password[0]:
        raise SyncError(f"password file {path} is empty")
    return password[0]


def rpc(url: str, service: str, method: str, args: list[Any], timeout: int) -> Any:
    """One ``call`` on ``<url>/jsonrpc``; an RPC fault or transport error is a SyncError."""
    body = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "call",
            "params": {"service": service, "method": method, "args": args},
            "id": 1,
        }
    ).encode()
    if not url.startswith(("http://", "https://")):
        raise SyncError(f"sources.odoo.url must start with http:// or https://, got {url!r}")
    request = urllib.request.Request(  # noqa: S310
        url.rstrip("/") + "/jsonrpc", data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode())
    except urllib.error.URLError as exc:
        raise SyncError(f"{url}: {exc.reason}") from exc
    except (TimeoutError, OSError, ValueError) as exc:
        raise SyncError(f"{url}: {exc}") from exc
    if "error" in payload:
        error = payload["error"]
        data = error.get("data") or {}
        raise SyncError(data.get("message") or error.get("message") or "rpc error")
    return payload.get("result")


def fetch_tasks(cfg: OdooSource, password: str) -> list[dict[str, Any]]:
    """Login, resolve the assignee (``assignee_login`` or the RPC user), then ``project.task``
    search_read of that user's open tasks (project filter if configured)."""
    for key in ("url", "database", "username"):
        if not getattr(cfg, key):
            raise SyncError(f"sources.odoo.{key} is not set")
    uid = rpc(cfg.url, "common", "login", [cfg.database, cfg.username, password], cfg.timeout)
    if not uid:
        raise SyncError(f"login as {cfg.username} on {cfg.database} failed")
    assignee = uid
    if cfg.assignee_login:
        # the RPC user may be a service account: the tasks wanted are somebody else's
        found = rpc(
            cfg.url,
            "object",
            "execute_kw",
            [
                cfg.database,
                uid,
                password,
                "res.users",
                "search_read",
                [[["login", "=", cfg.assignee_login]]],
                {"fields": ["id"], "limit": 1},
            ],
            cfg.timeout,
        )
        if not isinstance(found, list) or not found:
            raise SyncError(f"assignee {cfg.assignee_login!r} not found in res.users")
        assignee = found[0]["id"]
    domain: list[Any] = [["user_ids", "in", [assignee]], ["is_closed", "=", False]]
    if cfg.project_id:
        domain.append(["project_id", "=", cfg.project_id])
    result = rpc(
        cfg.url,
        "object",
        "execute_kw",
        [
            cfg.database,
            uid,
            password,
            "project.task",
            "search_read",
            [domain],
            {"fields": TASK_FIELDS},
        ],
        cfg.timeout,
    )
    if not isinstance(result, list):
        raise SyncError("search_read did not return a list")
    return result


def _date(value: Any, cfg: Config) -> dt.date | None:
    """Odoo's ``YYYY-MM-DD`` stays a date; ``YYYY-MM-DD HH:MM:SS`` is UTC -> kb-timezone date."""
    if not value:
        return None
    text = str(value)
    if len(text) > 10:  # noqa: PLR2004
        at = dt.datetime.fromisoformat(text).replace(tzinfo=dt.UTC)
        return local_date(at, cfg)
    return dt.date.fromisoformat(text)


def desired_props(row: dict[str, Any], cfg: Config) -> dict[str, Any]:
    """The Task properties one Odoo row maps to; the title is cut to the record limit."""
    odoo = cfg.sources.odoo
    key = row.get("key") or str(row["id"])
    stage = row.get("stage_id")
    stage_name = stage[1] if isinstance(stage, list) and len(stage) > 1 else None
    deadline = _date(row.get("date_deadline"), cfg)
    anchor = _date(row.get("date_assign"), cfg) or _date(row.get("create_date"), cfg)
    if anchor is None:
        raise records.RecordError("no date_assign and no create_date")
    title = " ".join(f"{key} {row.get('name') or ''}".split())
    return {
        "external_id": str(key),
        "odoo_id": str(row["id"]),
        "title": records.check_title(title[: records.TITLE_MAX]),
        "sched": "soft" if deadline else "age",
        "due": deadline,
        "anchor": anchor,
        "days": "work",
        "stage": stage_name,
        "body": f"{odoo.url.rstrip('/')}/web#id={row['id']}&model=project.task&view_type=form",
    }


SYNCED_FIELDS = ("sched", "due", "anchor", "stage")
LOCAL_AUTHORSHIP = ("edited", "promoted")
ORIGIN = "odoo-sync"


def upsert(
    tx: ManagedTransaction,
    rows: list[dict[str, Any]],
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> SyncResult:
    """Create/update Task records per row; odoo tasks the sync has seen before and that vanished
    from the result are closed.

    A row matches every existing odoo Task whose ``external_id`` is the key or the Odoo id
    (several state.md items can point at one ticket); each gets the schedule update and its
    ``external_id`` normalised to the key. Only schedule and stage are refreshed; the body is
    written once, the title only on records the sync itself created (``origin = odoo-sync``)
    and nobody edited. Records with ``source = odoo`` the sync has never seen (promoted,
    migrated) are left alone and reported.
    """
    odoo = cfg.sources.odoo
    if not odoo.scope:
        raise SyncError("sources.odoo.scope is not set")
    result = SyncResult(count=len(rows))
    existing = _odoo_tasks(tx)
    by_external: dict[str, list[records.Record]] = {}
    for rec in existing:
        if rec.external_id is not None:
            by_external.setdefault(rec.external_id, []).append(rec)
    events = records.events_for(tx, [r.id for r in existing])
    matched: set[int] = set()
    for row in rows:
        try:
            props = desired_props(row, cfg)
        except (records.RecordError, KeyError, ValueError) as exc:
            result.skipped.append(f"{row.get('id', '?')} ({exc})")
            continue
        group = [
            rec
            for key in dict.fromkeys((props["external_id"], props["odoo_id"]))
            for rec in by_external.get(key, [])
            if rec.id not in matched
        ]
        if not group:
            rec = records.create(
                tx,
                kind="task",
                scope=odoo.scope,
                title=props["title"],
                at=at,
                body=props["body"],
                source="odoo",
                external_id=props["external_id"],
                schedule={k: props[k] for k in ("sched", "due", "anchor", "days")},
                session=session,
                stage=props["stage"],
                synced_at=at,
                origin=ORIGIN,
            )
            result.created.append(rec.id)
            continue
        for current in group:
            matched.add(current.id)
            _refresh(
                tx,
                current,
                props=props,
                history=events[current.id],
                at=at,
                session=session,
                result=result,
            )
    for rec in existing:
        if rec.id in matched or rec.done_at is not None or rec.archived_at is not None:
            continue
        if rec.synced_at is None:
            result.untracked.append(rec.id)
            continue
        records.add_event(tx, rec.id, "done", at, note="closed in Odoo", session=session)
        records.touch(tx, rec.id, at, done_at=at)
        result.closed.append(rec.id)
    return result


def _refresh(
    tx: ManagedTransaction,
    current: records.Record,
    *,
    props: dict[str, Any],
    history: list[records.Event],
    at: dt.datetime,
    session: str | None,
    result: SyncResult,
) -> None:
    """Apply one row to one matched record: schedule, stage, key; the title only on a record
    the sync created and nobody edited."""
    changed = {k: props[k] for k in SYNCED_FIELDS if getattr(current, k) != props[k]}
    if current.external_id != props["external_id"]:
        changed["external_id"] = props["external_id"]
    authored = current.origin != ORIGIN or any(e.kind in LOCAL_AUTHORSHIP for e in history)
    if not authored and current.title != props["title"]:
        changed["title"] = props["title"]
    if current.done_at is not None:
        # reopened in Odoo after kb saw it closed
        changed["done_at"] = None
    if not changed:
        records.mark_synced(tx, current.id, at)
        return
    records.add_event(
        tx, current.id, "synced", at, payload={"changed": sorted(changed)}, session=session
    )
    result.updated.append(current.id)
    records.touch(tx, current.id, at, synced_at=at, **changed)


def _odoo_tasks(tx: ManagedTransaction) -> list[records.Record]:
    rows = tx.run("MATCH (t:Record:Task {source: 'odoo'}) RETURN t ORDER BY t.id")
    return [records.Record.from_node(row["t"]) for row in rows]


def write_meta(
    tx: ManagedTransaction,
    at: dt.datetime,
    ok: bool,
    error: str | None,
    count: int | None,
    *,
    untracked: int | None = None,
) -> None:
    """``(:Meta {name: 'odoo_sync'})``: the outcome of the last run; counts kept when None."""
    props: dict[str, Any] = {"at": at, "ok": ok, "error": error}
    if count is not None:
        props["count"] = count
    if untracked is not None:
        props["untracked"] = untracked
    records.set_meta(tx, META_NAME, **props)
