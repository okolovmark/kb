"""Similar records: the duplicate check of ``kb add`` and ``kb sync odoo``, and the merge
proposals of ``kb consolidate``.

Lucene finds the candidates: the new title and summary, escaped, against ``record_text``.
Lucene's score cannot decide on its own, because it grows with the length of the query: a
long title scores higher against an unrelated record than a short one against its own
duplicate. The decision is a cosine of the two records' title and summary words, each word
weighted by its rarity among all records (IDF), so a shared ticket key or module name counts
and a shared "the" or "odoo" does not. Calibrated on the store, 2026-10-22: docs/adr/0006.
"""

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from neo4j import ManagedTransaction

from kb.db import FULLTEXT_INDEX
from kb.records import KNOWLEDGE_KINDS, Record, lucene_escape

THRESHOLD = 0.40
CANDIDATES = 20
SHOWN = 3
# StandardTokenizer splits on everything that is not a letter or a digit, and so does this
WORD = re.compile(r"[^\W_]+")


@dataclass(frozen=True)
class Similar:
    """A record close enough to count as a duplicate, with its cosine."""

    record: Record
    similarity: float

    def to_json(self) -> dict[str, object]:
        rec = self.record
        return {
            "id": rec.id,
            "kind": rec.kind,
            "scope": rec.scope,
            "slug": rec.slug,
            "title": rec.title,
            "similarity": round(self.similarity, 3),
        }


def words(text: str) -> set[str]:
    return set(WORD.findall(text.lower()))


def family(kind: str) -> tuple[str, ...]:
    """The kinds a record of *kind* can duplicate: a task another task, knowledge knowledge.

    A note on the same subject as a task is its context, not its duplicate.
    """
    return ("task",) if kind == "task" else KNOWLEDGE_KINDS


class Corpus:
    """Word document frequencies over every record's title and summary, sessions left out."""

    def __init__(self, texts: Iterable[str]) -> None:
        self.df: Counter[str] = Counter()
        self.size = 0
        for text in texts:
            self.df.update(words(text))
            self.size += 1

    @classmethod
    def load(cls, tx: ManagedTransaction) -> Corpus:
        rows = tx.run(
            "MATCH (r:Record) WHERE r.kind <> 'session' "
            "RETURN r.title AS title, r.summary AS summary"
        )
        return cls(f"{row['title']} {row['summary'] or ''}" for row in rows)

    def weight(self, word: str) -> float:
        """BM25's idf: rare words weigh more, a word in every record next to nothing."""
        n = self.df[word]
        return math.log(1 + (self.size - n + 0.5) / (n + 0.5))

    def cosine(self, a: set[str], b: set[str]) -> float:
        norm = math.sqrt(sum(self.weight(w) ** 2 for w in a) * sum(self.weight(w) ** 2 for w in b))
        if not norm:
            return 0.0
        return sum(self.weight(w) ** 2 for w in a & b) / norm


def text_of(rec: Record) -> str:
    return f"{rec.title} {rec.summary or ''}"


def find(
    tx: ManagedTransaction,
    corpus: Corpus,
    *,
    title: str,
    summary: str | None,
    kind: str,
    scopes: list[str] | None,
    exclude: Iterable[int] = (),
    no_odoo: bool = False,
    limit: int = SHOWN,
) -> list[Similar]:
    """Open records of *kind*'s family in the view *scopes* (None = every scope) whose title
    and summary reach THRESHOLD against *title* and *summary*, most similar first.

    Open means not archived and, for a task, not done. *no_odoo* leaves out records with
    ``source = odoo`` (the sync compares a new ticket with the threads not yet promoted).
    """
    text = f"{title} {summary or ''}"
    mine = words(text)
    if not mine:
        return []
    rows = tx.run(
        f"CALL db.index.fulltext.queryNodes('{FULLTEXT_INDEX}', $q) YIELD node, score "
        "WHERE node.kind IN $kinds AND NOT node.id IN $exclude AND node.archived_at IS NULL "
        "AND (node.kind <> 'task' OR node.done_at IS NULL) "
        "AND ($scopes IS NULL OR node.scope IN $scopes) "
        "AND (NOT $no_odoo OR coalesce(node.source, 'manual') <> 'odoo') "
        "RETURN node LIMIT $candidates",
        q=lucene_escape(text),
        kinds=list(family(kind)),
        exclude=list(exclude),
        scopes=scopes,
        no_odoo=no_odoo,
        candidates=CANDIDATES,
    )
    found = []
    for row in rows:
        rec = Record.from_node(row["node"])
        similarity = corpus.cosine(mine, words(text_of(rec)))
        if similarity >= THRESHOLD:
            found.append(Similar(rec, similarity))
    found.sort(key=lambda s: (-s.similarity, s.record.id))
    return found[:limit]


def pairs(corpus: Corpus, recs: list[Record]) -> list[tuple[Record, Record, float]]:
    """Every pair of *recs* at THRESHOLD or above, the most similar first (kb consolidate)."""
    bags = [(rec, words(text_of(rec))) for rec in recs]
    out = []
    for i, (a, wa) in enumerate(bags):
        for b, wb in bags[i + 1 :]:
            similarity = corpus.cosine(wa, wb)
            if similarity >= THRESHOLD:
                out.append((a, b, similarity))
    out.sort(key=lambda p: (-p[2], p[0].id, p[1].id))
    return out
