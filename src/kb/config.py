"""Config file: ~/.config/kb/config.toml (env KB_CONFIG), defaults, validation, rendering."""

import dataclasses
import json
import os
import re
import tomllib
import typing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_TOML = """\
[general]
timezone = "Asia/Manila"

[neo4j]
bolt_port = 7687
http_port = 7474
data_dir = "~/.local/share/kb"          # neo4j/{data,logs,run,import,plugins,conf} under it
start_timeout = 30                       # seconds the hook / kb-setup waits for bolt
heap_max = "512m"
pagecache = "128m"
tx_log_preallocate = true                # false reclaims ~516 MB of empty transaction logs

[scopes]                                 # name = absolute path; cwd inside -> that scope
# my-project = "~/projects/my-project"

[sources.odoo]
enabled = false
url = ""                                 # https://odoo.example.com
database = ""
username = ""                            # the RPC login (may be a service account)
assignee_login = ""                      # whose tasks to pull; empty = the RPC user
password_file = "~/.config/kb/odoo-auth" # one line, 0600
project_id = 0                           # 0 = every project
scope = ""                               # kb scope the synced tasks get
timeout = 10                             # seconds per RPC call

[calendar]
country = "PH"
extra_days_off = []                      # "YYYY-MM-DD"

[escalation.hard]      # calendar days before the deadline
whisper = 14
normal = 7
loud = 2
[escalation.age]       # working days since the anchor
whisper = 5
normal = 10
loud = 15
[escalation.window]
skips_to_backlog = 2

[index]
budget_lines = 200                       # kb index prints at most this many lines

[backup]
dir = "~/kb-backups"
"""


class ConfigError(Exception):
    """Invalid config file content."""


@dataclass(frozen=True)
class General:
    """``[general]``."""

    timezone: str


@dataclass(frozen=True)
class Neo4j:
    """``[neo4j]``: ports, data dir, start timeout, JVM memory, transaction log preallocation."""

    bolt_port: int
    http_port: int
    data_dir: Path
    start_timeout: int
    heap_max: str
    pagecache: str
    tx_log_preallocate: bool


@dataclass(frozen=True)
class OdooSource:
    """``[sources.odoo]``: connection, project filter, target scope."""

    enabled: bool
    url: str
    database: str
    username: str
    assignee_login: str
    password_file: Path
    project_id: int
    scope: str
    timeout: int


@dataclass(frozen=True)
class Sources:
    """``[sources]``."""

    odoo: OdooSource


@dataclass(frozen=True)
class Calendar:
    """``[calendar]``: country code and extra days off."""

    country: str
    extra_days_off: tuple[str, ...]


@dataclass(frozen=True)
class Thresholds:
    """Escalation thresholds in days: whisper, normal, loud."""

    whisper: int
    normal: int
    loud: int


@dataclass(frozen=True)
class Window:
    """``[escalation.window]``."""

    skips_to_backlog: int


@dataclass(frozen=True)
class Escalation:
    """``[escalation]``."""

    hard: Thresholds
    age: Thresholds
    window: Window


@dataclass(frozen=True)
class Index:
    """``[index]``."""

    budget_lines: int


@dataclass(frozen=True)
class Backup:
    """``[backup]``."""

    dir: Path


@dataclass(frozen=True)
class Config:
    """The validated config: every table present, defaults filling what the file omits."""

    general: General
    neo4j: Neo4j
    scopes: dict[str, Path]
    sources: Sources
    calendar: Calendar
    escalation: Escalation
    index: Index
    backup: Backup


def config_path(home: Path) -> Path:
    """``$KB_CONFIG`` when set, else ``<home>/.config/kb/config.toml``."""
    override = os.environ.get("KB_CONFIG")
    return Path(override) if override else home / ".config" / "kb" / "config.toml"


def render_default() -> str:
    """The default config file text, byte for byte."""
    return DEFAULT_TOML


