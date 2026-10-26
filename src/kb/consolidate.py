"""kb consolidate: proposals to merge, condense and archive knowledge records. Writes nothing.

Size, not count, is what accumulates (measured 2026-10-22: median 3.2 KB, 15 records over
10 KB): an append-only record grows a dated paragraph per finding until reading it costs
more than it returns. The proposals go to a person, who ticks them; the commands that apply
them (merge, edit, archive) keep the history in events, so condensing loses nothing.

Notes, references, feedback and howtos are merged, condensed and archived. A decision is
never rewritten; it is proposed for archive only once superseded. Tasks and sessions are
never touched.
"""

import datetime as dt
import re
from dataclasses import dataclass
from typing import Any

from neo4j import ManagedTransaction

from kb import similar, tags
from kb.calendar import local_date
from kb.config import Config
from kb.records import Record, pin_rank

KINDS = ("note", "reference", "feedback", "howto")
ARCHIVE_KINDS = (*KINDS, "decision")
CONDENSE_BYTES = 10_000
# a note's tasks closed at least this long ago, and the note untouched since: done with
CLOSED_DAYS = 14
DATED = re.compile(r"^\*\*(\d{4}-\d{2}-\d{2}):\*\*", re.MULTILINE)
WORK_LINKS = "RELATED|PART_OF|BLOCKS"


@dataclass
class Proposal:
    """One action on one group of records, with why and how."""

    action: str  # merge | condense | archive
    records: list[Record]
    reason: str
    commands: list[str]
    keep: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "records": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "scope": r.scope,
                    "slug": r.slug,
                    "title": r.title,
                    "tags": r.tags,
                    "bytes": len(r.body.encode()),
                }
                for r in self.records
            ],
            "keep": self.keep,
            "reason": self.reason,
            "commands": self.commands,
        }


def kilobytes(size: int) -> str:
    return f"{size / 1000:.1f} KB"


def _knowledge(tx: ManagedTransaction, scopes: list[str] | None) -> list[Record]:
    rows = tx.run(
        "MATCH (r:Record) WHERE r.kind IN $kinds AND r.archived_at IS NULL "
        "AND ($scopes IS NULL OR r.scope IN $scopes) RETURN r ORDER BY r.id",
        kinds=list(ARCHIVE_KINDS),
        scopes=scopes,
    )
    return [Record.from_node(row["r"]) for row in rows]


def _superseded(tx: ManagedTransaction, ids: list[int]) -> dict[int, Record]:
    """``{old id: the live record that supersedes it}``."""
    rows = tx.run(
        "MATCH (n:Record)-[:SUPERSEDES]->(o:Record) WHERE o.id IN $ids AND n.archived_at IS NULL "
        "RETURN o.id AS old, n ORDER BY n.id",
        ids=ids,
    )
    return {row["old"]: Record.from_node(row["n"]) for row in rows}


def _linked_tasks(tx: ManagedTransaction, ids: list[int]) -> dict[int, list[Record]]:
    rows = tx.run(
        f"MATCH (r:Record)-[:{WORK_LINKS}]-(t:Record:Task) WHERE r.id IN $ids "
        "RETURN DISTINCT r.id AS id, t ORDER BY t.id",
        ids=ids,
    )
    out: dict[int, list[Record]] = {}
    for row in rows:
        out.setdefault(row["id"], []).append(Record.from_node(row["t"]))
    return out


def _knowledge_links(tx: ManagedTransaction, ids: list[int]) -> dict[int, int]:
    """``{id: live knowledge records it is linked to}``: what other knowledge still points at."""
    rows = tx.run(
        f"MATCH (r:Record)-[:{WORK_LINKS}|SUPERSEDES]-(k:Record) WHERE r.id IN $ids "
        "AND k.kind IN $kinds AND k.archived_at IS NULL "
        "RETURN r.id AS id, count(DISTINCT k) AS n",
        ids=ids,
        kinds=list(ARCHIVE_KINDS),
    )
    return {row["id"]: row["n"] for row in rows}


def _keep(group: list[Record]) -> Record:
    """Critical first, then pinned, then the longest body, then the oldest."""
    return min(group, key=lambda r: (pin_rank(r), -len(r.body), r.id))


