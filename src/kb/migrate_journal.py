"""kb migrate journal: journal day files become Session records; ``[ID]`` and ``[[slug]]``
mentions become TOUCHED edges, ids after added/filed/opened also OPENED_IN."""

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from neo4j import ManagedTransaction

from kb import records
from kb.config import Config
from kb.migrate_nodes import MIGRATED_TAG, link_targets, strip_code

DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")
SESSION_HEADING = re.compile(r"^## Session\b(.*)$")
HEADING = "## "
# the block number: the first integer after "Session", unless it is the hour of a time
NUMBER = re.compile(r"^\s*(\d+)(?![:\d])")
TIME = re.compile(r"\b(\d{1,2}):(\d{2})\b")
# the dash of a range: hyphen, en dash or em dash
TIME_RANGE = re.compile(r"\b(\d{1,2}):(\d{2})\s*[-\u2013\u2014]\s*\d{1,2}:\d{2}\b")
# an aside about another day: "(started 2026-08-03 ~16:30, spans midnight)"
PARENS = re.compile(r"\([^()]*\)")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
RULE = "---"
INTENT = re.compile(r"^\*\*Intent:\*\*\s*(.*)$")
TAGS = re.compile(r"^\*\*Tags:\*\*\s*(.*)$")
HASHTAG = re.compile(r"#([A-Za-z0-9_][A-Za-z0-9_-]*)")
ID_REF = re.compile(r"\[(\d+)\]")
# "added [20] (note), [21]" / "added [48] [49] [50]": every id in the chain was opened here
OPENED = re.compile(
    r"\b(?:added|filed|opened)\s+\[\d+\](?:\s*(?:\([^()\n]*\))?\s*,?\s*\[\d+\])*", re.IGNORECASE
)
LEADING_BLANK = re.compile(r"\A(?:[ \t]*\n)+")
# "**Nodes created:** [[a]], [[b]]; updated [[c]]": only the part before the ";" was created
NODES_CREATED = re.compile(r"^\*\*Nodes created:\*\*\s*(.*)$")
# an Intent wrapped onto the next line stops at the next field, heading, list or table
CONTINUATION_STOP = ("**", "#", "-", "|", "`", ">")
HOURS, MINUTES = 24, 60
# a journal entry can mention a record created the same night; one day of slack covers the
# entry written after midnight, nothing beyond it
ID_GRACE = dt.timedelta(days=1)


@dataclass(frozen=True)
class Block:
    """One ``## Session`` block of a day file, before the store is consulted."""

    file: str
    date: dt.date
    n: int
    heading: str
    by_position: bool
    time: dt.time
    at: dt.datetime
    body: str
    intent: str | None
    tags: tuple[str, ...]
    ids: tuple[int, ...]
    opened_ids: tuple[int, ...]
    targets: tuple[str, ...]
    opened_targets: tuple[str, ...]

    @property
    def slug(self) -> str:
        return f"{self.date.isoformat()}-{self.n}"

    @property
    def session_id(self) -> str:
        return f"journal:{self.slug}"

    @property
    def title(self) -> str:
        """The intent cut to 120 characters; ``session <date> <n>`` without one."""
        if self.intent:
            return cut_title(self.intent)
        return f"session {self.date.isoformat()} {self.n}"


def cut_title(text: str) -> str:
    text = " ".join(text.split())
    if len(text) > records.TITLE_MAX:
        text = text[: records.TITLE_MAX - 1].rstrip() + "…"
    return text


# --- one day file -----------------------------------------------------------------------


@dataclass
class _Raw:
    heading: str
    lines: list[str] = field(default_factory=list)


def _closes(fence: str, line: str) -> bool:
    return re.match(rf"^ {{0,3}}{re.escape(fence[0])}{{{len(fence)},}}\s*$", line) is not None


