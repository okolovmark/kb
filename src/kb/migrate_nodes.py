"""kb migrate nodes: markdown memory nodes become records, their wikilinks RELATED edges."""

import datetime as dt
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from neo4j import ManagedTransaction

from kb import records
from kb.calendar import day_start_utc
from kb.config import Config

FRONTMATTER = re.compile(r"\A---[ \t]*\n(.*?)\n---[ \t]*\n?", re.DOTALL)
FENCE = re.compile(r"^[ \t]*(`{3,}|~{3,}).*?^[ \t]*\1[ \t]*$", re.MULTILINE | re.DOTALL)
INLINE_CODE = re.compile(r"`+[^`\n]*`+")
LEADING_BLANK = re.compile(r"\A(?:[ \t]*\n)+")
HEADING = re.compile(r"^# +(.+?)\s*$")
WIKILINK = re.compile(r"\[\[([A-Za-z0-9][A-Za-z0-9_-]*)\]\]")
NODE_LINK = re.compile(r"\]\((?:\.\./)*nodes/([A-Za-z0-9][A-Za-z0-9_-]*)\.md(?:#[^)]*)?\)")
KIND_BY_TYPE = {"feedback": "feedback", "user": "note", "project": "note", "reference": "reference"}
PINNED_TYPES = frozenset({"feedback", "user"})
HOWTO_PREFIX = "conventions_"
TYPE_PREFIXES = ("feedback_", "reference_", "project_", "conventions_", "user_")
MIGRATED_TAG = "migrated"
DATE_LEN = 10


# --- frontmatter ------------------------------------------------------------------------


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _scalar(value: str) -> str:
    """A quoted or bare YAML scalar; ``\\"`` and ``\\\\`` are the only escapes handled."""
    if len(value) >= 2 and value[0] == value[-1] == '"':  # noqa: PLR2004
        return re.sub(r'\\(["\\])', r"\1", value[1:-1])
    if len(value) >= 2 and value[0] == value[-1] == "'":  # noqa: PLR2004
        return value[1:-1].replace("''", "'")
    return value


def _next_content(lines: list[str], start: int) -> int:
    i = start
    while i < len(lines) and (not lines[i].strip() or lines[i].lstrip().startswith("#")):
        i += 1
    return i


def _parse_list(lines: list[str], start: int, indent: int) -> tuple[list[str], int]:
    items: list[str] = []
    i = _next_content(lines, start)
    while i < len(lines):
        stripped = lines[i].strip()
        if _indent(lines[i]) != indent or not stripped.startswith("- "):
            break
        items.append(_scalar(stripped[2:].strip()))
        i = _next_content(lines, i + 1)
    return items, i


def _parse_mapping(lines: list[str], start: int, indent: int) -> tuple[dict[str, Any], int]:
    out: dict[str, Any] = {}
    i = _next_content(lines, start)
    while i < len(lines):
        line = lines[i]
        if _indent(line) < indent:
            break
        if _indent(line) > indent or line.strip().startswith("- "):
            i = _next_content(lines, i + 1)  # a stray deeper line without a key
            continue
        key, sep, value = line.strip().partition(":")
        i = _next_content(lines, i + 1)
        if not sep:
            continue
        key, value = key.strip(), value.strip()
        if value:
            out[key] = _scalar(value)
            continue
        child = _indent(lines[i]) if i < len(lines) else 0
        if child <= indent:
            out[key] = None
        elif lines[i].strip().startswith("- "):
            out[key], i = _parse_list(lines, i, child)
        else:
            out[key], i = _parse_mapping(lines, i, child)
    return out, i


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str] | None:
    """(mapping, body) for a leading ``---`` block; None when the file has none.

    A YAML subset, not a YAML parser: ``key: value`` lines, nested mappings by indentation,
    ``- item`` lists, single- and double-quoted scalars. Enough for the memory nodes.
    """
    match = FRONTMATTER.match(text)
    if match is None:
        return None
    data, _ = _parse_mapping(match.group(1).splitlines(), 0, 0)
    return data, text[match.end() :]


