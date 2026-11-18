"""kb sync odoo: pull my open project.task rows over JSON-RPC (stdlib urllib) into Task records,
with what the task's agent layer offers where the Odoo has it: modules and type as tags, the
parent and the blockers as links, the brief, the estimate and the open question rows."""

import datetime as dt
import json
import stat
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from neo4j import ManagedTransaction

from kb import records, similar, tags
from kb.calendar import local_date
from kb.config import Config, OdooSource

META_NAME = "odoo_sync"
UNTRACKED_SHOWN = 8
# what every Odoo has
TASK_FIELDS = ["id", "name", "stage_id", "date_assign", "date_deadline", "create_date"]
# read where the Odoo has them (fields_get first): a search_read naming a field the model lacks
# fails as a whole. key: OCA project_key; the rest: the agent layer of an AI-driven project
OPTIONAL_FIELDS = [
    "key",
    "module_names",
    "task_type",
    "parent_id",
    "depend_on_ids",
    "planned_hours",
    "brief",
    "question_ids",
]
# asked for presence only: the open rows are counted in a read of their own
PRESENCE_ONLY = frozenset({"question_ids"})
QUESTION_MODEL = "project.task.question"
# task types with a kb type tag of the same meaning; the others have none
TYPE_TAGS = {"bug": "bug", "feature": "feature"}


class SyncError(Exception):
    """Anything that stops a sync: config, auth file, network, RPC fault, bad payload."""


@dataclass(frozen=True)
class Duplicate:
    """A task the sync just created for a new ticket, and the open thread it resembles."""

    new: records.Record
    old: records.Record
    key: str
    similarity: float

    def line(self) -> str:
        return (
            f"possible duplicate: [{self.new.id}] {self.new.title} ~ [{self.old.id}] "
            f"{self.old.title}: kb promote {self.old.id} {self.key}, "
            f"then kb merge {self.new.id} {self.old.id}"
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "new": self.new.id,
            "old": self.old.id,
            "key": self.key,
            "similarity": round(self.similarity, 3),
        }


@dataclass
class Change:
    """What one sync does to one record: ``new``, the fields and links it changes, or closed."""

    id: int
    key: str
    title: str
    kind: str  # new | updated | closed
    what: list[str] = field(default_factory=list)

    def line(self) -> str:
        if self.kind == "new":
            return f"new {self.title}" + (f": {'; '.join(self.what)}" if self.what else "")
        return f"[{self.id}] {self.key}: {'; '.join(self.what)}"

    def to_json(self) -> dict[str, Any]:
        return {"id": self.id, "key": self.key, "kind": self.kind, "changes": self.what}


@dataclass
class SyncResult:
    """Counts of one successful sync, and what it changed record by record."""

    count: int = 0
    created: list[int] = field(default_factory=list)
    updated: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    untracked: list[int] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    duplicates: list[Duplicate] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "created": self.created,
            "updated": self.updated,
            "closed": self.closed,
            "untracked": self.untracked,
            "skipped": self.skipped,
            "possible_duplicates": [d.to_json() for d in self.duplicates],
            "changes": [c.to_json() for c in self.changes],
        }

    def change_lines(self) -> list[str]:
        """One line per record the sync creates, changes or closes: new, then updated, then
        closed, each in the order of the rows."""
        order = {"new": 0, "updated": 1, "closed": 2}
        return [c.line() for c in sorted(self.changes, key=lambda c: order[c.kind])]

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
        # once: a hint belongs to the run that created the record, the next run is silent
        lines += [d.line() for d in self.duplicates]
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


def present_fields(cfg: OdooSource, uid: int, password: str) -> list[str]:
    """The OPTIONAL_FIELDS this Odoo's ``project.task`` has (``fields_get``): a keyless task is
    filed under its id, a task without the agent layer syncs as before."""
    found = rpc(
        cfg.url,
        "object",
        "execute_kw",
        [
            cfg.database,
            uid,
            password,
            "project.task",
            "fields_get",
            [OPTIONAL_FIELDS],
            {"attributes": ["type"]},
        ],
        cfg.timeout,
    )
    if not isinstance(found, dict):
        return []
    return [name for name in OPTIONAL_FIELDS if name in found]


