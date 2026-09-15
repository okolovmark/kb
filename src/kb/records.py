"""Records, events, links and the task commands' bookkeeping, over one Neo4j transaction each."""

import datetime as dt
import itertools
import json
import re
from dataclasses import dataclass, field, fields
from typing import Any

from dateutil.rrule import rrulestr
from neo4j import ManagedTransaction
from neo4j.graph import Node
from neo4j.time import Date, DateTime

from kb import urgency
from kb.calendar import local_date
from kb.config import Config
from kb.db import COUNTER_NAME, next_id

KINDS = ("task", "note", "howto", "feedback", "reference", "decision", "session")
LABELS = {kind: kind.capitalize() for kind in KINDS}
SCHEDULES = ("hard", "soft", "window", "age", "once")
DAYS = ("work", "off", "any")
SOURCES = ("manual", "odoo")
# where a session came from: the SessionStart matcher, or the journal migration
SESSION_SOURCES = ("startup", "resume", "clear", "compact", "fork", "migrated")
REL_TYPES = ("RELATED", "SUPERSEDES", "BLOCKS", "OPENED_IN", "TOUCHED", "PART_OF")
EVENT_KINDS = (
    "created",
    "edited",
    "done",
    "skip",
    "snooze",
    "shown",
    "synced",
    "promoted",
    "archived",
    "superseded",
    "merged",
    "linked",
    "opened",
    "closed",
)
TITLE_MAX = 120
SLUG_MAX = 60
SESSION_SLUG = re.compile(r"^\d{4}-\d{2}-\d{2}-\d+$")
SESSION_WINDOW = dt.timedelta(hours=24)
WINDOW_ALIASES = {"weekend": "FREQ=WEEKLY;BYDAY=SA,SU", "sat,sun": "FREQ=WEEKLY;BYDAY=SA,SU"}
REL_PATTERN = "|".join(REL_TYPES)
# tiebreaker for events written at the same instant in one command (shown + done)
_SEQ = itertools.count()


class RecordError(ValueError):
    """A domain error the CLI prints as one line."""


def to_py(value: Any) -> Any:
    """Driver temporal values to stdlib ones; everything else unchanged."""
    if isinstance(value, DateTime | Date):
        return value.to_native()
    return value


@dataclass
class Record:
    """One ``(:Record)`` node; schedule fields are set on tasks only."""

    id: int
    kind: str
    scope: str
    title: str
    body: str = ""
    summary: str | None = None
    slug: str | None = None
    created_at: dt.datetime | None = None
    updated_at: dt.datetime | None = None
    pinned: bool = False
    tags: list[str] = field(default_factory=list)
    source: str = "manual"
    external_id: str | None = None
    journal_ref: str | None = None
    archived_at: dt.datetime | None = None
    done_at: dt.datetime | None = None
    sched: str | None = None
    due: dt.date | None = None
    window_rrule: str | None = None
    anchor: dt.date | None = None
    days: str | None = None
    repeat: str | None = None
    snoozed_until: dt.date | None = None
    skips: int = 0
    backlog: bool = False
    escalation: str | None = None
    stage: str | None = None
    synced_at: dt.datetime | None = None
    origin: str | None = None
    session_id: str | None = None
    opened_at: dt.datetime | None = None
    closed_at: dt.datetime | None = None
    reason: str | None = None
    no_summary: bool = False

    @classmethod
    def from_node(cls, node: Node) -> Record:
        """Build from a driver node, ignoring properties this version does not know."""
        names = {f.name for f in fields(cls)}
        data = {k: to_py(v) for k, v in node.items() if k in names}
        return cls(**data)

    def to_json(self) -> dict[str, Any]:
        """Plain dict with ISO dates."""
        out: dict[str, Any] = {}
        for f in fields(self):
            value = getattr(self, f.name)
            out[f.name] = value.isoformat() if isinstance(value, dt.date | dt.datetime) else value
        return out


@dataclass
class Event:
    """One ``(:Event)-[:ON]->(:Record)`` node."""

    kind: str
    at: dt.datetime
    note: str | None = None
    payload: str | None = None
    session: str | None = None

    @classmethod
    def from_node(cls, node: Node) -> Event:
        names = {f.name for f in fields(cls)}
        return cls(**{k: to_py(v) for k, v in node.items() if k in names})

    @property
    def data(self) -> dict[str, Any]:
        """Parsed payload, ``{}`` when absent."""
        return json.loads(self.payload) if self.payload else {}

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "at": self.at.isoformat(),
            "note": self.note,
            "payload": self.data or None,
            "session": self.session,
        }


@dataclass(frozen=True)
class Link:
    """A vocabulary relationship seen from one record: ``->`` outgoing, ``<-`` incoming."""

    type: str
    direction: str
    other: Record
    hop: int = 1