# --- one node ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Node:
    """One parsed node file, before the store is consulted."""

    file: str
    slug: str
    dated_now: bool
    node_type: str | None
    kind: str
    pinned: bool
    title: str
    summary: str | None
    body: str
    tags: list[str]
    created_at: dt.datetime
    updated_at: dt.datetime
    targets: tuple[str, ...]


def split_title(body: str) -> tuple[str | None, str]:
    """The leading ``# `` heading (blank lines before it allowed) and the body without it."""
    lines = body.split("\n")
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    match = HEADING.match(lines[i]) if i < len(lines) else None
    if match is None:
        return None, LEADING_BLANK.sub("", body)
    return match.group(1), LEADING_BLANK.sub("", "\n".join(lines[i + 1 :]))


def title_from_slug(slug: str) -> str:
    """``feedback_attach_prod_scripts`` -> ``Attach prod scripts``; the slug itself when empty."""
    rest = slug
    for prefix in TYPE_PREFIXES:
        if slug.startswith(prefix):
            rest = slug[len(prefix) :]
            break
    words = " ".join(rest.split("_"))
    return (words[:1].upper() + words[1:]) if words else slug


def strip_code(body: str) -> str:
    """The body without fenced blocks and inline code spans: examples are not links."""
    return INLINE_CODE.sub("", FENCE.sub("", body))


def link_targets(body: str, slug: str) -> tuple[str, ...]:
    """Distinct ``[[slug]]`` and ``](nodes/slug.md)`` targets in file order, self excluded;
    code blocks and spans do not count."""
    prose = strip_code(body)
    found = [m.group(1) for m in WIKILINK.finditer(prose)]
    found += [m.group(1) for m in NODE_LINK.finditer(prose)]
    return tuple(t for t in dict.fromkeys(found) if t != slug)


def stamp(value: Any, cfg: Config) -> dt.datetime | None:
    """A ``YYYY-MM-DD`` (midnight in the kb timezone) or ISO datetime as a UTC instant."""
    text = str(value).strip() if value is not None else ""
    if not text:
        return None
    try:
        if len(text) == DATE_LEN:
            return day_start_utc(dt.date.fromisoformat(text), cfg)
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(cfg.general.timezone))
    return parsed.astimezone(dt.UTC)


def parse_node(path: Path, cfg: Config, now: dt.datetime) -> Node | None:
    """The record a node file describes; None when the file has no frontmatter block."""
    parsed = parse_frontmatter(path.read_text())
    if parsed is None:
        return None
    front, rest = parsed
    meta = front.get("metadata") if isinstance(front.get("metadata"), dict) else {}

    def get(name: str) -> Any:
        # some files carry the metadata keys at the top level
        return meta.get(name) if meta.get(name) is not None else front.get(name)

    slug = str(front.get("name") or path.stem).strip()
    node_type = str(get("type")).strip() if get("type") else None
    kind = "howto" if slug.startswith(HOWTO_PREFIX) else KIND_BY_TYPE.get(node_type or "", "note")
    heading, body = split_title(rest)
    description = front.get("description")
    created = stamp(get("created"), cfg) or stamp(get("modified"), cfg)
    updated = stamp(get("updated"), cfg) or stamp(get("modified"), cfg) or created or now
    return Node(
        file=path.name,
        slug=slug,
        dated_now=created is None,
        node_type=node_type,
        kind=kind,
        pinned=node_type in PINNED_TYPES,
        title=(heading or title_from_slug(slug))[: records.TITLE_MAX],
        summary=records.check_summary(str(description)) if description else None,
        body=body,
        tags=[*([node_type] if node_type else []), MIGRATED_TAG],
        created_at=created or now,
        updated_at=updated,
        targets=link_targets(body, slug),
    )