def expand(value: str, home: Path) -> Path:
    """Expand a leading ``~`` against *home*, not against $HOME (kb-setup --home)."""
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def load(path: Path | None = None, home: Path | None = None) -> Config:
    """Defaults merged with the user file; a missing file yields the defaults."""
    home = home or Path.home()
    path = path or config_path(home)
    data = tomllib.loads(DEFAULT_TOML)
    if path.exists():
        try:
            user = tomllib.loads(path.read_text())
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{path}: {exc}") from exc
        data = _merge(data, user)
    cfg = _build(Config, data, home, "")
    odoo_scope = cfg.sources.odoo.scope
    # a stale scope in a disabled section must not break every other command
    if cfg.sources.odoo.enabled and odoo_scope and odoo_scope not in ("global", *cfg.scopes):
        raise ConfigError(
            f"sources.odoo.scope: {odoo_scope!r} is not global or a [scopes] name "
            f"({', '.join(cfg.scopes) or 'none configured'})"
        )
    return cfg


def dump_toml(cfg: Config) -> str:
    """Config as TOML text: one table per section, no comments."""
    lines: list[str] = []
    _emit(to_plain(cfg), "", lines)
    return "\n".join(lines).strip() + "\n"


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _build[T](cls: type[T], data: dict[str, Any], home: Path, prefix: str) -> T:
    hints = typing.get_type_hints(cls)
    names = [f.name for f in dataclasses.fields(cls)]  # type: ignore[arg-type]
    unknown = sorted(set(data) - set(names))
    if unknown:
        raise ConfigError(f"unknown key: {prefix}{unknown[0]}")
    missing = [name for name in names if name not in data]
    if missing:
        raise ConfigError(f"missing key: {prefix}{missing[0]}")
    kwargs = {name: _coerce(hints[name], data[name], home, f"{prefix}{name}") for name in names}
    return cls(**kwargs)


def _coerce(hint: Any, value: Any, home: Path, key: str) -> Any:
    if dataclasses.is_dataclass(hint):
        if not isinstance(value, dict):
            raise _type_error(key, "table", value)
        return _build(hint, value, home, f"{key}.")
    origin = typing.get_origin(hint)
    if origin is dict:
        if not isinstance(value, dict):
            raise _type_error(key, "table", value)
        item_hint = typing.get_args(hint)[1]
        return {k: _coerce(item_hint, v, home, f"{key}.{k}") for k, v in value.items()}
    if origin is tuple:
        if not isinstance(value, list):
            raise _type_error(key, "array", value)
        item_hint = typing.get_args(hint)[0]
        return tuple(_coerce(item_hint, v, home, f"{key}[{i}]") for i, v in enumerate(value))
    return _scalar(hint, value, home, key)


_SCALAR_TYPES: dict[Any, type] = {int: int, str: str, bool: bool, Path: str}


def _scalar(hint: Any, value: Any, home: Path, key: str) -> Any:
    expected = _SCALAR_TYPES[hint]
    # bool is an int subclass: `true` must not pass for an int field and vice versa
    if isinstance(value, bool) is not (hint is bool) or not isinstance(value, expected):
        raise _type_error(key, expected.__name__ if hint is not Path else "path string", value)
    return expand(value, home) if hint is Path else value


def _type_error(key: str, expected: str, value: Any) -> ConfigError:
    return ConfigError(f"{key}: expected {expected}, got {type(value).__name__}")


def to_plain(obj: Any) -> Any:
    """Config as nested dicts/lists/str (Paths as strings), for TOML and JSON output."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: to_plain(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {k: to_plain(v) for k, v in obj.items()}
    if isinstance(obj, tuple):
        return [to_plain(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    return obj


def _emit(table: dict[str, Any], prefix: str, lines: list[str]) -> None:
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    if prefix and (scalars or not subtables):
        lines.append(f"[{prefix}]")
    lines.extend(f"{_toml_key(k)} = {_toml_value(v)}" for k, v in scalars.items())
    for k, v in subtables.items():
        lines.append("")
        _emit(v, f"{prefix}.{k}" if prefix else k, lines)


def _toml_key(key: str) -> str:
    return key if re.fullmatch(r"[A-Za-z0-9_]+", key) else json.dumps(key)


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return json.dumps(str(value), ensure_ascii=False)