# --- validation -------------------------------------------------------------------------


def check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise RecordError(f"unknown kind {kind!r}; one of {', '.join(KINDS)}")
    return kind


def check_rel_type(rel: str) -> str:
    """Normalize to upper case; a type outside the closed vocabulary is an error."""
    upper = rel.upper()
    if upper not in REL_TYPES:
        raise RecordError(f"unknown relationship type {rel!r}; one of {', '.join(REL_TYPES)}")
    return upper


def check_title(title: str) -> str:
    title = " ".join(title.split())
    if not title:
        raise RecordError("title must not be empty")
    if len(title) > TITLE_MAX:
        raise RecordError(f"title longer than {TITLE_MAX} characters ({len(title)})")
    return title


def check_summary(text: str | None) -> str | None:
    """One line, trimmed; None when empty (an empty ``--summary`` clears it)."""
    summary = " ".join((text or "").split())
    return summary or None


def check_rrule(text: str) -> str:
    """An RFC 5545 RRULE body (``FREQ=...``), the ``RRULE:`` prefix stripped; one rule only."""
    body = text.removeprefix("RRULE:").strip()
    if "\n" in body or "\r" in body or "DTSTART" in body.upper():
        raise RecordError(f"invalid rrule {text!r}: a single RRULE body without DTSTART")
    try:
        rrulestr(body, dtstart=dt.datetime(2000, 1, 1))
    except (ValueError, TypeError, KeyError) as exc:
        raise RecordError(f"invalid rrule {text!r}: {exc}") from exc
    return body


def window_rrule(spec: str) -> str:
    """``weekend`` / ``sat,sun`` aliases or a raw RRULE."""
    return WINDOW_ALIASES.get(spec.lower()) or check_rrule(spec)


def check_repeat(spec: str) -> str:
    """``calendar:<rrule>`` or ``interval:<N>[d]`` -> the stored form."""
    kind, sep, value = spec.partition(":")
    if not sep:
        raise RecordError(f"repeat must be calendar:<rrule> or interval:<N>d, got {spec!r}")
    if kind == "calendar":
        return f"calendar:{check_rrule(value)}"
    if kind == "interval":
        digits = value.removesuffix("d")
        if not digits.isdigit() or int(digits) < 1:
            raise RecordError(f"interval must be a positive day count, got {value!r}")
        return f"interval:{int(digits)}"
    raise RecordError(f"repeat must be calendar:<rrule> or interval:<N>d, got {spec!r}")


def check_escalation(text: str) -> str:
    """A JSON object of threshold overrides: whisper, normal, loud, skips_to_backlog."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RecordError(f"escalation is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RecordError("escalation must be a JSON object")
    unknown = sorted(set(data) - set(urgency.OVERRIDE_KEYS))
    if unknown:
        raise RecordError(f"escalation: unknown key {unknown[0]!r}")
    bad = [k for k, v in data.items() if not isinstance(v, int) or isinstance(v, bool)]
    if bad:
        raise RecordError(f"escalation: {bad[0]} must be an integer")
    return json.dumps(data, sort_keys=True)


def slugify(title: str) -> str:
    """Lower case, ``[^a-z0-9]+`` -> ``_``, trimmed, at most 60 characters."""
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:SLUG_MAX].rstrip("_")
    return slug or "record"


def check_slug(slug: str) -> str:
    """An all-digit slug can never be addressed (digits mean an id)."""
    if slug.isdigit():
        raise RecordError(f"slug {slug!r} is all digits; digits mean an id, pass --slug")
    return slug


def check_session_slug(slug: str | None) -> str:
    """``YYYY-MM-DD-<n>``, kept verbatim: the date form is the session's address."""
    if not slug or not SESSION_SLUG.match(slug):
        raise RecordError(f"a session slug is YYYY-MM-DD-<n>, got {slug!r}")
    return slug


def default_days(scope: str) -> str:
    """``off`` for personal, ``any`` for global, ``work`` for a project."""
    return {"personal": "off", "global": "any"}.get(scope, "work")


# --- read -------------------------------------------------------------------------------


def get(tx: ManagedTransaction, record_id: int) -> Record:
    node = tx.run("MATCH (r:Record {id: $id}) RETURN r", id=record_id).single()
    if node is None:
        raise RecordError(f"no record {record_id}")
    return Record.from_node(node["r"])


def check_visible(rec: Record, scopes: list[str] | None) -> Record:
    """*rec* when its scope is in the view (None = every scope); the error names no title."""
    if scopes is not None and rec.scope not in scopes:
        raise RecordError(
            f"not visible in this scope: [{rec.id}] is in scope {rec.scope}; "
            f"rerun with --scope {rec.scope}"
        )
    return rec