def load_dir(directory: Path, cfg: Config, now: dt.datetime) -> tuple[list[Node], list[str]]:
    """Every ``*.md`` of the directory: (parsed nodes, names of files without frontmatter)."""
    nodes: list[Node] = []
    skipped: list[str] = []
    for path in sorted(directory.glob("*.md")):
        node = parse_node(path, cfg, now)
        if node is None:
            skipped.append(path.name)
        else:
            nodes.append(node)
    dupes = sorted(slug for slug, n in Counter(n.slug for n in nodes).items() if n > 1)
    if dupes:
        raise records.RecordError(f"duplicate slug in {directory}: {', '.join(dupes)}")
    # the store slugifies on create; a slug that would change could never be found again
    odd = [n for n in nodes if records.slugify(n.slug) != n.slug or n.slug.isdigit()]
    if odd:
        first = odd[0]
        raise records.RecordError(
            f"{first.file}: slug {first.slug!r} is not in slug form "
            f"({records.slugify(first.slug)!r}); rename it first ({len(odd)} such files)"
        )
    return nodes, skipped


def read_scope_file(path: Path) -> dict[str, str]:
    """``slug<whitespace>scope`` lines; ``#`` starts a comment; blank lines allowed."""
    out: dict[str, str] = {}
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        parts = raw.split("#", 1)[0].split()
        if not parts:
            continue
        if len(parts) != 2:  # noqa: PLR2004
            raise records.RecordError(f"{path}:{n}: expected 'slug scope', got {raw.strip()!r}")
        if parts[0] in out:
            raise records.RecordError(f"{path}:{n}: {parts[0]} listed twice")
        out[parts[0]] = parts[1]
    return out


# --- against the store ------------------------------------------------------------------


@dataclass
class Report:
    """What a run did, or would do under --dry-run."""

    created: list[Node] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    no_frontmatter: list[str] = field(default_factory=list)
    linked: int = 0
    unresolved: dict[str, list[str]] = field(default_factory=dict)

    @property
    def dated_now(self) -> list[str]:
        """Created files with no created/updated/modified stamp: both dates are the run's."""
        return [n.file for n in self.created if n.dated_now]

    def to_json(self) -> dict[str, Any]:
        return {
            "created": [{"slug": n.slug, "kind": n.kind} for n in self.created],
            "skipped": self.skipped,
            "no_frontmatter": self.no_frontmatter,
            "dated_now": self.dated_now,
            "linked": self.linked,
            "unresolved": self.unresolved,
        }

    def summary(self, *, dry_run: bool = False) -> str:
        notes = Counter(n.node_type for n in self.created if n.kind == "note")
        kinds = Counter(n.kind for n in self.created)
        counts = (
            f"{kinds['feedback']} feedback, {notes['user']} user notes, "
            f"{notes['project']} project notes, {kinds['reference']} references, "
            f"{kinds['howto']} howtos"
        )
        verbs = ("would create", "skip", "link") if dry_run else ("created", "skipped", "linked")
        return (
            f"{verbs[0]} {len(self.created)} records ({counts}), "
            f"{verbs[1]} {len(self.skipped)} existing, {verbs[2]} {self.linked} edges, "
            f"{len(self.unresolved)} unresolved link targets"
        )

    def markdown(self) -> str:
        lines = ["# kb migrate nodes", "", f"## Created ({len(self.created)})"]
        lines += [f"- {n.slug}  {n.kind}{'  pinned' if n.pinned else ''}" for n in self.created]
        lines += ["", f"## Skipped, already in the store ({len(self.skipped)})"]
        lines += [f"- {slug}" for slug in self.skipped]
        lines += ["", f"## Skipped, no frontmatter ({len(self.no_frontmatter)})"]
        lines += [f"- {name}" for name in self.no_frontmatter]
        lines += ["", f"## Dated now, no date in the frontmatter ({len(self.dated_now)})"]
        lines += [f"- {name}" for name in self.dated_now]
        lines += ["", f"## Unresolved link targets ({len(self.unresolved)})"]
        for target, files in sorted(self.unresolved.items()):
            noun = "file" if len(files) == 1 else "files"
            lines.append(f"- {target} ← {len(files)} {noun}: {', '.join(files)}")
        return "\n".join(lines)