def split_blocks(text: str) -> list[_Raw]:
    """The session blocks of a day file.

    A ``## Session`` heading at column 0 outside a code fence starts one; any other ``## ``
    heading (the standup, a stray section), or a ``---`` line followed by a blank line or
    EOF, ends it. Whatever sits outside a session block (the ``# date`` header, standups)
    is dropped.
    """
    lines = text.split("\n")
    blocks: list[_Raw] = []
    current: _Raw | None = None
    fence: str | None = None
    for i, line in enumerate(lines):
        if fence is not None:
            if _closes(fence, line):
                fence = None
            if current is not None:
                current.lines.append(line)
            continue
        opened = FENCE.match(line)
        if opened:
            fence = opened.group(1)
            if current is not None:
                current.lines.append(line)
            continue
        if line.startswith(HEADING):
            if current is not None:
                blocks.append(current)
            current = _Raw(line) if SESSION_HEADING.match(line) else None
            continue
        if current is None:
            continue
        if line.rstrip() == RULE and (i + 1 >= len(lines) or not lines[i + 1].strip()):
            blocks.append(current)
            current = None
            continue
        current.lines.append(line)
    if current is not None:
        blocks.append(current)
    return blocks


def parse_heading(heading: str) -> tuple[int | None, dt.time]:
    """(the block number or None, the heading's clock time or midnight).

    A time in parentheses is an aside about another day (``(started 2026-08-03 ~16:30, spans
    midnight)``) and is ignored. Of what is left, a range ``HH:MM-HH:MM`` gives its first
    time; anything else gives its last, so ``started ..., closed ...`` dates the block by its
    close. Blocks then sort in file order within their day, which the start of an overnight
    session would break.
    """
    rest = SESSION_HEADING.match(heading).group(1)
    number = NUMBER.match(rest)
    timed = PARENS.sub(" ", rest)
    ranged = TIME_RANGE.search(timed)
    times = [t for t in TIME.findall(timed) if _valid(t)]
    if ranged is not None and _valid(ranged.groups()):
        time = _time(ranged.groups())
    else:
        time = _time(times[-1]) if times else dt.time()
    return (int(number.group(1)) if number else None), time


def _valid(hhmm: tuple[str, str]) -> bool:
    return int(hhmm[0]) < HOURS and int(hhmm[1]) < MINUTES


def _time(hhmm: tuple[str, str]) -> dt.time:
    return dt.time(int(hhmm[0]), int(hhmm[1]))


def number_blocks(numbers: list[int | None]) -> list[tuple[int, bool]]:
    """Each block's number: its own on first use; a repeated or missing one becomes the next
    integer above every number the file uses, flagged ``by_position``."""
    first: dict[int, int] = {}
    for i, n in enumerate(numbers):
        if n is not None and n not in first:
            first[n] = i
    top = max(first, default=0)
    out: list[tuple[int, bool]] = []
    for i, n in enumerate(numbers):
        if n is not None and first[n] == i:
            out.append((n, False))
        else:
            top += 1
            out.append((top, True))
    return out


def intent_of(lines: list[str]) -> str | None:
    """The ``**Intent:**`` value with its wrapped continuation lines; None without one."""
    for i, line in enumerate(lines):
        match = INTENT.match(line)
        if match is None:
            continue
        parts = [match.group(1).strip()]
        for nxt in lines[i + 1 :]:
            text = nxt.strip()
            if not text or text.startswith(CONTINUATION_STOP):
                break
            parts.append(text)
        return " ".join(p for p in parts if p) or None
    return None


def tags_of(lines: list[str]) -> tuple[str, ...]:
    """The hashtags of the first ``**Tags:**`` line, ``#`` dropped, in order, distinct."""
    for line in lines:
        match = TAGS.match(line)
        if match is not None:
            return tuple(dict.fromkeys(HASHTAG.findall(match.group(1))))
    return ()


