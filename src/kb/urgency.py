"""urgency(task, today) as pure functions: levels 0..4 from schedule, events and calendar."""

import datetime as dt
import json
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from dateutil.rrule import rrule, rrulestr

from kb.calendar import holiday_name, is_working_day, local_date, today, working_days_between
from kb.config import Config, Thresholds

if TYPE_CHECKING:
    from kb.records import Event, Record

LEVEL_NAMES = ("silent", "whisper", "normal", "loud", "scream")
SILENT, WHISPER, NORMAL, LOUD, SCREAM = range(5)
OVERRIDE_KEYS = ("whisper", "normal", "loud", "skips_to_backlog")
# a run of consecutive window days longer than this is treated as single-day windows
MAX_RUN_DAYS = 31
Window = tuple[dt.date, dt.date]


def overrides(task: Record) -> dict[str, int]:
    """The task's ``escalation`` JSON as a dict (``{}`` when unset or unreadable)."""
    if not task.escalation:
        return {}
    try:
        data = json.loads(task.escalation)
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def thresholds(task: Record, cfg: Config) -> tuple[Thresholds, str]:
    """Whisper/normal/loud for the task's schedule kind, with the per-task override applied.

    The second item names the source: ``config`` or ``task override (<keys>)``.
    """
    base = cfg.escalation.age if task.sched == "age" else cfg.escalation.hard
    over = {k: v for k, v in overrides(task).items() if k in ("whisper", "normal", "loud")}
    if not over:
        return base, "config"
    merged = Thresholds(
        whisper=over.get("whisper", base.whisper),
        normal=over.get("normal", base.normal),
        loud=over.get("loud", base.loud),
    )
    return merged, f"task override ({', '.join(sorted(over))})"


def window_threshold(task: Record, cfg: Config) -> tuple[int, str]:
    """``skips_to_backlog`` for the task: its override or the config value."""
    over = overrides(task).get("skips_to_backlog")
    if over is None:
        return cfg.escalation.window.skips_to_backlog, "config"
    return int(over), "task override (skips_to_backlog)"


# --- windows ----------------------------------------------------------------------------


def created_day(task: Record, cfg: Config) -> dt.date:
    """The record's creation date in the kb timezone (today when it has no timestamp yet)."""
    return local_date(task.created_at, cfg) if task.created_at else today(cfg)


def _rule(task: Record, cfg: Config) -> rrule:
    """The window rrule anchored on ``anchor`` (creation date); ValueError when it is invalid."""
    start = task.anchor or created_day(task, cfg)
    try:
        return rrulestr(task.window_rrule or "", dtstart=dt.datetime.combine(start, dt.time()))
    except (TypeError, KeyError, AttributeError) as exc:
        raise ValueError(str(exc) or type(exc).__name__) from exc


def _at(day: dt.date) -> dt.datetime:
    return dt.datetime.combine(day, dt.time())


def _run(rule: rrule, day: dt.date) -> Window:
    """The maximal run of consecutive window days around *day* (a weekend is one window)."""
    start = end = day
    for _ in range(MAX_RUN_DAYS):
        prev = rule.before(_at(start))
        if prev is None or prev.date() != start - dt.timedelta(days=1):
            break
        start = prev.date()
    for _ in range(MAX_RUN_DAYS):
        nxt = rule.after(_at(end))
        if nxt is None or nxt.date() != end + dt.timedelta(days=1):
            break
        end = nxt.date()
    if (end - start).days >= MAX_RUN_DAYS:
        return day, day
    return start, end


def window_containing(task: Record, day: dt.date, cfg: Config) -> Window | None:
    """The window *day* falls in, or None when *day* is not a window day."""
    rule = _rule(task, cfg)
    occ = rule.before(_at(day), inc=True)
    if occ is None or occ.date() != day:
        return None
    return _run(rule, day)


def last_window_before(task: Record, day: dt.date, cfg: Config) -> Window | None:
    """The most recent window that ended before *day*; None inside a window or before the first."""
    rule = _rule(task, cfg)
    occ = rule.before(_at(day))
    if occ is None:
        return None
    start, end = _run(rule, occ.date())
    if end >= day:
        return None
    return start, end


def next_window_start(task: Record, day: dt.date, cfg: Config) -> dt.date | None:
    """First window day after the window containing *day* (or after *day* itself)."""
    rule = _rule(task, cfg)
    current = window_containing(task, day, cfg)
    nxt = rule.after(_at(current[1] if current else day))
    return nxt.date() if nxt else None


def window_touch(
    events: Iterable[Event],
    start: dt.date,
    end: dt.date,
    cfg: Config,
    snoozed_until: dt.date | None = None,
) -> str | None:
    """What closed the window: a ``done``/``skip`` naming it or dated on/after its first day,
    or ``snooze`` when the window ended inside a snooze period."""
    if snoozed_until is not None and end < snoozed_until:
        return "snooze"
    for event in events:
        if event.kind not in ("done", "skip"):
            continue
        named = event.data.get("window")
        if named == end.isoformat() or (named is None and local_date(event.at, cfg) >= start):
            return event.kind
    return None


