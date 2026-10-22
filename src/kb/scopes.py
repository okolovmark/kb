"""``[scopes]`` in the config file: register a project path, list them, find a path's scope.

The config file is edited by hand, so ``add`` changes it as text: one line inserted or one
line rewritten inside ``[scopes]``, every other line and comment left as it was.
"""

import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from kb.config import render_default

NAME_RE = re.compile(r"[A-Za-z0-9_-]+")
# scope names kb gives a meaning of its own; a project must not take them
RESERVED = frozenset({"global", "personal", "all"})
HEADER_RE = re.compile(r"^\s*\[\s*scopes\s*\]\s*(#.*)?$")
TABLE_RE = re.compile(r"^\s*\[")


class ScopeConflictError(Exception):
    """NAME already maps to another path, or PATH to another name."""


class ScopeNameError(Exception):
    """A name or path ``add`` cannot take at all."""


@dataclass(frozen=True)
class Outcome:
    """What ``add`` did: ``added``, ``exists`` (nothing written) or ``updated`` (--force)."""

    action: str
    name: str
    path: str


def display(path: Path, home: Path) -> str:
    """*path* with the home directory written as ``~``."""
    try:
        rest = path.relative_to(home)
    except ValueError:
        return str(path)
    return "~" if rest == Path() else f"~/{rest}"


def normalise(raw: str, home: Path, cwd: Path) -> Path:
    """An absolute, resolved path from what the user typed (``~``, relative or absolute)."""
    if raw == "~" or raw.startswith("~/"):
        path = home / raw[2:] if raw != "~" else home
    else:
        path = Path(raw)
        if not path.is_absolute():
            path = cwd / path
    return path.resolve()


def scope_of(scopes: dict[str, Path], path: Path) -> str | None:
    """The scope whose path contains *path*; the longest (most specific) path wins."""
    target = path.resolve()
    best: tuple[int, str] | None = None
    for name, raw in scopes.items():
        root = raw.resolve()
        if target == root or root in target.parents:
            depth = len(root.parts)
            if best is None or depth > best[0]:
                best = (depth, name)
    return best[1] if best else None


def _expand(value: str, home: Path) -> Path:
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _check(current: dict[str, Path], name: str, path: Path, home: Path, *, force: bool) -> bool:
    """True when NAME already points at PATH; raises when the pair clashes with the file."""
    shown = display(path, home)
    for other, other_path in current.items():
        if other != name and other_path == path:
            raise ScopeConflictError(
                f"{shown} is already the scope {other!r}; edit [scopes] by hand to rename it"
            )
    if name not in current:
        return False
    if current[name] == path:
        return True
    if not force:
        raise ScopeConflictError(
            f"scope {name!r} already points at {display(current[name], home)}; "
            f"--force rewrites it to {shown}"
        )
    return False


def _place(text: str, name: str, entry: str) -> tuple[str, str]:
    """*text* with *entry* written into [scopes]: NAME's line rewritten, else one line added."""
    lines = text.splitlines(keepends=True)
    head = next((i for i, line in enumerate(lines) if HEADER_RE.match(line)), None)
    if head is None:
        tail = "" if text.endswith("\n") or not text else "\n"
        sep = "\n" if text.strip() else ""
        return f"{text}{tail}{sep}[scopes]\n{entry}\n", "added"
    end = next((i for i in range(head + 1, len(lines)) if TABLE_RE.match(lines[i])), len(lines))
    key_re = re.compile(rf'^\s*("?){re.escape(name)}\1\s*=')
    for i in range(head + 1, end):
        if key_re.match(lines[i]):
            lines[i] = entry + "\n"
            return "".join(lines), "updated"
    # after the section's last non-blank line, so a blank line before the next table stays
    last = max((i for i in range(head + 1, end) if lines[i].strip()), default=head)
    if not lines[last].endswith("\n"):
        lines[last] += "\n"
    lines.insert(last + 1, entry + "\n")
    return "".join(lines), "added"


def add(text: str | None, name: str, path: Path, home: Path, *, force: bool) -> tuple[str, Outcome]:
    """The config text with ``name = "path"`` in ``[scopes]``, and what changed.

    *text* None means the file does not exist yet: it starts from the default config.
    Raises ScopeNameError for a name kb cannot take, ScopeConflictError when the pair clashes
    with an existing one (``force`` rewrites NAME's line; a path that belongs to another name
    is never taken over, because renaming a scope would orphan its records).
    """
    if not NAME_RE.fullmatch(name):
        raise ScopeNameError(f"scope name {name!r}: use letters, digits, '_' and '-'")
    if name in RESERVED:
        raise ScopeNameError(f"scope name {name!r} is reserved by kb")
    text = render_default() if text is None else text
    try:
        raw = tomllib.loads(text).get("scopes", {})
    except tomllib.TOMLDecodeError as exc:
        raise ScopeNameError(f"the config file is not valid TOML: {exc}") from exc
    current = {key: _expand(str(value), home).resolve() for key, value in raw.items()}
    shown = display(path, home)
    if _check(current, name, path, home, force=force):
        return text, Outcome("exists", name, shown)
    entry = f"{name} = {json.dumps(shown, ensure_ascii=False)}"
    new, action = _place(text, name, entry)
    return new, Outcome(action, name, shown)