def resolve(tx: ManagedTransaction, ref: str, scopes: list[str] | None = None) -> Record:
    """A record by integer id or slug, inside the view *scopes* (None = every scope)."""
    if ref.isdigit():
        return check_visible(get(tx, int(ref)), scopes)
    node = tx.run("MATCH (r:Record {slug: $slug}) RETURN r", slug=ref).single()
    if node is None:
        raise RecordError(f"no record {ref}")
    return check_visible(Record.from_node(node["r"]), scopes)


def exists(tx: ManagedTransaction, record_id: int) -> bool:
    return tx.run("MATCH (r:Record {id: $id}) RETURN 1", id=record_id).single() is not None


def unique_slug_taken(tx: ManagedTransaction, base: str) -> set[str]:
    """Every slug starting with *base*."""
    rows = tx.run(
        "MATCH (r:Record) WHERE r.slug STARTS WITH $base RETURN r.slug AS slug", base=base
    )
    return {row["slug"] for row in rows}


def unique_slug(tx: ManagedTransaction, base: str) -> str:
    """*base*, or ``base_2``, ``base_3``, ... the first one no record carries."""
    taken = unique_slug_taken(tx, base)
    if base not in taken:
        return base
    n = 2
    while f"{base}_{n}" in taken:
        n += 1
    return f"{base}_{n}"


def events_of(tx: ManagedTransaction, record_id: int, limit: int | None = None) -> list[Event]:
    """Events newest first."""
    query = "MATCH (e:Event)-[:ON]->(:Record {id: $id}) RETURN e ORDER BY e.at DESC, e.seq DESC"
    if limit is not None:
        query += " LIMIT $limit"
    return [Event.from_node(row["e"]) for row in tx.run(query, id=record_id, limit=limit)]


def events_for(tx: ManagedTransaction, ids: list[int]) -> dict[int, list[Event]]:
    """Events of many records in one query, newest first per record."""
    rows = tx.run(
        "MATCH (e:Event)-[:ON]->(r:Record) WHERE r.id IN $ids "
        "RETURN r.id AS id, e ORDER BY e.at DESC, e.seq DESC",
        ids=ids,
    )
    out: dict[int, list[Event]] = {i: [] for i in ids}
    for row in rows:
        out[row["id"]].append(Event.from_node(row["e"]))
    return out


def links_of(tx: ManagedTransaction, record_id: int) -> list[Link]:
    """Vocabulary relationships touching the record, both directions, grouped by type."""
    rows = tx.run(
        f"MATCH (r:Record {{id: $id}})-[l:{REL_PATTERN}]-(x:Record) "
        "RETURN type(l) AS type, startNode(l) = r AS outgoing, x ORDER BY type, x.id",
        id=record_id,
    )
    return [
        Link(row["type"], "->" if row["outgoing"] else "<-", Record.from_node(row["x"]))
        for row in rows
    ]


def graph_of(tx: ManagedTransaction, record_id: int, depth: int) -> list[Link]:
    """Records within *depth* hops over the vocabulary, each once at its shortest hop."""
    rows = tx.run(
        f"MATCH p = (r:Record {{id: $id}})-[:{REL_PATTERN}*1..{int(depth)}]-(x:Record) "
        "WHERE x.id <> $id WITH p, x, last(relationships(p)) AS l, nodes(p)[-2] AS prev "
        "RETURN length(p) AS hop, type(l) AS type, startNode(l) = prev AS outgoing, x "
        "ORDER BY hop, type, x.id",
        id=record_id,
    )
    seen: set[int] = set()
    out: list[Link] = []
    for row in rows:
        other = Record.from_node(row["x"])
        if other.id in seen:
            continue
        seen.add(other.id)
        out.append(Link(row["type"], "->" if row["outgoing"] else "<-", other, row["hop"]))
    return out


def search(
    tx: ManagedTransaction,
    query: str,
    *,
    scopes: list[str] | None,
    kind: str | None,
    include_archived: bool,
    limit: int,
) -> list[tuple[Record, float]]:
    """Full-text query over title+body through the ``record_text`` index."""
    rows = tx.run(
        "CALL db.index.fulltext.queryNodes('record_text', $q) YIELD node, score "
        "WHERE ($scopes IS NULL OR node.scope IN $scopes) "
        "AND ($kind IS NULL OR node.kind = $kind) "
        "AND ($all OR node.archived_at IS NULL) "
        "RETURN node, score LIMIT $limit",
        q=query,
        scopes=scopes,
        kind=kind,
        all=include_archived,
        limit=limit,
    )
    return [(Record.from_node(row["node"]), row["score"]) for row in rows]