def parse_day(path: Path, day: dt.date, cfg: Config) -> list[Block]:
    """The session blocks of one ``YYYY-MM-DD.md`` file, numbered and timed."""
    raws = split_blocks(path.read_text())
    headings = [parse_heading(raw.heading) for raw in raws]
    tz = ZoneInfo(cfg.general.timezone)
    blocks: list[Block] = []
    for raw, (n, by_position), (_, time) in zip(
        raws, number_blocks([h[0] for h in headings]), headings, strict=True
    ):
        body = LEADING_BLANK.sub("", "\n".join(raw.lines)).rstrip()
        prose = strip_code(body)
        ids = tuple(dict.fromkeys(int(x) for x in ID_REF.findall(prose)))
        opened = tuple(
            dict.fromkeys(
                int(x) for match in OPENED.finditer(prose) for x in ID_REF.findall(match.group(0))
            )
        )
        blocks.append(
            Block(
                file=path.name,
                date=day,
                n=n,
                heading=raw.heading,
                by_position=by_position,
                time=time,
                at=dt.datetime.combine(day, time, tzinfo=tz).astimezone(dt.UTC),
                body=body,
                intent=intent_of(raw.lines),
                tags=tags_of(raw.lines),
                ids=ids,
                opened_ids=opened,
                targets=link_targets(body, ""),
                opened_targets=created_targets(prose),
            )
        )
    return blocks


def created_targets(prose: str) -> tuple[str, ...]:
    """The slugs a ``**Nodes created:**`` line names as created, in order, distinct.

    Only up to the first ``;``: the corpus writes ``created [[a]]; updated [[b]]`` on one line
    and only the first half was opened in that session.
    """
    out: list[str] = []
    for line in prose.split("\n"):
        match = NODES_CREATED.match(line)
        if match is not None:
            out += link_targets(match.group(1).split(";")[0], "")
    return tuple(dict.fromkeys(out))


def load_dir(directory: Path, cfg: Config) -> tuple[list[Block], list[str]]:
    """Every ``YYYY-MM-DD.md`` of the directory: (blocks in file order, other ``*.md`` names)."""
    blocks: list[Block] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.md")):
        match = DAY_FILE.match(path.name)
        if match is None:
            skipped.append(path.name)
            continue
        try:
            day = dt.date.fromisoformat(match.group(1))
        except ValueError as exc:
            raise records.RecordError(f"{path.name}: not a date: {exc}") from exc
        blocks += parse_day(path, day, cfg)
    return blocks, skipped


# --- against the store ------------------------------------------------------------------


Edge = tuple[str, str, int]  # (rel, session slug, record id)


