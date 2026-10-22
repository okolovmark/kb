"""Tag vocabulary: area tags from the project's ``.kb/tags.toml`` plus kb's built-in areas, and
the built-in type tags (docs/adr/0007).

Area tags name a module or subsystem and pull context (``kb context``); type tags say what
kind of work a record is and pull nothing. The vocabulary is closed: ``add`` and ``edit``
refuse a tag outside it, while tags records already carry stay as they are.
"""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from kb.config import Config

TAGS_FILE = Path(".kb") / "tags.toml"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# Areas every scope may use: what global and personal records are about, and the setup
# around any project. Decided in the PR that added them (2026-10-22); a change is a release.
BUILTIN_AREAS = {
    "kb": "kb itself: the CLI, the store, its hooks and plugins",
    "claude_setup": "agent harnesses: Claude Code settings, hooks, skills, plugins, MCP servers",
    "tooling": "developer tools and the machine: git, Nix, editors, linters, the desktop",
    "workflow": "how the work is done: rules, conventions, estimates, reviews, status",
    "writing": "texts written in your name: posts, messages, the CV, documentation style",
    "career": "jobs, applications, the professional profile",
    "finance": "money: taxes, bills, accounts",
    "home": "the household and errands",
}
TYPE_TAGS = {
    "bug": "something is broken",
    "feature": "something new",
    "refactor": "restructuring with no change in behaviour",
    "review": "reviewing work, one's own or someone else's",
    "testing": "tests and what runs them",
    "docs": "documentation",
    "config": "configuration and settings",
    "research": "finding something out",
    "discussion": "a conversation or a decision still to reach",
    "cleanup": "removing what is no longer needed",
    "migration": "moving code or data between versions or systems",
    "data_fix": "correcting stored data",
}
# the kinds an add may leave without an area tag; personal records never need one
UNTAGGED_KINDS = frozenset({"session"})

SEED = """\
# Area tags for kb records in this project, one per line: name = "what it covers".
# kb add here needs at least one area tag (--tag NAME): one of these or a built-in area;
# `kb tags` lists both with their use counts. Type tags (bug, feature, ...) are built in.
# Names are lower case: letters, digits, '_' and '-'. A new area is a change to this file.

[areas]
# example = "what records tagged example are about"
"""


class VocabularyError(Exception):
    """A ``.kb/tags.toml`` kb cannot read."""


class TagError(Exception):
    """A tag outside the vocabulary, or no area tag where one is required."""


@dataclass(frozen=True)
class Vocabulary:
    """The tags one scope may use."""

    scope: str
    path: Path | None  # where the scope's tags.toml lives; None for global and personal
    project_areas: dict[str, str]
    found: bool = False  # the file exists and was read

    @property
    def file(self) -> Path | None:
        """The tags.toml in use, None when the scope has none."""
        return self.path if self.found else None

    @property
    def areas(self) -> dict[str, str]:
        """Project areas first, then the built-in ones; a project description wins."""
        return {
            **self.project_areas,
            **{k: v for k, v in BUILTIN_AREAS.items() if k not in self.project_areas},
        }

    def origin(self, area: str) -> str:
        return "file" if area in self.project_areas else "built-in"

    @property
    def valid(self) -> set[str]:
        return set(self.areas) | set(TYPE_TAGS)

    def area_tags(self, tags: list[str]) -> list[str]:
        """The tags in *tags* that are areas of this vocabulary, in their order."""
        areas = self.areas
        return [t for t in tags if t in areas]

    def check(self, tags: list[str]) -> list[str]:
        """*tags* normalised (lower case, trimmed, each once); TagError naming the unknown."""
        clean = list(dict.fromkeys(t.strip().lower() for t in tags if t.strip()))
        unknown = [t for t in clean if t not in self.valid]
        if unknown:
            raise TagError(
                f"unknown {'tag' if len(unknown) == 1 else 'tags'} {', '.join(unknown)} "
                f"in scope {self.scope}; {self.listing()}"
            )
        return clean

    def listing(self) -> str:
        """``areas: a, b (from <file>), built-in: ...; types: ...`` for an error line."""
        parts = []
        if self.project_areas:
            parts.append(f"areas: {', '.join(self.project_areas)} (from {self.path})")
        elif self.found:
            parts.append(f"no areas in {self.path}")
        elif self.path is not None:
            parts.append(f"no {self.path} (kb tags init writes one)")
        builtin = [a for a in BUILTIN_AREAS if a not in self.project_areas]
        parts.append(f"built-in areas: {', '.join(builtin)}")
        parts.append(f"types: {', '.join(TYPE_TAGS)}")
        return "; ".join(parts)


def project_root(cfg: Config, scope: str) -> Path | None:
    """The repo root of a project scope (its ``[scopes]`` path); None for global/personal."""
    return cfg.scopes.get(scope)


def tags_file(cfg: Config, scope: str) -> Path | None:
    root = project_root(cfg, scope)
    return root / TAGS_FILE if root is not None else None


def parse(text: str, path: Path) -> dict[str, str]:
    """``[areas] name = "description"``; anything else is an error, as in config.toml."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise VocabularyError(f"{path}: {exc}") from exc
    unknown = sorted(set(data) - {"areas"})
    if unknown:
        raise VocabularyError(f"{path}: unknown key {unknown[0]!r}; the file has one [areas] table")
    areas = data.get("areas", {})
    if not isinstance(areas, dict):
        raise VocabularyError(f"{path}: areas must be a table")
    out: dict[str, str] = {}
    for name, description in areas.items():
        if not NAME.match(name):
            raise VocabularyError(
                f"{path}: area {name!r}: lower case letters, digits, '_' and '-' only"
            )
        if name in TYPE_TAGS:
            raise VocabularyError(f"{path}: area {name!r} is a built-in type tag")
        if not isinstance(description, str):
            raise VocabularyError(f"{path}: area {name!r}: the description must be a string")
        out[name] = " ".join(description.split())
    return out


def load(cfg: Config, scope: str) -> Vocabulary:
    """The vocabulary of *scope*: its project file when there is one, the built-in tags."""
    path = tags_file(cfg, scope)
    if path is None or not path.is_file():
        return Vocabulary(scope, path, {})
    try:
        text = path.read_text()
    except OSError as exc:
        raise VocabularyError(f"{path}: {exc}") from exc
    return Vocabulary(scope, path, parse(text, path), found=True)


def requires_area(kind: str, scope: str) -> bool:
    """Every kind but a session needs an area tag, outside the personal scope."""
    return kind not in UNTAGGED_KINDS and scope != "personal"


def require_area(vocab: Vocabulary, kind: str, tags: list[str], what: str) -> None:
    """TagError when *kind* in the vocabulary's scope needs an area and *tags* holds none."""
    if requires_area(kind, vocab.scope) and not vocab.area_tags(tags):
        raise TagError(
            f"{what} needs an area tag in scope {vocab.scope}: --tag AREA; {vocab.listing()}"
        )