def open_tasks(tx: ManagedTransaction, scopes: list[str] | None) -> list[Record]:
    """Tasks neither done nor archived, in the scopes (None = every scope)."""
    rows = tx.run(
        "MATCH (t:Record:Task) WHERE t.done_at IS NULL AND t.archived_at IS NULL "
        "AND ($scopes IS NULL OR t.scope IN $scopes) RETURN t ORDER BY t.id",
        scopes=scopes,
    )
    return [Record.from_node(row["t"]) for row in rows]


def pinned(tx: ManagedTransaction, scopes: list[str] | None, kinds: list[str]) -> list[Record]:
    """Pinned, non-archived records of the kinds and scopes, by id."""
    rows = tx.run(
        "MATCH (r:Record) WHERE r.pinned AND r.kind IN $kinds AND r.archived_at IS NULL "
        "AND ($scopes IS NULL OR r.scope IN $scopes) RETURN r ORDER BY r.id",
        scopes=scopes,
        kinds=kinds,
    )
    return [Record.from_node(row["r"]) for row in rows]


def recent(
    tx: ManagedTransaction, scopes: list[str] | None, kinds: list[str], limit: int
) -> list[Record]:
    """Non-pinned, non-archived records of the kinds and scopes, most recently updated first."""
    rows = tx.run(
        "MATCH (r:Record) WHERE NOT r.pinned AND r.kind IN $kinds AND r.archived_at IS NULL "
        "AND ($scopes IS NULL OR r.scope IN $scopes) "
        "RETURN r ORDER BY r.updated_at DESC, r.id DESC LIMIT $limit",
        scopes=scopes,
        kinds=kinds,
        limit=max(limit, 0),
    )
    return [Record.from_node(row["r"]) for row in rows]


def count_records(tx: ManagedTransaction) -> int:
    return tx.run("MATCH (r:Record) RETURN count(r) AS n").single(strict=True)["n"]


def get_meta(tx: ManagedTransaction, name: str) -> dict[str, Any] | None:
    row = tx.run("MATCH (m:Meta {name: $name}) RETURN m", name=name).single()
    return {k: to_py(v) for k, v in row["m"].items()} if row else None


def set_meta(tx: ManagedTransaction, name: str, **props: Any) -> None:
    tx.run("MERGE (m:Meta {name: $name}) SET m += $props", name=name, props=props)


# --- write ------------------------------------------------------------------------------


def add_event(
    tx: ManagedTransaction,
    record_id: int,
    kind: str,
    at: dt.datetime,
    *,
    note: str | None = None,
    payload: dict[str, Any] | None = None,
    session: str | None = None,
) -> None:
    """``(:Event)-[:ON]->(record)``; *payload* is stored as a JSON string."""
    if kind not in EVENT_KINDS:
        raise RecordError(f"unknown event kind {kind!r}")
    tx.run(
        "MATCH (r:Record {id: $id}) CREATE (e:Event {kind: $kind, at: $at, seq: $seq, note: $note, "
        "payload: $payload, session: $session})-[:ON]->(r)",
        id=record_id,
        kind=kind,
        at=at,
        seq=next(_SEQ),
        note=note,
        payload=json.dumps(payload, sort_keys=True, default=str) if payload else None,
        session=session,
    )


def add_skip(
    tx: ManagedTransaction,
    record_id: int,
    end: dt.date,
    at: dt.datetime,
    *,
    threshold: int,
    session: str | None,
) -> bool:
    """One ``skip`` per (record, window end): MERGE on the window key, so two sessions
    evaluating the same window cannot double-count; ``skips`` grows only on creation."""
    row = tx.run(
        "MATCH (r:Record {id: $id}) "
        "MERGE (e:Event {kind: 'skip', window: $window})-[:ON]->(r) "
        "ON CREATE SET e.at = $at, e.seq = $seq, e.payload = $payload, e.session = $session, "
        "e.fresh = true "
        "WITH r, e, coalesce(e.fresh, false) AS fresh REMOVE e.fresh "
        "WITH r, fresh WHERE fresh "
        "SET r.skips = coalesce(r.skips, 0) + 1, r.updated_at = $at "
        "WITH r, fresh SET r.backlog = r.skips >= $threshold "
        "RETURN fresh",
        id=record_id,
        window=end.isoformat(),
        at=at,
        seq=next(_SEQ),
        payload=json.dumps({"window": end.isoformat()}),
        session=session,
        threshold=threshold,
    ).single()
    return row is not None and bool(row["fresh"])


def touch(tx: ManagedTransaction, record_id: int, at: dt.datetime, **props: Any) -> Record:
    """Set *props* (None removes the property) and ``updated_at``; return the record."""
    tx.run(
        "MATCH (r:Record {id: $id}) SET r += $props, r.updated_at = $at",
        id=record_id,
        props=props,
        at=at,
    )
    return get(tx, record_id)