@dataclass
class Report:
    """What a run did, or would do under --dry-run."""

    created: list[Block] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    by_position: list[Block] = field(default_factory=list)
    renumbered: list[tuple[Block, str]] = field(default_factory=list)
    slugs: dict[str, str] = field(default_factory=dict)
    skipped_files: list[str] = field(default_factory=list)
    touched: int = 0
    opened_in: int = 0
    unresolved_ids: dict[int, list[str]] = field(default_factory=dict)
    id_reasons: dict[int, str] = field(default_factory=dict)
    unresolved_slugs: dict[str, list[str]] = field(default_factory=dict)

    def slug_of(self, block: Block) -> str:
        """The slug the block landed on, which is its own unless a live session held it."""
        return self.slugs.get(block.session_id, block.slug)

    @property
    def days(self) -> int:
        return len({b.date for b in self.created})

    @property
    def unresolved(self) -> int:
        return len(self.unresolved_ids) + len(self.unresolved_slugs)

    def to_json(self) -> dict[str, Any]:
        return {
            "created": [{"slug": self.slug_of(b), "title": b.title} for b in self.created],
            "days": self.days,
            "skipped": self.skipped,
            "by_position": [
                {"slug": self.slug_of(b), "heading": b.heading} for b in self.by_position
            ],
            "renumbered": [
                {"session_id": b.session_id, "wanted": b.slug, "slug": slug}
                for b, slug in self.renumbered
            ],
            "skipped_files": self.skipped_files,
            "touched": self.touched,
            "opened_in": self.opened_in,
            "unresolved_ids": {str(k): v for k, v in sorted(self.unresolved_ids.items())},
            "id_reasons": {str(k): v for k, v in sorted(self.id_reasons.items())},
            "unresolved_slugs": dict(sorted(self.unresolved_slugs.items())),
        }

    def summary(self, *, dry_run: bool = False) -> str:
        verbs = ("would create", "link", "skip") if dry_run else ("created", "linked", "skipped")
        return (
            f"{verbs[0]} {len(self.created)} sessions across {self.days} days, "
            f"{verbs[1]} {self.touched} touched / {self.opened_in} opened_in edges, "
            f"{self.unresolved} unresolved targets, {verbs[2]} {len(self.skipped)} existing"
        )

    def markdown(self) -> str:
        lines = ["# kb migrate journal", "", f"## Created ({len(self.created)})"]
        lines += [f"- {self.slug_of(b)}  {b.time:%H:%M}  {b.title}" for b in self.created]
        lines += ["", f"## Skipped, already in the store ({len(self.skipped)})"]
        lines += [f"- {slug}" for slug in self.skipped]
        lines += ["", f"## Numbered by position ({len(self.by_position)})"]
        lines += [f"- {self.slug_of(b)} ← {b.heading!r}" for b in self.by_position]
        lines += ["", f"## Renumbered (live session held the slug) ({len(self.renumbered)})"]
        lines += [f"- {b.session_id}: {b.slug} taken -> {slug}" for b, slug in self.renumbered]
        lines += ["", f"## Skipped files ({len(self.skipped_files)})"]
        lines += [f"- {name}" for name in self.skipped_files]
        lines += ["", f"## Unresolved targets ({self.unresolved})"]
        for rid, slugs in sorted(self.unresolved_ids.items()):
            lines.append(f"- [{rid}] {self.id_reasons.get(rid, 'no record')} ← {_sessions(slugs)}")
        for slug, slugs in sorted(self.unresolved_slugs.items()):
            lines.append(f"- [[{slug}]] ← {_sessions(slugs)}")
        return "\n".join(lines)


def _sessions(slugs: list[str]) -> str:
    noun = "session" if len(slugs) == 1 else "sessions"
    return f"{len(slugs)} {noun}: {', '.join(slugs)}"


@dataclass(frozen=True)
class Target:
    """What the store holds under an id a block mentions.

    ``created_on`` is the local date, not the UTC one: a record created at midnight Manila is
    stored as 16:00 the previous day, which would hand every comparison a free extra day.
    """

    kind: str
    created_on: dt.date