def open_questions(cfg: OdooSource, uid: int, password: str, task_ids: list[int]) -> dict[int, int]:
    """``{task id: open question rows}`` for the tasks that have any."""
    found = rpc(
        cfg.url,
        "object",
        "execute_kw",
        [
            cfg.database,
            uid,
            password,
            QUESTION_MODEL,
            "search_read",
            [[["task_id", "in", task_ids], ["state", "=", "open"]]],
            {"fields": ["task_id"]},
        ],
        cfg.timeout,
    )
    if not isinstance(found, list):
        raise SyncError(f"{QUESTION_MODEL} search_read did not return a list")
    counts: dict[int, int] = {}
    for row in found:
        task = row.get("task_id")
        if isinstance(task, list) and task:
            counts[task[0]] = counts.get(task[0], 0) + 1
    return counts


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
    present = present_fields(cfg, uid, password)
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
            {"fields": TASK_FIELDS + [f for f in present if f not in PRESENCE_ONLY]},
        ],
        cfg.timeout,
    )
    if not isinstance(result, list):
        raise SyncError("search_read did not return a list")
    if "question_ids" in present and result:
        counts = open_questions(cfg, uid, password, [row["id"] for row in result])
        for row in result:
            row["open_questions"] = counts.get(row["id"], 0)
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