def window_touched(
    events: Iterable[Event],
    start: dt.date,
    end: dt.date,
    cfg: Config,
    snoozed_until: dt.date | None = None,
) -> bool:
    return window_touch(events, start, end, cfg, snoozed_until) is not None


def byday(task: Record) -> str:
    """``SA,SU`` from ``FREQ=WEEKLY;BYDAY=SA,SU``, else the whole rule."""
    for part in (task.window_rrule or "").split(";"):
        key, _, value = part.partition("=")
        if key.upper() == "BYDAY":
            return value
    return task.window_rrule or ""


# --- levels -----------------------------------------------------------------------------


def t_label(d: int) -> str:
    """``T-2`` two days before, ``T-0`` on the day, ``T+3`` three days after."""
    return f"T+{-d}" if d < 0 else f"T-{d}"


def _countdown(d: int, th: Thresholds) -> tuple[int, str]:
    reason = t_label(d)
    if d <= th.loud:
        return LOUD, reason
    if d <= th.normal:
        return NORMAL, reason
    if d <= th.whisper:
        return WHISPER, reason
    return SILENT, reason


def _snoozed(task: Record, today: dt.date) -> bool:
    return task.snoozed_until is not None and today < task.snoozed_until


def _snooze_reason(task: Record) -> str:
    return f"snoozed until {task.snoozed_until}"


def _hard(task: Record, today: dt.date, cfg: Config, _events: list[Event]) -> tuple[int, str]:
    if task.due is None:
        return SILENT, "no due date"
    d = (task.due - today).days
    if d <= 0:
        return SCREAM, t_label(d)
    if _snoozed(task, today):
        return SILENT, _snooze_reason(task)
    return _countdown(d, thresholds(task, cfg)[0])


def _soft(task: Record, today: dt.date, cfg: Config, _events: list[Event]) -> tuple[int, str]:
    if task.due is None:
        return SILENT, "no due date"
    if _snoozed(task, today):
        return SILENT, _snooze_reason(task)
    d = (task.due - today).days
    if d <= 0:
        return NORMAL, f"decide: rebalance or done (T+{-d})"
    return _countdown(d, thresholds(task, cfg)[0])


def _age(task: Record, today: dt.date, cfg: Config, _events: list[Event]) -> tuple[int, str]:
    if _snoozed(task, today):
        return SILENT, _snooze_reason(task)
    anchor = task.anchor or created_day(task, cfg)
    w = working_days_between(anchor, today, cfg)
    th = thresholds(task, cfg)[0]
    reason = f"{w} working days since {anchor}"
    if w >= th.loud:
        return LOUD, reason
    if w >= th.normal:
        return NORMAL, reason
    if w >= th.whisper:
        return WHISPER, reason
    return SILENT, reason


_TOUCH_REASONS = {"done": "done this window", "skip": "skipped this window"}


def _window(task: Record, today: dt.date, cfg: Config, events: list[Event]) -> tuple[int, str]:
    if task.backlog:
        return SILENT, f"backlog after {task.skips} skips"
    if _snoozed(task, today):
        return SILENT, _snooze_reason(task)
    try:
        window = window_containing(task, today, cfg)
        nxt = next_window_start(task, today, cfg) if window is None else None
    except ValueError as exc:
        # one broken rule must not take the whole standup down
        return SILENT, f"invalid rrule: {exc}"
    if window is None:
        return SILENT, f"next window {nxt}" if nxt else "no window ahead"
    touch = window_touch(events, *window, cfg)
    if touch in _TOUCH_REASONS:
        return SILENT, _TOUCH_REASONS[touch]
    return NORMAL, f"window {byday(task)}"


def _once(task: Record, today: dt.date, _cfg: Config, events: list[Event]) -> tuple[int, str]:
    if any(e.kind == "shown" for e in events):
        return SILENT, "shown"
    if _snoozed(task, today):
        return SILENT, _snooze_reason(task)
    return NORMAL, "once"


_BY_SCHEDULE = {"hard": _hard, "soft": _soft, "age": _age, "window": _window, "once": _once}


def raw_level(
    task: Record, today: dt.date, cfg: Config, events: Iterable[Event] = ()
) -> tuple[int, str]:
    """Level before the day-fit adjustment."""
    sched = task.sched or "age"
    compute = _BY_SCHEDULE.get(sched)
    if compute is None:
        return SILENT, f"unknown schedule {sched!r}"
    return compute(task, today, cfg, list(events))