@dataclass
class Plan:
    """The blocks resolved against the store: existing sessions, known targets, present edges."""

    blocks: list[Block]
    existing: dict[str, str]
    slugs: dict[str, str]
    renumbered: list[tuple[Block, str]]
    known: dict[int, Target]
    known_slugs: dict[str, int]
    present: set[Edge]
    skipped_files: list[str]

    def slug_of(self, block: Block) -> str:
        return self.slugs[block.session_id]

    def target_of(self, block: Block, rid: int) -> str | None:
        """Why *rid* cannot be what the block meant, or None when it can.

        A journal ``[NNN]`` was written as a state-item number, so only a task can be one, and
        only a task that already existed: ids the later migrations handed out (nodes, sessions)
        and tasks created after the entry are a different record wearing that number.
        """
        target = self.known.get(rid)
        if target is None:
            return "no record"
        if target.kind != "task":
            return f"not a task ({target.kind})"
        if target.created_on > block.date + ID_GRACE:
            return "created after the block"
        return None

    def edges_of(self, block: Block) -> tuple[list[Edge], list[tuple[int, str]], list[str]]:
        """(new edges the block yields, (unresolved id, why), unresolved slugs)."""
        edges: list[Edge] = []
        bad_ids: list[tuple[int, str]] = []
        sid = block.session_id
        for rid in block.ids:
            reason = self.target_of(block, rid)
            if reason is None:
                edges.append(("TOUCHED", sid, rid))
            else:
                bad_ids.append((rid, reason))
        for rid in block.opened_ids:
            if self.target_of(block, rid) is None:
                edges.append(("OPENED_IN", sid, rid))
        bad_slugs = [slug for slug in block.targets if slug not in self.known_slugs]
        for slug in block.targets:
            if slug in self.known_slugs:
                edges.append(("TOUCHED", sid, self.known_slugs[slug]))
        for slug in block.opened_targets:
            if slug in self.known_slugs:
                edges.append(("OPENED_IN", sid, self.known_slugs[slug]))
        fresh = [e for e in dict.fromkeys(edges) if e not in self.present]
        return fresh, bad_ids, bad_slugs

    def line(self, block: Block) -> str:
        edges, bad_ids, bad_slugs = self.edges_of(block)
        state = "  (exists, skip)" if block.session_id in self.existing else ""
        numbered = "  (numbered by position)" if block.by_position else ""
        moved = "" if self.slug_of(block) == block.slug else f"  (slug taken, was {block.slug})"
        return (
            f"{self.slug_of(block):<14} {block.time:%H:%M}  {len(edges):>3} edges  "
            f"{len(bad_ids) + len(bad_slugs):>2} unresolved  {block.title}{state}{numbered}{moved}"
        )

    def report(self) -> Report:
        """The dry-run report: what ``apply`` would create and link."""
        report = Report(
            skipped_files=list(self.skipped_files),
            slugs=dict(self.slugs),
            renumbered=list(self.renumbered),
        )
        for block in self.blocks:
            if block.session_id in self.existing:
                report.skipped.append(self.slug_of(block))
            else:
                report.created.append(block)
            if block.by_position:
                report.by_position.append(block)
            edges, bad_ids, bad_slugs = self.edges_of(block)
            report.touched += sum(1 for e in edges if e[0] == "TOUCHED")
            report.opened_in += sum(1 for e in edges if e[0] == "OPENED_IN")
            _note_bad(report, self.slug_of(block), bad_ids, bad_slugs)
        return report


def _note_bad(
    report: Report, slug: str, bad_ids: list[tuple[int, str]], bad_slugs: list[str]
) -> None:
    """Add a block's unresolved targets to the report, the first reason per id winning."""
    for rid, reason in bad_ids:
        report.unresolved_ids.setdefault(rid, []).append(slug)
        report.id_reasons.setdefault(rid, reason)
    for target in bad_slugs:
        report.unresolved_slugs.setdefault(target, []).append(slug)


def resolve(
    tx: ManagedTransaction, blocks: list[Block], skipped_files: list[str], cfg: Config
) -> Plan:
    """Which sessions exist, which ids and slugs resolve, and which edges are already there.

    A session is "already imported" by its ``journal:`` session id, never by its slug: a slug
    is a date and a live session of that date holds one too. Such a block keeps its own body
    and edges under the next free number.
    """
    session_ids = [b.session_id for b in blocks]
    ids = sorted(
        {rid for b in blocks for rid in b.ids} | {rid for b in blocks for rid in b.opened_ids}
    )
    targets = sorted(
        {t for b in blocks for t in b.targets} | {t for b in blocks for t in b.opened_targets}
    )
    existing = {
        row["sid"]: row["slug"]
        for row in tx.run(
            "MATCH (s:Record:Session) WHERE s.session_id IN $sids "
            "RETURN s.session_id AS sid, s.slug AS slug",
            sids=session_ids,
        )
    }
    tz = ZoneInfo(cfg.general.timezone)
    known = {
        row["id"]: Target(row["kind"], records.to_py(row["created_at"]).astimezone(tz).date())
        for row in tx.run(
            "MATCH (r:Record) WHERE r.id IN $ids "
            "RETURN r.id AS id, r.kind AS kind, r.created_at AS created_at",
            ids=ids,
        )
    }
    known_slugs = {
        row["slug"]: row["id"]
        for row in tx.run(
            "MATCH (r:Record) WHERE r.slug IN $slugs RETURN r.slug AS slug, r.id AS id",
            slugs=targets,
        )
    }
    slugs, renumbered = _assign_slugs(tx, blocks, existing)
    present: set[Edge] = set()
    if existing:
        rows = tx.run(
            "MATCH (s:Record:Session)-[:TOUCHED]->(r:Record) WHERE s.session_id IN $sids "
            "RETURN 'TOUCHED' AS rel, s.session_id AS sid, r.id AS id "
            "UNION MATCH (r:Record)-[:OPENED_IN]->(s:Record:Session) WHERE s.session_id IN $sids "
            "RETURN 'OPENED_IN' AS rel, s.session_id AS sid, r.id AS id",
            sids=sorted(existing),
        )
        present = {(row["rel"], row["sid"], row["id"]) for row in rows}
    return Plan(blocks, existing, slugs, renumbered, known, known_slugs, present, skipped_files)