def desired_props(
    row: dict[str, Any], cfg: Config, vocab: tags.Vocabulary | None = None
) -> dict[str, Any]:
    """The Task properties one Odoo row maps to; the title is cut to the record limit.

    A field the row lacks (the Odoo has none) leaves its properties out, so the sync keeps
    what an earlier run stored: ``tags`` (with ``tag_kinds``, the kinds of tag the row speaks
    for), ``brief``, ``estimate``, ``open_questions``, ``parent``, ``blockers``.
    """
    odoo = cfg.sources.odoo
    key = row.get("key") or str(row["id"])
    stage = row.get("stage_id")
    stage_name = stage[1] if isinstance(stage, list) and len(stage) > 1 else None
    deadline = _date(row.get("date_deadline"), cfg)
    anchor = _date(row.get("date_assign"), cfg) or _date(row.get("create_date"), cfg)
    if anchor is None:
        raise records.RecordError("no date_assign and no create_date")
    title = " ".join(f"{key} {row.get('name') or ''}".split())
    props: dict[str, Any] = {
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
    if "module_names" in row or "task_type" in row:
        props["tags"], props["tag_kinds"] = row_tags(row, vocab)
    if "brief" in row:
        props["brief"] = row["brief"] or None
    if "planned_hours" in row:
        props["estimate"] = float(row["planned_hours"]) if row["planned_hours"] else None
    if "open_questions" in row:
        props["open_questions"] = int(row["open_questions"])
    if "parent_id" in row:
        parent = row["parent_id"]
        props["parent"] = parent[0] if isinstance(parent, list) and parent else None
    if "depend_on_ids" in row:
        props["blockers"] = [i for i in row["depend_on_ids"] or [] if isinstance(i, int)]
    return props


def row_tags(row: dict[str, Any], vocab: tags.Vocabulary | None) -> tuple[list[str], set[str]]:
    """The kb tags a row maps to, and the kinds of tag (``area``, ``type``) it speaks for: a
    module whose name is an area of the vocabulary, never a new one; the task type where kb
    has a type of the same meaning."""
    found: list[str] = []
    kinds: set[str] = set()
    if "module_names" in row:
        kinds.add("area")
        areas = vocab.areas if vocab is not None else {}
        names = (n.strip().lower() for n in (row["module_names"] or "").split(","))
        found += [n for n in names if n in areas]
    if "task_type" in row:
        kinds.add("type")
        mapped = TYPE_TAGS.get(row["task_type"] or "")
        if mapped:
            found.append(mapped)
    return list(dict.fromkeys(found)), kinds


def tag_kind(tag: str) -> str:
    return "type" if tag in tags.TYPE_TAGS else "area"


def merge_tags(
    current: list[str], synced: list[str], wanted: list[str], kinds: set[str]
) -> tuple[list[str], list[str]]:
    """``(tags, odoo_tags)`` after a row: the wanted tags added, a tag the sync gave before and
    Odoo no longer names taken away (for the kinds the row speaks for); a tag given by hand is
    never taken away, even when Odoo named it too."""
    dropped = {t for t in synced if t not in wanted and tag_kind(t) in kinds}
    merged = [t for t in current if t not in dropped] + [t for t in wanted if t not in current]
    owned = [t for t in synced if t not in dropped]
    owned += [t for t in wanted if t not in current and t not in synced]
    return merged, owned


SYNCED_FIELDS = ("sched", "due", "anchor", "stage")
# refreshed only when the row carries them (the Odoo has the field)
AGENT_FIELDS = ("brief", "estimate", "open_questions")
LOCAL_AUTHORSHIP = ("edited", "promoted")
# tagging a ticket is how it is taken into work (kb context asks for it), not a rewrite
NOT_AUTHORSHIP_FIELDS = frozenset({"tags"})
ORIGIN = "odoo-sync"


def authored_locally(event: records.Event) -> bool:
    """An edit or a promote, except an edit of the tags alone."""
    if event.kind not in LOCAL_AUTHORSHIP:
        return False
    if event.kind == "edited":
        return bool(set(event.data.get("fields", [])) - NOT_AUTHORSHIP_FIELDS)
    return True


def view_of(scope: str) -> list[str]:
    """The scope a synced task lands in, plus global (ADR 0003)."""
    return ["global"] if scope == "global" else [scope, "global"]


@dataclass
class _Plan:
    """The changes one matched record gets from its row, written once the links are known."""

    record: records.Record
    key: str
    changed: dict[str, Any]
    what: list[str]
    links: list[str] = field(default_factory=list)


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
    ``external_id`` normalised to the key. Schedule, stage, the tags from modules and type,
    brief, estimate and open questions are refreshed; the body is written once, the title only
    on records the sync itself created (``origin = odoo-sync``) and nobody edited. The parent
    and the blockers become PART_OF and BLOCKS edges between the records of the rows. Records
    with ``source = odoo`` the sync has never seen (promoted, migrated) are left alone and
    reported.
    """
    odoo = cfg.sources.odoo
    if not odoo.scope:
        raise SyncError("sources.odoo.scope is not set")
    vocab = _vocabulary(cfg, odoo.scope)
    result = SyncResult(count=len(rows))
    existing = _odoo_tasks(tx)
    by_external: dict[str, list[records.Record]] = {}
    for rec in existing:
        if rec.external_id is not None:
            by_external.setdefault(rec.external_id, []).append(rec)
    events = records.events_for(tx, [r.id for r in existing])
    matched: set[int] = set()
    corpus: similar.Corpus | None = None
    # the records of each row, by Odoo id: what the links are drawn between
    by_odoo: dict[int, list[records.Record]] = {}
    props_of: dict[int, dict[str, Any]] = {}
    plans: dict[int, _Plan] = {}
    created: dict[int, Change] = {}
    for row in rows:
        try:
            props = desired_props(row, cfg, vocab)
        except (records.RecordError, KeyError, ValueError, TypeError) as exc:
            result.skipped.append(f"{row.get('id', '?')} ({exc})")
            continue
        props_of[row["id"]] = props
        group = [
            rec
            for key in dict.fromkeys((props["external_id"], props["odoo_id"]))
            for rec in by_external.get(key, [])
            if rec.id not in matched
        ]
        if not group:
            rec = _create(tx, props, cfg, at, session)
            by_odoo[row["id"]] = [rec]
            result.created.append(rec.id)
            created[rec.id] = Change(
                rec.id, props["external_id"], rec.title, "new", describe_new(props)
            )
            corpus = corpus or similar.Corpus.load(tx)
            _check_duplicate(
                tx, corpus, rec, key=props["external_id"], scope=odoo.scope, result=result
            )
            continue
        by_odoo[row["id"]] = group
        for current in group:
            matched.add(current.id)
            plans[current.id] = _plan(current, props, events[current.id])
    for rid, note in _sync_links(tx, by_odoo, props_of, at):
        if rid in plans:
            plans[rid].links.append(note)
        elif rid in created:
            created[rid].what.append(note)
    result.changes += created.values()
    for plan in plans.values():
        _apply(tx, plan, at=at, session=session, result=result)
    _close_vanished(tx, existing, matched, at=at, session=session, result=result)
    return result


def _vocabulary(cfg: Config, scope: str) -> tags.Vocabulary:
    """The tags the synced tasks may get; a broken tags.toml fails the sync, loud."""
    try:
        return tags.load(cfg, scope)
    except tags.VocabularyError as exc:
        raise SyncError(str(exc)) from exc


def _close_vanished(
    tx: ManagedTransaction,
    existing: list[records.Record],
    matched: set[int],
    *,
    at: dt.datetime,
    session: str | None,
    result: SyncResult,
) -> None:
    """Close the open odoo tasks the sync has seen before and the result no longer holds;
    report the ones it has never seen (promoted, migrated) as untracked."""
    for rec in existing:
        if rec.id in matched or rec.done_at is not None or rec.archived_at is not None:
            continue
        if rec.synced_at is None:
            result.untracked.append(rec.id)
            continue
        records.add_event(tx, rec.id, "done", at, note="closed in Odoo", session=session)
        records.touch(tx, rec.id, at, done_at=at)
        result.closed.append(rec.id)
        key = rec.external_id or str(rec.id)
        result.changes.append(Change(rec.id, key, rec.title, "closed", ["closed in Odoo"]))


def _create(
    tx: ManagedTransaction,
    props: dict[str, Any],
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> records.Record:
    synced_tags = props.get("tags", [])
    return records.create(
        tx,
        kind="task",
        scope=cfg.sources.odoo.scope,
        title=props["title"],
        at=at,
        body=props["body"],
        tags=synced_tags,
        source="odoo",
        external_id=props["external_id"],
        schedule={k: props[k] for k in ("sched", "due", "anchor", "days")},
        session=session,
        stage=props["stage"],
        synced_at=at,
        origin=ORIGIN,
        extra={
            **{k: props[k] for k in AGENT_FIELDS if k in props},
            "odoo_tags": synced_tags,
        },
    )


def _plan(current: records.Record, props: dict[str, Any], history: list[records.Event]) -> _Plan:
    """What one row changes on one matched record: schedule, stage, key, tags, brief, estimate,
    open questions; the title only on a record the sync created and nobody edited."""
    changed = {k: props[k] for k in SYNCED_FIELDS if getattr(current, k) != props[k]}
    if current.external_id != props["external_id"]:
        changed["external_id"] = props["external_id"]
    authored = current.origin != ORIGIN or any(authored_locally(e) for e in history)
    if not authored and current.title != props["title"]:
        changed["title"] = props["title"]
    if current.done_at is not None:
        # reopened in Odoo after kb saw it closed
        changed["done_at"] = None
    if "tags" in props:
        merged, owned = merge_tags(
            current.tags, current.odoo_tags, props["tags"], props["tag_kinds"]
        )
        if merged != current.tags:
            changed["tags"] = merged
        if owned != current.odoo_tags:
            changed["odoo_tags"] = owned
    for name in AGENT_FIELDS:
        if name in props and getattr(current, name) != props[name]:
            changed[name] = props[name]
    return _Plan(current, props["external_id"], changed, describe(current, changed))


def _apply(
    tx: ManagedTransaction,
    plan: _Plan,
    *,
    at: dt.datetime,
    session: str | None,
    result: SyncResult,
) -> None:
    """Write one plan: a ``synced`` event naming what changed, or only ``synced_at``."""
    current, changed = plan.record, plan.changed
    if not changed and not plan.links:
        records.mark_synced(tx, current.id, at)
        return
    # odoo_tags is bookkeeping: it changes with tags, or when a tag given by hand is Odoo's too
    shown = sorted(k for k in changed if k != "odoo_tags")
    links = sorted(plan.links)
    payload: dict[str, Any] = {"changed": shown + (["links"] if links else [])}
    if links:
        payload["links"] = links
    if payload["changed"]:
        records.add_event(tx, current.id, "synced", at, payload=payload, session=session)
        result.updated.append(current.id)
        result.changes.append(
            Change(current.id, plan.key, current.title, "updated", [*plan.what, *links])
        )
        records.touch(tx, current.id, at, synced_at=at, **changed)
    else:
        records.mark_synced(tx, current.id, at)
        tx.run(
            "MATCH (r:Record {id: $id}) SET r += $props",
            id=current.id,
            props=changed,
        )


def _sync_links(
    tx: ManagedTransaction,
    by_odoo: dict[int, list[records.Record]],
    props_of: dict[int, dict[str, Any]],
    at: dt.datetime,
) -> list[tuple[int, str]]:
    """Draw the parent (PART_OF, child to parent) and the blockers (BLOCKS, blocker to blocked)
    between the records of the rows, as edges marked ``origin = odoo-sync``. Such an edge is
    removed once Odoo drops the link between two tasks both still in the result; an edge to a
    task gone from the result stays (closed or no longer mine: the sync cannot tell), and an
    edge drawn by hand is never touched. Returns ``(record id, note)`` for the record each
    change belongs to: the child of a PART_OF, the blocked task of a BLOCKS."""
    wanted: set[tuple[str, int, int]] = set()
    owned: set[tuple[str, int]] = set()  # (type, record) whose edges of that type Odoo speaks for
    for odoo_id, recs in by_odoo.items():
        props = props_of[odoo_id]
        if "parent" in props:
            for child in recs:
                owned.add(("PART_OF", child.id))
                for parent in by_odoo.get(props["parent"] or 0, []):
                    wanted.add(("PART_OF", child.id, parent.id))
        if "blockers" in props:
            for task in recs:
                owned.add(("BLOCKS", task.id))
                for blocker_id in props["blockers"]:
                    for blocker in by_odoo.get(blocker_id, []):
                        wanted.add(("BLOCKS", blocker.id, task.id))
    in_result = {rec.id for recs in by_odoo.values() for rec in recs}
    notes: list[tuple[int, str]] = []
    rows = tx.run(
        "MATCH (a:Record)-[l:PART_OF|BLOCKS {origin: $origin}]->(b:Record) "
        "WHERE a.id IN $ids OR b.id IN $ids "
        "RETURN type(l) AS type, a.id AS a, b.id AS b",
        origin=ORIGIN,
        ids=sorted(in_result),
    )
    present: set[tuple[str, int, int]] = set()
    for row in list(rows):
        edge = (row["type"], row["a"], row["b"])
        present.add(edge)
        owner, other = (row["a"], row["b"]) if row["type"] == "PART_OF" else (row["b"], row["a"])
        if edge in wanted or (row["type"], owner) not in owned or other not in in_result:
            continue
        tx.run(
            f"MATCH (:Record {{id: $a}})-[l:{row['type']} {{origin: $origin}}]->"
            "(:Record {id: $b}) DELETE l",
            a=row["a"],
            b=row["b"],
            origin=ORIGIN,
        )
        notes.append((owner, "-" + link_note(*edge)))
    for edge in sorted(wanted - present):
        rel, a, b = edge
        made, _ = records.link(tx, records.get(tx, a), rel, records.get(tx, b), at, origin=ORIGIN)
        if made:
            notes.append((a if rel == "PART_OF" else b, "+" + link_note(*edge)))
    return notes


def link_note(rel: str, a: int, b: int) -> str:
    """``PART_OF -> [parent]`` on the child, ``BLOCKS <- [blocker]`` on the blocked task."""
    return f"PART_OF -> [{b}]" if rel == "PART_OF" else f"BLOCKS <- [{a}]"


def _hours(value: float | None) -> str:
    return "-" if value is None else f"{value:g}h"


def _plain(value: Any) -> str:
    return "-" if value is None else str(value)


def describe_new(props: dict[str, Any]) -> list[str]:
    """What a new record arrives with, beyond its title and schedule."""
    what = []
    if props.get("tags"):
        what.append("tags " + " ".join(f"+{t}" for t in props["tags"]))
    if props.get("brief"):
        what.append(f"brief ({len(props['brief'])} chars)")
    if props.get("estimate") is not None:
        what.append(f"estimate {_hours(props['estimate'])}")
    if props.get("open_questions"):
        what.append(f"open questions {props['open_questions']}")
    return what


def describe(current: records.Record, changed: dict[str, Any]) -> list[str]:
    """One phrase per changed field, in a fixed order."""
    what = []
    if "title" in changed:
        what.append("title follows Odoo")
    if "external_id" in changed:
        what.append(f"key {_plain(current.external_id)} -> {changed['external_id']}")
    for name in SYNCED_FIELDS:
        if name in changed:
            what.append(f"{name} {_plain(getattr(current, name))} -> {_plain(changed[name])}")
    if "done_at" in changed:
        what.append("reopened")
    if "tags" in changed:
        added = [f"+{t}" for t in changed["tags"] if t not in current.tags]
        removed = [f"-{t}" for t in current.tags if t not in changed["tags"]]
        what.append("tags " + " ".join(added + removed))
    if "brief" in changed:
        brief = changed["brief"]
        if brief is None:
            what.append("brief removed")
        else:
            verb = "added" if current.brief is None else "changed"
            what.append(f"brief {verb} ({len(brief)} chars)")
    if "estimate" in changed:
        what.append(f"estimate {_hours(current.estimate)} -> {_hours(changed['estimate'])}")
    if "open_questions" in changed:
        old, new = current.open_questions, changed["open_questions"]
        what.append(f"open questions {_plain(old)} -> {_plain(new)}")
    return what


def _check_duplicate(
    tx: ManagedTransaction,
    corpus: similar.Corpus,
    rec: records.Record,
    *,
    key: str,
    scope: str,
    result: SyncResult,
) -> None:
    """The duplicate check of ``kb add``, against the open threads that are not Odoo tasks:
    a ticket filed for a thread nobody promoted arrives as a second task (five of the six
    duplicates merged by 2026-10-22 came in this way, the sixth through kb add)."""
    found = similar.find(
        tx,
        corpus,
        title=rec.title,
        summary=None,
        kind="task",
        scopes=view_of(scope),
        exclude=[rec.id],
        no_odoo=True,
        limit=1,
    )
    if found:
        result.duplicates.append(Duplicate(rec, found[0].record, key, found[0].similarity))


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