def _merges(
    corpus: similar.Corpus, recs: list[Record], areas: dict[int, frozenset[str]]
) -> list[Proposal]:
    """Groups of overlapping records whose area tags are the same set (no tags counts too)."""
    parent = {r.id: r.id for r in recs}

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    best: dict[frozenset[int], float] = {}
    for a, b, score in similar.pairs(corpus, recs):
        if areas[a.id] != areas[b.id]:
            continue
        best[frozenset((a.id, b.id))] = score
        parent[root(a.id)] = root(b.id)
    groups: dict[int, list[Record]] = {}
    for rec in recs:
        if any(rec.id in pair for pair in best):
            groups.setdefault(root(rec.id), []).append(rec)
    out = []
    for group in sorted(groups.values(), key=lambda g: g[0].id):
        keep = _keep(group)
        shared = sorted(areas[keep.id])
        scores = ", ".join(
            f"[{min(p)}]~[{max(p)}] {s:.2f}"
            for p, s in sorted(best.items(), key=lambda kv: -kv[1])
            if p <= {r.id for r in group}
        )
        where = f"same areas: {', '.join(shared)}" if shared else "no area tags on any of them"
        dups = [r for r in group if r.id != keep.id]
        out.append(
            Proposal(
                "merge",
                group,
                f"{where}; title and summary similarity {scores}; keep [{keep.id}]",
                [
                    f"kb edit {keep.id} --body-file <merged.md>",
                    *(f"kb merge {d.id} {keep.id}" for d in dups),
                ],
                keep=keep.id,
            )
        )
    return out


def _condense(recs: list[Record], skip: set[int]) -> list[Proposal]:
    out = []
    for rec in recs:
        size = len(rec.body.encode())
        if rec.kind not in KINDS or rec.id in skip or size < CONDENSE_BYTES:
            continue
        dates = DATED.findall(rec.body)
        dated = (
            f", {len(dates)} dated paragraphs ({min(dates)} … {max(dates)})"
            if dates
            else ", no dated paragraphs"
        )
        out.append(
            Proposal(
                "condense",
                [rec],
                f"body {kilobytes(size)}{dated}; the edited event keeps the full text",
                [f"kb edit {rec.id} --body-file <condensed.md>"],
            )
        )
    return sorted(out, key=lambda p: -len(p.records[0].body.encode()))


def _archives(
    recs: list[Record],
    superseded: dict[int, Record],
    linked: dict[int, list[Record]],
    cited: dict[int, int],
    *,
    today: dt.date,
    cfg: Config,
) -> list[Proposal]:
    """Superseded records, and notes of finished work: every task the note is linked to closed
    CLOSED_DAYS ago or more, the note unchanged since, and no live knowledge record linked to
    it (one that is still cited is still in use, whatever its tasks did)."""
    out = []
    for rec in recs:
        newer = superseded.get(rec.id)
        if newer is not None:
            out.append(
                Proposal(
                    "archive",
                    [rec],
                    f"superseded by [{newer.id}] {newer.title}",
                    [f'kb archive {rec.id} --reason "superseded by [{newer.id}]"'],
                )
            )
            continue
        work = linked.get(rec.id, [])
        if rec.kind != "note" or rec.pinned or not work or cited.get(rec.id, 0):
            continue
        closed = [t.done_at or t.archived_at for t in work]
        if any(c is None for c in closed):
            continue
        last = max(c for c in closed if c is not None)
        if (today - local_date(last, cfg)).days < CLOSED_DAYS or (
            rec.updated_at is not None and rec.updated_at > last
        ):
            continue
        ids = ", ".join(f"[{t.id}]" for t in work)
        out.append(
            Proposal(
                "archive",
                [rec],
                f"every task it is linked to is closed ({ids}, the last on "
                f"{local_date(last, cfg)}) "
                "and it has not changed since",
                [f'kb archive {rec.id} --reason "its tasks are closed: {ids}"'],
            )
        )
    return out


def propose(
    tx: ManagedTransaction, cfg: Config, scopes: list[str] | None, today: dt.date
) -> list[Proposal]:
    """Merge, condense and archive proposals over the knowledge records in the view."""
    recs = _knowledge(tx, scopes)
    ids = [r.id for r in recs]
    vocabularies: dict[str, tags.Vocabulary] = {}
    areas: dict[int, frozenset[str]] = {}
    for rec in recs:
        if rec.scope not in vocabularies:
            vocabularies[rec.scope] = tags.load(cfg, rec.scope)
        areas[rec.id] = frozenset(vocabularies[rec.scope].area_tags(rec.tags))
    archives = _archives(
        recs,
        _superseded(tx, ids),
        _linked_tasks(tx, ids),
        _knowledge_links(tx, ids),
        today=today,
        cfg=cfg,
    )
    gone = {p.records[0].id for p in archives}
    mergeable = [r for r in recs if r.kind in KINDS and r.id not in gone]
    merges = _merges(similar.Corpus.load(tx), mergeable, areas)
    merged = {r.id for p in merges for r in p.records if r.id != p.keep}
    return [*merges, *_condense(recs, gone | merged), *archives]