def _assign_slugs(
    tx: ManagedTransaction, blocks: list[Block], existing: dict[str, str]
) -> tuple[dict[str, str], list[tuple[Block, str]]]:
    """The slug each block lands on: its own, or the next free number of that date."""
    taken: set[str] = set()
    for day in sorted({b.date for b in blocks}):
        taken |= records.unique_slug_taken(tx, f"{day.isoformat()}-")
    slugs: dict[str, str] = {}
    renumbered: list[tuple[Block, str]] = []
    for block in blocks:
        if block.session_id in existing:  # imported before: it keeps the slug it got then
            slugs[block.session_id] = existing[block.session_id]
            continue
        slug, n = block.slug, block.n
        while slug in taken or slug in slugs.values():
            n += 1
            slug = f"{block.date.isoformat()}-{n}"
        if slug != block.slug:
            renumbered.append((block, slug))
        slugs[block.session_id] = slug
    return slugs, renumbered


def apply(tx: ManagedTransaction, plan: Plan, *, scope: str, session: str | None) -> Report:
    """Create the new sessions, then link every block's targets; imported ones are skipped.

    A block is "already imported" by its ``journal:`` session id, so a live session holding
    its slug renumbers it instead of swallowing its edges. The link pass covers skipped
    sessions too, so a re-run after the tasks were migrated adds the edges; edges are MERGEd
    and counted only when new.
    """
    report = Report(
        skipped_files=list(plan.skipped_files),
        slugs=dict(plan.slugs),
        renumbered=list(plan.renumbered),
    )
    by_session: dict[str, records.Record] = {}
    for block in plan.blocks:
        if block.by_position:
            report.by_position.append(block)
        if block.session_id in plan.existing:
            report.skipped.append(plan.slug_of(block))
            continue
        rec = records.create(
            tx,
            kind="session",
            scope=scope,
            title=block.title,
            at=block.at,
            body=block.body,
            slug=plan.slug_of(block),
            tags=[*block.tags, MIGRATED_TAG],
            source="migrated",
            session=session,
            created_at=block.at,
            note=f"migrated from journal/{block.file}",
            extra={
                "session_id": block.session_id,
                "opened_at": block.at,
                "closed_at": block.at,
                "no_summary": False,
            },
        )
        records.add_event(tx, rec.id, "opened", block.at, session=session)
        records.add_event(tx, rec.id, "closed", block.at, session=session)
        by_session[block.session_id] = rec
        report.created.append(block)
    for block in plan.blocks:
        rec = by_session.get(block.session_id) or records.session_by_id(tx, block.session_id)
        edges, bad_ids, bad_slugs = plan.edges_of(block)
        _note_bad(report, plan.slug_of(block), bad_ids, bad_slugs)
        for rel, _, target in edges:
            if records.attach(tx, rec, target, rel, block.at):
                if rel == "TOUCHED":
                    report.touched += 1
                else:
                    report.opened_in += 1
    return report
