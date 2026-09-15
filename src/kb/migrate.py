"""kb migrate state: the old state.md items become Task records with their ids kept."""

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any

from neo4j import ManagedTransaction

from kb import records
from kb.calendar import day_start_utc
from kb.config import Config

SECTION_TAGS = {
    "Ship blockers": "ship_blocker",
    "Blocked": "blocked",
    "Ready for review": "ready_for_review",
    "WIP": "wip",
    "Open threads": "open_thread",
}
TITLE_MAX = 100
OPEN_LINE = re.compile(r"^- \[(\d+)\] (\d{4}-\d{2}-\d{2}): (.*)$")
CLOSED_COMMENT = re.compile(
    r"^<!--\s*\[(\d+)\] CLOSED (\d{4}-\d{2}-\d{2}): (.*?)\s*-->$", re.DOTALL
)
JOURNAL_REF = re.compile(r"[Ss]ee journal/(\d{4}-\d{2}-\d{2})")
ODOO_TAG = re.compile(r"\s*\[odoo:(\d+)\]\s*$")
LEADING_ID_DATE = re.compile(r"^(?:\[\d+\]\s+)?(?:\d{4}-\d{2}-\d{2}:\s+)+")


@dataclass(frozen=True)
class Item:
    """One state.md item, open or closed."""

    id: int
    date: dt.date
    text: str
    tag: str
    closed_on: dt.date | None = None

    @property
    def journal_ref(self) -> str | None:
        match = JOURNAL_REF.search(self.text)
        return f"journal/{match.group(1)}" if match else None

    @property
    def external_id(self) -> str | None:
        match = ODOO_TAG.search(self.text)
        return match.group(1) if match else None

    @property
    def title(self) -> str:
        """Text up to the first `` — ``, bold markers and the odoo tag dropped, 100 chars."""
        text = ODOO_TAG.sub("", self.text)
        text = LEADING_ID_DATE.sub("", text)
        text = text.split(" — ", 1)[0].replace("**", "")
        text = " ".join(text.split())
        return text[:TITLE_MAX].rstrip() or f"state item {self.id}"


def parse(text: str) -> list[Item]:
    """Items of a state.md: open lines and closed comments under the five known sections."""
    items: list[Item] = []
    tag: str | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if line.startswith("## "):
            tag = SECTION_TAGS.get(line[3:].strip())
            continue
        if tag is None:
            continue
        if line.startswith("<!--"):
            block = [line]
            while not block[-1].rstrip().endswith("-->") and i < len(lines):
                block.append(lines[i])
                i += 1
            match = CLOSED_COMMENT.match("\n".join(block).strip())
            if match:
                items.append(_item(match, tag, closed=True))
            continue
        match = OPEN_LINE.match(line)
        if match:
            items.append(_item(match, tag, closed=False))
    return items


def _item(match: re.Match[str], tag: str, *, closed: bool) -> Item:
    item_id, date, text = int(match.group(1)), dt.date.fromisoformat(match.group(2)), match.group(3)
    text = " ".join(text.split())
    if not closed:
        return Item(item_id, date, text, tag)
    # a closed comment keeps the CLOSED date first; the item's own date is the one inside TEXT
    inner = re.match(r"^(?:\[\d+\]\s+)?(\d{4}-\d{2}-\d{2}):\s+", text)
    opened = dt.date.fromisoformat(inner.group(1)) if inner else date
    return Item(item_id, opened, text, tag, closed_on=date)


@dataclass
class Report:
    """What a migration run did (or would do under --dry-run)."""

    created: list[int] = field(default_factory=list)
    closed: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)
    counter_set_to: int | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "created": self.created,
            "closed": self.closed,
            "skipped": self.skipped,
            "counter_set_to": self.counter_set_to,
        }

    def summary(self) -> str:
        line = (
            f"created {len(self.created)} tasks ({len(self.closed)} closed), "
            f"skipped {len(self.skipped)} existing"
        )
        if self.skipped:
            line += f": {', '.join(str(i) for i in self.skipped)}"
        if self.counter_set_to is not None:
            line += f"; counter set to {self.counter_set_to}"
        return line


def plan_line(item: Item) -> str:
    state = f"closed {item.closed_on}" if item.closed_on else "open"
    extra = f" [odoo:{item.external_id}]" if item.external_id else ""
    return f"[{item.id}] {item.date} {item.tag:<16} {state:<17} {item.title}{extra}"


def check_counter(tx: ManagedTransaction, items: list[Item], counter: int | None) -> None:
    """``--counter N`` must not fall below the highest item id, the current Counter value or
    the highest existing record id; below any of them the next ``kb add`` would collide."""
    if counter is None:
        return
    top = max((i.id for i in items), default=0)
    current = records.counter_value(tx)
    highest = records.max_record_id(tx)
    if counter < max(top, current, highest):
        raise records.RecordError(
            f"--counter {counter} is below the floor: highest item id {top}, "
            f"Counter {current}, highest record id {highest}"
        )


def apply(
    tx: ManagedTransaction,
    items: list[Item],
    scope: str,
    cfg: Config,
    *,
    session: str | None,
    counter: int | None = None,
) -> Report:
    """Create one Task per item with its original id; existing ids are skipped and reported.

    The Counter is left alone for ids at or below its value; an item id above it raises the
    Counter to that id so the next ``kb add`` cannot collide with a migrated record.
    ``counter`` sets it to an explicit value (the old state_counter may be ahead of state.md).
    """
    check_counter(tx, items, counter)
    report = Report()
    for item in items:
        if records.exists(tx, item.id):
            report.skipped.append(item.id)
            continue
        opened_at = day_start_utc(item.date, cfg)
        rec = records.create(
            tx,
            kind="task",
            scope=scope,
            title=item.title,
            at=opened_at,
            body=item.text,
            tags=[item.tag],
            source="odoo" if item.external_id else "manual",
            external_id=item.external_id,
            journal_ref=item.journal_ref,
            schedule={"sched": "age", "anchor": item.date, "days": "work"},
            session=session,
            record_id=item.id,
            created_at=opened_at,
        )
        report.created.append(rec.id)
        if item.closed_on:
            closed_at = day_start_utc(item.closed_on, cfg)
            records.add_event(tx, rec.id, "done", closed_at, session=session)
            records.touch(tx, rec.id, closed_at, done_at=closed_at)
            report.closed.append(rec.id)
    if counter is not None:
        records.set_counter(tx, counter)
        report.counter_set_to = counter
    elif report.created:
        top = max(report.created)
        if top > records.counter_value(tx):
            records.raise_counter(tx, top)
            report.counter_set_to = top
    return report