def day_fit(task: Record, today: dt.date, cfg: Config, lvl: int, reason: str) -> tuple[int, str]:
    """Lower by one when the task's ``days`` does not fit today.

    Scream and silence are unchanged; ``once`` is exempt (a notification fires on the next
    session whatever the day).
    """
    days = task.days or "any"
    if lvl in (SILENT, SCREAM) or days == "any" or task.sched == "once":
        return lvl, reason
    working = is_working_day(today, cfg)
    if days == "work" and not working:
        return lvl - 1, f"{reason} (off-day)"
    if days == "off" and working:
        return lvl - 1, f"{reason} (workday)"
    return lvl, reason


def level(
    task: Record, today: dt.date, cfg: Config, events: Iterable[Event] = ()
) -> tuple[int, str]:
    """``(level 0..4, reason)`` for *task* on *today*."""
    events = list(events)
    lvl, reason = raw_level(task, today, cfg, events)
    return day_fit(task, today, cfg, lvl, reason)


def describe(lvl: int, reason: str) -> str:
    """``L3 loud — T-2``."""
    return f"L{lvl} {LEVEL_NAMES[lvl]} — {reason}"


def schedule_line(task: Record) -> str:
    """One line describing the task's schedule and its state."""
    sched = task.sched or "age"
    parts = [sched]
    if sched in ("hard", "soft"):
        parts.append(f"due {task.due}")
    elif sched == "age":
        parts.append(f"anchor {task.anchor}")
    elif sched == "window":
        parts.append(task.window_rrule or "")
    parts.append(f"days={task.days or 'any'}")
    if task.repeat:
        parts.append(f"repeat={task.repeat}")
    if task.snoozed_until:
        parts.append(f"snoozed until {task.snoozed_until}")
    if task.skips:
        parts.append(f"skips={task.skips}")
    if task.backlog:
        parts.append("backlog")
    if task.done_at:
        parts.append(f"done {task.done_at:%Y-%m-%d}")
    return " ".join(parts)


def explain(task: Record, today: dt.date, cfg: Config, events: Iterable[Event] = ()) -> list[str]:
    """The level computation spelled out for ``kb why``."""
    events = list(events)
    sched = task.sched or "age"
    lines = [f"schedule: {schedule_line(task)}", f"today: {today} ({_day_kind(today, cfg)})"]
    if sched in ("hard", "soft", "age"):
        th, source = thresholds(task, cfg)
        unit = "working days since the anchor" if sched == "age" else "calendar days before due"
        lines.append(
            f"thresholds: whisper {th.whisper} / normal {th.normal} / loud {th.loud} "
            f"{unit} ({source})"
        )
    if sched in ("hard", "soft") and task.due:
        lines.append(f"due: {task.due}, {t_label((task.due - today).days)}")
    if sched == "age":
        anchor = task.anchor or created_day(task, cfg)
        lines.append(
            f"age: {working_days_between(anchor, today, cfg)} working days in [{anchor}, {today})"
        )
    if sched == "window":
        threshold, source = window_threshold(task, cfg)
        lines.append(f"backlog after {threshold} skips ({source}); skips so far {task.skips}")
        try:
            current = window_containing(task, today, cfg)
            last = last_window_before(task, today, cfg)
            nxt = next_window_start(task, today, cfg)
        except ValueError as exc:
            lines.append(f"rrule: invalid ({exc})")
        else:
            lines.append(f"current window: {_window_text(current)}")
            lines.append(f"last ended window: {_window_text(last)}")
            lines.append(f"next window: {nxt}")
    if sched == "once":
        lines.append(f"shown events: {sum(1 for e in events if e.kind == 'shown')}")
    if task.snoozed_until:
        state = "active" if _snoozed(task, today) else "expired"
        lines.append(f"snooze: until {task.snoozed_until} ({state})")
    else:
        lines.append("snooze: none")
    raw_lvl, raw_reason = raw_level(task, today, cfg, events)
    lines.append(f"before day fit: {describe(raw_lvl, raw_reason)}")
    lvl, reason = day_fit(task, today, cfg, raw_lvl, raw_reason)
    fit = "fits" if lvl == raw_lvl else f"lowered from L{raw_lvl}"
    lines.append(f"day fit: days={task.days or 'any'} on a {_day_kind(today, cfg)}: {fit}")
    lines.append(f"result: {describe(lvl, reason)}")
    return lines


def _day_kind(day: dt.date, cfg: Config) -> str:
    name = day.strftime("%A")
    if is_working_day(day, cfg):
        return f"{name}, working day"
    holiday = holiday_name(day, cfg)
    return f"{name}, {holiday}" if holiday else f"{name}, non-working day"


def _window_text(window: Window | None) -> str:
    if window is None:
        return "none"
    start, end = window
    return f"{start}" if start == end else f"{start}..{end}"


def as_json(
    task: Record, today: dt.date, cfg: Config, events: Iterable[Event] = ()
) -> dict[str, Any]:
    """Level, name and reason as a dict."""
    lvl, reason = level(task, today, cfg, events)
    return {"level": lvl, "name": LEVEL_NAMES[lvl], "reason": reason}