@dataclass
class Plan:
    """The nodes resolved against the store: which are new, and which link targets exist."""

    nodes: list[Node]
    scopes: dict[str, str]
    existing: set[str]
    resolvable: set[str]
    no_frontmatter: list[str]

    def links_of(self, node: Node) -> list[str]:
        return [t for t in node.targets if t in self.resolvable]

    def line(self, node: Node) -> str:
        pinned = "pinned" if node.pinned else "-"
        state = "  (exists, skip)" if node.slug in self.existing else ""
        return (
            f"{node.slug:<50} {node.kind:<9} {self.scopes[node.slug]:<12} {pinned:<6} "
            f"{len(self.links_of(node)):>2} links  {node.title}{state}"
        )

    def report(self) -> Report:
        """The dry-run report: every new node created, each resolvable pair linked once."""
        report = Report(no_frontmatter=list(self.no_frontmatter))
        pairs: set[frozenset[str]] = set()
        for node in self.nodes:
            if node.slug in self.existing:
                report.skipped.append(node.slug)
            else:
                report.created.append(node)
            for target in node.targets:
                if target in self.resolvable:
                    pairs.add(frozenset((node.slug, target)))
                else:
                    report.unresolved.setdefault(target, []).append(node.file)
        report.linked = len(pairs)
        return report


def existing_slugs(tx: ManagedTransaction, slugs: list[str]) -> set[str]:
    rows = tx.run("MATCH (r:Record) WHERE r.slug IN $slugs RETURN r.slug AS slug", slugs=slugs)
    return {row["slug"] for row in rows}


def resolve(
    tx: ManagedTransaction, nodes: list[Node], scopes: dict[str, str], no_frontmatter: list[str]
) -> Plan:
    """Which slugs already have a record, and which link targets will resolve after the run."""
    slugs = [n.slug for n in nodes]
    targets = sorted({t for n in nodes for t in n.targets} - set(slugs))
    existing = existing_slugs(tx, slugs)
    resolvable = set(slugs) | existing_slugs(tx, targets)
    return Plan(nodes, scopes, existing, resolvable, no_frontmatter)


def _related(tx: ManagedTransaction, a: records.Record, b: records.Record) -> bool:
    """RELATED is symmetric: one edge per pair, whichever side was migrated first."""
    row = tx.run(
        "MATCH (:Record {id: $a})-[:RELATED]-(:Record {id: $b}) RETURN 1 LIMIT 1", a=a.id, b=b.id
    ).single()
    return row is not None


def apply(tx: ManagedTransaction, plan: Plan, *, session: str | None, at: dt.datetime) -> Report:
    """Create the new records, then link every resolvable target; existing slugs are skipped.

    The link pass covers skipped records too, so a re-run after a missing target file
    appeared adds the edge; edges are idempotent and a ``linked`` event marks only new ones.
    """
    report = Report(no_frontmatter=list(plan.no_frontmatter))
    by_slug: dict[str, records.Record] = {}
    for node in plan.nodes:
        if node.slug in plan.existing:
            report.skipped.append(node.slug)
            continue
        rec = records.create(
            tx,
            kind=node.kind,
            scope=plan.scopes[node.slug],
            title=node.title,
            at=node.created_at,
            body=node.body,
            summary=node.summary,
            slug=node.slug,
            pinned=node.pinned,
            tags=node.tags,
            session=session,
            created_at=node.created_at,
            note=f"migrated from nodes/{node.file}",
        )
        if node.updated_at != node.created_at:
            records.add_event(
                tx, rec.id, "edited", node.updated_at, note="last markdown update", session=session
            )
            rec = records.touch(tx, rec.id, node.updated_at)
        by_slug[node.slug] = rec
        report.created.append(node)
    for node in plan.nodes:
        src = by_slug.get(node.slug) or records.resolve(tx, node.slug)
        for target in node.targets:
            if target not in plan.resolvable:
                report.unresolved.setdefault(target, []).append(node.file)
                continue
            dst = by_slug.get(target) or records.resolve(tx, target)
            if dst.id == src.id or _related(tx, src, dst):
                continue
            records.link(tx, src, "RELATED", dst, at)
            records.add_event(
                tx,
                src.id,
                "linked",
                at,
                payload={"type": "RELATED", "target": dst.id},
                session=session,
            )
            report.linked += 1
    return report