def create(
    tx: ManagedTransaction,
    *,
    kind: str,
    scope: str,
    title: str,
    at: dt.datetime,
    body: str = "",
    summary: str | None = None,
    slug: str | None = None,
    pinned: bool | None = None,
    tags: list[str] | None = None,
    source: str = "manual",
    external_id: str | None = None,
    journal_ref: str | None = None,
    schedule: dict[str, Any] | None = None,
    session: str | None = None,
    record_id: int | None = None,
    created_at: dt.datetime | None = None,
    stage: str | None = None,
    synced_at: dt.datetime | None = None,
    origin: str | None = None,
    note: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Record:
    """Create ``(:Record:<Kind>)`` with the next id (or *record_id*) and its ``created`` event.

    *note* goes on the ``created`` event (the migrations name the source file there);
    *extra* holds kind-specific properties (the session fields), set as given.

    Non-task kinds get a slug: the given one, or one derived from the title and made unique.
    A session's slug is given and kept verbatim (``YYYY-MM-DD-<n>``), its *source* is the
    SessionStart matcher. A task needs a *schedule* (``sched`` plus its dates); the caller
    knows today's date.
    """
    check_kind(kind)
    title = check_title(title)
    if kind == "session":
        slug = check_session_slug(slug)
        if slug in unique_slug_taken(tx, slug):
            raise RecordError(f"slug {slug!r} is taken")
    elif kind != "task":
        slug = unique_slug(tx, check_slug(slugify(slug or title)))
    elif slug:
        slug = unique_slug(tx, check_slug(slugify(slug)))
    allowed = SESSION_SOURCES if kind == "session" else SOURCES
    if source not in allowed:
        raise RecordError(f"source must be one of {', '.join(allowed)}")
    if schedule and kind != "task":
        raise RecordError("schedule flags apply to tasks only")
    if kind == "task" and not schedule:
        raise RecordError("a task needs a schedule")
    rid = record_id if record_id is not None else next_id(tx)
    created = created_at or at
    props: dict[str, Any] = {
        "id": rid,
        "kind": kind,
        "scope": scope,
        "title": title,
        "body": body,
        "summary": summary or None,
        "slug": slug,
        "created_at": created,
        "updated_at": created,
        "pinned": (kind == "feedback") if pinned is None else pinned,
        "tags": list(tags or []),
        "source": source,
        "external_id": external_id,
        "journal_ref": journal_ref,
        "stage": stage,
        "synced_at": synced_at,
        "origin": origin,
        **(extra or {}),
    }
    if kind == "task":
        props.update(skips=0, backlog=False, **(schedule or {}))
        props.setdefault("days", default_days(scope))
        if props["sched"] == "age" and props.get("anchor") is None:
            raise RecordError("an age task needs an anchor date")
    tx.run(f"CREATE (r:Record:{LABELS[kind]}) SET r = $props", props=props)
    add_event(tx, rid, "created", created, note=note, session=session)
    return get(tx, rid)


def link(tx: ManagedTransaction, a: Record, rel: str, b: Record, at: dt.datetime) -> bool:
    """``(a)-[:REL]->(b)``, idempotent; True when the relationship was created now."""
    rel = check_rel_type(rel)
    if a.id == b.id:
        raise RecordError("a record cannot link to itself")
    present = tx.run(
        f"MATCH (:Record {{id: $a}})-[l:{rel}]->(:Record {{id: $b}}) RETURN 1", a=a.id, b=b.id
    ).single()
    if present is not None:
        return False
    tx.run(
        f"MATCH (a:Record {{id: $a}}), (b:Record {{id: $b}}) "
        f"CREATE (a)-[:{rel} {{created_at: $at}}]->(b)",
        a=a.id,
        b=b.id,
        at=at,
    )
    return True


def unlink(tx: ManagedTransaction, a: Record, rel: str, b: Record) -> int:
    """Delete ``(a)-[:REL]->(b)``; the number of relationships removed."""
    rel = check_rel_type(rel)
    summary = tx.run(
        f"MATCH (a:Record {{id: $a}})-[l:{rel}]->(b:Record {{id: $b}}) DELETE l", a=a.id, b=b.id
    ).consume()
    return summary.counters.relationships_deleted


def move_edges_and_events(tx: ManagedTransaction, src: Record, dst: Record) -> int:
    """Re-point *src*'s events (its ``created`` stays) and vocabulary edges onto *dst*.

    Edges between the two records are dropped, not moved.
    """
    tx.run(
        "MATCH (e:Event)-[on:ON]->(s:Record {id: $s}) WHERE e.kind <> 'created' "
        "MATCH (d:Record {id: $d}) DELETE on CREATE (e)-[:ON]->(d)",
        s=src.id,
        d=dst.id,
    )
    rows = tx.run(
        f"MATCH (s:Record {{id: $s}})-[l:{REL_PATTERN}]-(x:Record) "
        "RETURN type(l) AS type, startNode(l) = s AS outgoing, x.id AS other, "
        "properties(l) AS props",
        s=src.id,
    )
    moved = 0
    for row in list(rows):
        if row["other"] != dst.id:
            pattern = "(d)-[m:%s]->(x)" if row["outgoing"] else "(x)-[m:%s]->(d)"
            tx.run(
                f"MATCH (d:Record {{id: $d}}), (x:Record {{id: $x}}) "
                f"MERGE {pattern % row['type']} ON CREATE SET m = $props",
                d=dst.id,
                x=row["other"],
                props=row["props"],
            )
            moved += 1
    tx.run(f"MATCH (s:Record {{id: $s}})-[l:{REL_PATTERN}]-() DELETE l", s=src.id)
    return moved


# --- tasks ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Assessment:
    """A task with its computed level for one day."""

    task: Record
    level: int
    reason: str
    sort_date: dt.date

    @property
    def name(self) -> str:
        return urgency.LEVEL_NAMES[self.level]

    @property
    def sort_key(self) -> tuple[int, dt.date, int]:
        return (-self.level, self.sort_date, self.task.id)


def _unsettled_window(
    task: Record, events: list[Event], today: dt.date, cfg: Config
) -> urgency.Window | None:
    """The most recent ended window nobody closed, or None (also while snoozed / bad rule)."""
    if task.sched != "window" or task.backlog or not task.window_rrule:
        return None
    if task.snoozed_until is not None and today < task.snoozed_until:
        return None
    try:
        window = urgency.last_window_before(task, today, cfg)
    except ValueError:
        return None
    if window is None or urgency.window_touched(events, *window, cfg, task.snoozed_until):
        return None
    return window


def settle_window(
    tx: ManagedTransaction,
    task: Record,
    *,
    events: list[Event],
    today: dt.date,
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> tuple[Record, list[Event]]:
    """Lazy bookkeeping: a window that ended untouched gets one ``skip`` event and ``skips += 1``.

    Nothing happens while the task is snoozed, and a window that ended inside a snooze period
    counts as touched. Idempotent: the skip is keyed on the window's last day.
    """
    window = _unsettled_window(task, events, today, cfg)
    if window is None:
        return task, events
    threshold, _ = urgency.window_threshold(task, cfg)
    if not add_skip(tx, task.id, window[1], at, threshold=threshold, session=session):
        return task, events
    return get(tx, task.id), events_of(tx, task.id)


def assess(
    tx: ManagedTransaction,
    tasks: list[Record],
    *,
    today: dt.date,
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> list[Assessment]:
    """Level of every task for *today*, after the window bookkeeping; sorted for display."""
    all_events = events_for(tx, [t.id for t in tasks])
    out: list[Assessment] = []
    for original in tasks:
        task, events = settle_window(
            tx,
            original,
            events=all_events[original.id],
            today=today,
            cfg=cfg,
            at=at,
            session=session,
        )
        lvl, reason = urgency.level(task, today, cfg, events)
        sort_date = task.due or task.anchor or urgency.created_day(task, cfg)
        out.append(Assessment(task, lvl, reason, sort_date))
    return sorted(out, key=lambda a: a.sort_key)


def next_occurrence(task: Record, today: dt.date) -> dt.date | None:
    """Where ``due`` moves on done: the next rrule date after max(today, due), or today + N.

    None when a COUNT/UNTIL rule has no occurrence left.
    """
    kind, _, value = (task.repeat or "").partition(":")
    if kind == "interval":
        return today + dt.timedelta(days=int(value))
    if kind == "calendar":
        # doing the taxes on the 14th must move the 15th to next month, not leave it for tomorrow
        base = max(today, task.due or today)
        start = dt.datetime.combine(task.due or today, dt.time())
        nxt = rrulestr(value, dtstart=start).after(dt.datetime.combine(base, dt.time()))
        return nxt.date() if nxt else None
    raise RecordError(f"task {task.id} has no repeat rule")


def done_today(events: list[Event], today: dt.date, cfg: Config) -> bool:
    """A ``done`` event dated *today* in the kb timezone."""
    return any(e.kind == "done" and local_date(e.at, cfg) == today for e in events)


def _window_done_state(
    task: Record, events: list[Event], today: dt.date, cfg: Config
) -> tuple[bool, dict[str, Any] | None]:
    """(already done this window, payload naming the window) for a window task."""
    try:
        window = urgency.window_containing(task, today, cfg) or urgency.last_window_before(
            task, today, cfg
        )
    except ValueError as exc:
        raise RecordError(f"[{task.id}] has an invalid rrule: {exc}") from exc
    if window is None:
        return False, None
    done = urgency.window_touch(events, *window, cfg) == "done"
    return done, {"window": window[1].isoformat()}


def mark_done(
    tx: ManagedTransaction,
    task: Record,
    *,
    comment: str | None,
    today: dt.date,
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> tuple[Record, dt.date | None, bool]:
    """``done`` event; recurring and window tasks advance and stay open, others get ``done_at``.

    A task already done today (or a window already closed) is left alone. Returns
    (record, next date or None, whether anything was written).
    """
    events = events_of(tx, task.id)
    already = task.done_at is not None or done_today(events, today, cfg)
    payload = None
    if task.sched == "window" and not already:
        already, payload = _window_done_state(task, events, today, cfg)
    if already:
        return task, None, False
    props: dict[str, Any] = {"skips": 0, "backlog": False, "snoozed_until": None}
    nxt: dt.date | None = None
    note = comment
    if task.sched == "window":
        nxt = urgency.next_window_start(task, today, cfg)
    elif task.repeat:
        nxt = next_occurrence(task, today)
        if nxt is None:
            note = f"{comment} (recurrence exhausted)" if comment else "recurrence exhausted"
            props["done_at"] = at
        else:
            props["due"] = nxt
    else:
        props["done_at"] = at
    add_event(tx, task.id, "done", at, note=note, payload=payload, session=session)
    return touch(tx, task.id, at, **props), nxt, True


def skip_window(
    tx: ManagedTransaction,
    task: Record,
    *,
    today: dt.date,
    cfg: Config,
    at: dt.datetime,
    session: str | None,
) -> tuple[Record, dt.date | None]:
    """Close the current (or last) window with a ``skip``; ``skips += 1``, backlog at the threshold.

    Returns (record, window end) or (record, None) when that window was already closed.
    """
    if task.sched != "window":
        raise RecordError(f"[{task.id}] is not a window task; skip applies to window tasks only")
    try:
        window = urgency.window_containing(task, today, cfg) or urgency.last_window_before(
            task, today, cfg
        )
    except ValueError as exc:
        raise RecordError(f"[{task.id}] has an invalid rrule: {exc}") from exc
    if window is None:
        raise RecordError(f"[{task.id}] has no window before {today}")
    start, end = window
    if urgency.window_touched(events_of(tx, task.id), start, end, cfg, task.snoozed_until):
        return task, None
    threshold, _ = urgency.window_threshold(task, cfg)
    if not add_skip(tx, task.id, end, at, threshold=threshold, session=session):
        return task, None
    return get(tx, task.id), end


def snooze(
    tx: ManagedTransaction,
    task: Record,
    until: dt.date,
    *,
    today: dt.date,
    at: dt.datetime,
    session: str | None,
) -> Record:
    """Silence the task until *until*; a hard task is never snoozed onto or past its deadline."""
    if task.kind != "task":
        raise RecordError(f"[{task.id}] is a {task.kind}, snooze applies to tasks")
    if until <= today:
        raise RecordError(f"snooze date must be after {today}")
    if task.sched == "hard" and task.due is not None and until >= task.due:
        raise RecordError(f"cannot snooze past the deadline {task.due}")
    add_event(tx, task.id, "snooze", at, note=until.isoformat(), session=session)
    return touch(tx, task.id, at, snoozed_until=until, skips=0, backlog=False)


def counter_value(tx: ManagedTransaction) -> int:
    row = tx.run("MATCH (c:Counter {name: $n}) RETURN c.value AS v", n=COUNTER_NAME).single()
    if row is None:
        raise RecordError("Counter missing; run kb-setup")
    return row["v"]


def max_record_id(tx: ManagedTransaction) -> int:
    row = tx.run("MATCH (r:Record) RETURN coalesce(max(r.id), 0) AS m").single(strict=True)
    return row["m"]


def mark_synced(tx: ManagedTransaction, record_id: int, at: dt.datetime) -> None:
    """``synced_at`` only: an unchanged row must not move ``updated_at``."""
    tx.run("MATCH (r:Record {id: $id}) SET r.synced_at = $at", id=record_id, at=at)


def set_counter(tx: ManagedTransaction, value: int) -> None:
    tx.run("MATCH (c:Counter {name: $n}) SET c.value = $v", n=COUNTER_NAME, v=value)


def raise_counter(tx: ManagedTransaction, value: int) -> None:
    tx.run(
        "MATCH (c:Counter {name: $n}) "
        "SET c.value = CASE WHEN c.value < $v THEN $v ELSE c.value END",
        n=COUNTER_NAME,
        v=value,
    )


# --- sessions ---------------------------------------------------------------------------


def session_by_id(
    tx: ManagedTransaction, session_id: str, scopes: list[str] | None = None
) -> Record | None:
    """The Session record carrying Claude's *session_id*; None when there is none.

    *scopes* applies the view rule of ADR 0003 (None = every scope): named by ``--id``, a
    session outside the view is refused without printing its title, like every other REF.
    """
    row = tx.run("MATCH (s:Record:Session {session_id: $sid}) RETURN s", sid=session_id).single()
    if row is None:
        return None
    return check_visible(Record.from_node(row["s"]), scopes)


def next_session_slug(tx: ManagedTransaction, day: dt.date) -> str:
    """``<day>-<n>``: the lowest n no record of that date carries.

    Counted across every scope, not per scope: slugs are unique store-wide, so the first
    personal session of a day the project already used twice is number 3.
    """
    prefix = f"{day.isoformat()}-"
    taken = unique_slug_taken(tx, prefix)
    n = 1
    while f"{prefix}{n}" in taken:
        n += 1
    return f"{prefix}{n}"


def current_session(
    tx: ManagedTransaction, scopes: list[str] | None, *, at: dt.datetime, env_id: str | None
) -> Record | None:
    """The session a write belongs to.

    ``KB_SESSION`` set: the Session with that id whatever its scope (the hook exports it and
    knows better than the cwd), or None when the hook opened none. Otherwise the most recently
    opened, still open, not archived Session in the view *scopes* (None = every scope) opened
    within the last 24 hours; None when there is none.
    """
    if env_id:
        return session_by_id(tx, env_id)
    row = tx.run(
        "MATCH (s:Record:Session) WHERE s.closed_at IS NULL AND s.opened_at >= $since "
        "AND s.archived_at IS NULL AND ($scopes IS NULL OR s.scope IN $scopes) "
        "RETURN s ORDER BY s.opened_at DESC, s.id DESC LIMIT 1",
        since=at - SESSION_WINDOW,
        scopes=scopes,
    ).single()
    return Record.from_node(row["s"]) if row else None


def attach(
    tx: ManagedTransaction, session: Record, record_id: int, rel: str, at: dt.datetime
) -> bool:
    """``(record)-[:OPENED_IN]->(session)`` or ``(session)-[:TOUCHED]->(record)``, MERGEd:
    one edge per pair. False when nothing was created (present already, or the session
    itself)."""
    if record_id == session.id:
        return False
    pattern = {
        "OPENED_IN": "(r)-[l:OPENED_IN]->(s)",
        "TOUCHED": "(s)-[l:TOUCHED]->(r)",
    }[rel]
    summary = tx.run(
        f"MATCH (s:Record:Session {{id: $s}}), (r:Record {{id: $r}}) "
        f"MERGE {pattern} ON CREATE SET l.created_at = $at",
        s=session.id,
        r=record_id,
        at=at,
    ).consume()
    return summary.counters.relationships_created > 0


def sessions(
    tx: ManagedTransaction, scopes: list[str] | None, *, limit: int, open_only: bool
) -> list[Record]:
    """Sessions in the view, archived ones left out, newest opened first."""
    rows = tx.run(
        "MATCH (s:Record:Session) WHERE ($scopes IS NULL OR s.scope IN $scopes) "
        "AND s.archived_at IS NULL AND (NOT $open OR s.closed_at IS NULL) "
        "RETURN s ORDER BY s.opened_at DESC, s.id DESC LIMIT $limit",
        scopes=scopes,
        open=open_only,
        limit=max(limit, 0),
    )
    return [Record.from_node(row["s"]) for row in rows]


def session_link_counts(tx: ManagedTransaction, ids: list[int]) -> dict[int, tuple[int, int]]:
    """``{session id: (records opened in it, records it touched)}``."""
    rows = tx.run(
        "MATCH (s:Record:Session) WHERE s.id IN $ids "
        "OPTIONAL MATCH (s)<-[o:OPENED_IN]-() WITH s, count(o) AS opened "
        "OPTIONAL MATCH (s)-[t:TOUCHED]->() RETURN s.id AS id, opened, count(t) AS touched",
        ids=ids,
    )
    out = dict.fromkeys(ids, (0, 0))
    for row in rows:
        out[row["id"]] = (row["opened"], row["touched"])
    return out


def sessions_without_summary(
    tx: ManagedTransaction, scopes: list[str] | None, since: dt.datetime
) -> list[Record]:
    """Closed sessions in the view with no body, closed at or after *since*, oldest first."""
    rows = tx.run(
        "MATCH (s:Record:Session) WHERE s.no_summary AND s.closed_at >= $since "
        "AND s.archived_at IS NULL AND ($scopes IS NULL OR s.scope IN $scopes) "
        "RETURN s ORDER BY s.closed_at, s.id",
        since=since,
        scopes=scopes,
    )
    return [Record.from_node(row["s"]) for row in rows]
