import datetime as dt
import json

import pytest

from kb import urgency
from kb.config import load
from kb.records import Event, Record

UTC = dt.UTC
DUE = dt.date(2026, 9, 15)  # a Tuesday
MANILA_NOON = dt.time(4, 0)  # 12:00 Asia/Manila as UTC


@pytest.fixture(scope="module")
def cfg(tmp_path_factory: pytest.TempPathFactory):
    home = tmp_path_factory.mktemp("home")
    return load(path=home / "none.toml", home=home)


def task(**overrides) -> Record:
    base = {
        "id": 1,
        "kind": "task",
        "scope": "proj",
        "title": "t",
        "created_at": dt.datetime(2026, 8, 1, tzinfo=UTC),
        "sched": "age",
        "anchor": dt.date(2026, 8, 20),
        "days": "any",
    }
    base.update(overrides)
    return Record(**base)


def event(kind: str, day: dt.date, **payload) -> Event:
    return Event(
        kind=kind,
        at=dt.datetime.combine(day, MANILA_NOON, tzinfo=UTC),
        payload=json.dumps(payload) if payload else None,
    )


# --- hard / soft ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("today", "expected"),
    [
        (DUE - dt.timedelta(days=15), (0, "T-15")),
        (DUE - dt.timedelta(days=14), (1, "T-14")),
        (DUE - dt.timedelta(days=7), (2, "T-7")),
        (DUE - dt.timedelta(days=2), (3, "T-2")),
        (DUE, (4, "T-0")),
        (DUE + dt.timedelta(days=3), (4, "T+3")),
    ],
)
def test_hard_taxes_due_on_the_15th(cfg, today: dt.date, expected) -> None:
    assert urgency.level(task(sched="hard", due=DUE), today, cfg) == expected


def test_soft_after_the_date_asks_once_at_normal(cfg) -> None:
    soft = task(sched="soft", due=DUE)
    assert urgency.level(soft, DUE + dt.timedelta(days=1), cfg) == (
        2,
        "decide: rebalance or done (T+1)",
    )
    assert urgency.level(soft, DUE, cfg) == (2, "decide: rebalance or done (T+0)")
    assert urgency.level(soft, DUE - dt.timedelta(days=2), cfg) == (3, "T-2")
    assert urgency.level(soft, DUE - dt.timedelta(days=30), cfg) == (0, "T-30")


def test_snooze_on_hard_is_ignored_on_and_after_the_due_date(cfg) -> None:
    snoozed = task(sched="hard", due=DUE, snoozed_until=DUE + dt.timedelta(days=5))
    assert urgency.level(snoozed, DUE - dt.timedelta(days=2), cfg) == (
        0,
        f"snoozed until {DUE + dt.timedelta(days=5)}",
    )
    assert urgency.level(snoozed, DUE, cfg) == (4, "T-0")
    assert urgency.level(snoozed, DUE + dt.timedelta(days=1), cfg) == (4, "T+1")


def test_snooze_on_soft_is_respected_fully(cfg) -> None:
    snoozed = task(sched="soft", due=DUE, snoozed_until=DUE + dt.timedelta(days=5))
    assert urgency.level(snoozed, DUE + dt.timedelta(days=1), cfg)[0] == 0
    assert urgency.level(snoozed, DUE + dt.timedelta(days=5), cfg)[0] == 2


# --- age --------------------------------------------------------------------------------


def test_age_counts_working_days_and_a_monday_holiday_adds_none(cfg) -> None:
    aged = task(sched="age", anchor=dt.date(2026, 8, 20))
    heroes_day = dt.date(2026, 8, 31)  # Monday, PH National Heroes Day
    tuesday = dt.date(2026, 9, 1)
    assert urgency.raw_level(aged, heroes_day, cfg) == (1, "6 working days since 2026-08-20")
    assert urgency.raw_level(aged, tuesday, cfg) == (1, "6 working days since 2026-08-20")
    assert urgency.raw_level(aged, dt.date(2026, 9, 2), cfg) == (
        1,
        "7 working days since 2026-08-20",
    )
    assert urgency.level(aged, dt.date(2026, 9, 7), cfg)[0] == 2  # 10 working days
    assert urgency.level(aged, dt.date(2026, 9, 14), cfg)[0] == 3  # 15 working days
    assert urgency.level(aged, dt.date(2026, 12, 1), cfg)[0] == 3  # never screams
    assert urgency.level(aged, dt.date(2026, 8, 20), cfg) == (0, "0 working days since 2026-08-20")


def test_age_snooze_hides_while_age_keeps_ticking(cfg) -> None:
    aged = task(sched="age", anchor=dt.date(2026, 8, 20), snoozed_until=dt.date(2026, 9, 15))
    assert urgency.level(aged, dt.date(2026, 9, 14), cfg) == (0, "snoozed until 2026-09-15")
    assert urgency.level(aged, dt.date(2026, 9, 15), cfg)[0] == 3


# --- window -----------------------------------------------------------------------------


def weekend(**overrides) -> Record:
    return task(sched="window", window_rrule="FREQ=WEEKLY;BYDAY=SA,SU", anchor=None, **overrides)


def test_window_is_normal_inside_and_silent_outside(cfg) -> None:
    assert urgency.level(weekend(), dt.date(2026, 9, 12), cfg) == (2, "window SA,SU")
    assert urgency.level(weekend(), dt.date(2026, 9, 13), cfg) == (2, "window SA,SU")
    assert urgency.level(weekend(), dt.date(2026, 9, 9), cfg) == (0, "next window 2026-09-12")


def test_window_runs_and_neighbours(cfg) -> None:
    saturday, sunday = dt.date(2026, 9, 12), dt.date(2026, 9, 13)
    assert urgency.window_containing(weekend(), saturday, cfg) == (saturday, sunday)
    assert urgency.window_containing(weekend(), sunday, cfg) == (saturday, sunday)
    assert urgency.window_containing(weekend(), dt.date(2026, 9, 9), cfg) is None
    assert urgency.last_window_before(weekend(), dt.date(2026, 9, 16), cfg) == (saturday, sunday)
    assert urgency.last_window_before(weekend(), sunday, cfg) is None
    assert urgency.next_window_start(weekend(), saturday, cfg) == dt.date(2026, 9, 19)
    assert urgency.next_window_start(weekend(), dt.date(2026, 9, 16), cfg) == dt.date(2026, 9, 19)
    assert urgency.last_window_before(weekend(), dt.date(2026, 8, 1), cfg) is None


def test_window_inside_a_snooze_counts_as_touched(cfg) -> None:
    saturday, sunday = dt.date(2026, 9, 12), dt.date(2026, 9, 13)
    assert urgency.window_touch([], saturday, sunday, cfg, dt.date(2026, 9, 20)) == "snooze"
    assert urgency.window_touch([], saturday, sunday, cfg, sunday) is None  # ended on the date
    assert urgency.window_touched([], saturday, sunday, cfg, dt.date(2026, 9, 14)) is True


def test_invalid_rrule_is_silent_with_a_reason(cfg) -> None:
    broken = task(sched="window", window_rrule="FREQ=BOGUS", anchor=dt.date(2026, 9, 1))
    lvl, reason = urgency.level(broken, dt.date(2026, 9, 12), cfg)
    assert lvl == 0
    assert reason.startswith("invalid rrule: ")
    with pytest.raises(ValueError, match="BOGUS"):
        urgency.window_containing(broken, dt.date(2026, 9, 12), cfg)
    assert any(
        line.startswith("rrule: invalid")
        for line in urgency.explain(broken, dt.date(2026, 9, 12), cfg)
    )


def test_window_done_on_saturday_silences_sunday(cfg) -> None:
    saturday, sunday = dt.date(2026, 9, 12), dt.date(2026, 9, 13)
    done = [event("done", saturday)]
    assert urgency.level(weekend(), sunday, cfg, done) == (0, "done this window")
    stale = [event("done", dt.date(2026, 9, 6))]  # last weekend's done does not count
    assert urgency.level(weekend(), sunday, cfg, stale) == (2, "window SA,SU")
    skipped = [event("skip", saturday, window=sunday.isoformat())]
    assert urgency.level(weekend(), sunday, cfg, skipped) == (0, "skipped this window")
    assert urgency.window_touch(skipped, saturday, sunday, cfg) == "skip"
    assert urgency.window_touch(done, saturday, sunday, cfg) == "done"
    assert urgency.window_touch(stale, saturday, sunday, cfg) is None


def test_window_backlog_after_two_skips(cfg) -> None:
    assert urgency.window_threshold(weekend(), cfg) == (2, "config")
    backlog = weekend(skips=2, backlog=True)
    assert urgency.level(backlog, dt.date(2026, 9, 12), cfg) == (0, "backlog after 2 skips")
    one_skip = weekend(skips=1)
    assert urgency.level(one_skip, dt.date(2026, 9, 12), cfg)[0] == 2


# --- once -------------------------------------------------------------------------------


def test_once_is_normal_until_shown(cfg) -> None:
    once = task(sched="once", anchor=None)
    assert urgency.level(once, DUE, cfg) == (2, "once")
    assert urgency.level(once, DUE, cfg, [event("shown", DUE)]) == (0, "shown")


def test_once_ignores_day_fit_and_respects_snooze(cfg) -> None:
    saturday = dt.date(2026, 9, 12)
    once = task(sched="once", anchor=None, days="work")
    assert urgency.level(once, saturday, cfg) == (2, "once")  # a work task, yet not lowered
    snoozed = task(sched="once", anchor=None, snoozed_until=dt.date(2026, 9, 20))
    assert urgency.level(snoozed, saturday, cfg) == (0, "snoozed until 2026-09-20")
    assert urgency.level(snoozed, dt.date(2026, 9, 20), cfg) == (2, "once")


# --- day fit and overrides --------------------------------------------------------------


@pytest.mark.parametrize(
    ("days", "today", "expected"),
    [
        ("work", dt.date(2026, 9, 13), (2, "T-2 (off-day)")),  # Sunday: loud drops to normal
        ("work", dt.date(2026, 9, 14), (3, "T-1")),
        ("off", dt.date(2026, 9, 14), (2, "T-1 (workday)")),
        ("off", dt.date(2026, 9, 13), (3, "T-2")),
        ("any", dt.date(2026, 9, 13), (3, "T-2")),
    ],
)
def test_day_fit_lowers_by_one(cfg, days: str, today: dt.date, expected) -> None:
    hard = task(sched="hard", due=DUE, days=days)
    assert urgency.level(hard, today, cfg) == expected


def test_day_fit_never_touches_scream_or_silence(cfg) -> None:
    hard = task(sched="hard", due=DUE, days="work")
    assert urgency.level(hard, DUE + dt.timedelta(days=4), cfg) == (4, "T+4")  # Saturday
    far = task(sched="hard", due=DUE, days="work")
    assert urgency.level(far, dt.date(2026, 8, 1), cfg) == (0, "T-45")  # Saturday, silent stays


def test_per_task_escalation_override(cfg) -> None:
    tight = task(sched="hard", due=DUE, escalation=json.dumps({"whisper": 3, "loud": 1}))
    th, source = urgency.thresholds(tight, cfg)
    assert (th.whisper, th.normal, th.loud) == (3, 7, 1)
    assert source == "task override (loud, whisper)"
    assert urgency.level(tight, DUE - dt.timedelta(days=2), cfg) == (2, "T-2")
    assert urgency.level(tight, DUE - dt.timedelta(days=1), cfg) == (3, "T-1")
    assert urgency.level(tight, DUE - dt.timedelta(days=5), cfg) == (2, "T-5")  # normal stays 7
    tighter = task(sched="hard", due=DUE, escalation='{"whisper": 3, "normal": 2, "loud": 1}')
    assert urgency.level(tighter, DUE - dt.timedelta(days=5), cfg) == (0, "T-5")
    assert urgency.level(tighter, DUE - dt.timedelta(days=3), cfg) == (1, "T-3")
    patient = weekend(escalation=json.dumps({"skips_to_backlog": 5}))
    assert urgency.window_threshold(patient, cfg) == (5, "task override (skips_to_backlog)")
    assert urgency.thresholds(task(escalation="not json"), cfg)[1] == "config"


def test_explain_mentions_thresholds_calendar_and_result(cfg) -> None:
    lines = urgency.explain(task(sched="hard", due=DUE, days="work"), dt.date(2026, 9, 13), cfg)
    text = "\n".join(lines)
    assert "thresholds: whisper 14 / normal 7 / loud 2 calendar days before due (config)" in text
    assert "today: 2026-09-13 (Sunday, non-working day)" in text
    assert "before day fit: L3 loud — T-2" in text
    assert "result: L2 normal — T-2 (off-day)" in text
    aged = urgency.explain(task(), dt.date(2026, 8, 31), cfg)
    assert "today: 2026-08-31 (Monday, National Heroes Day)" in "\n".join(aged)
    assert "age: 6 working days in [2026-08-20, 2026-08-31)" in "\n".join(aged)


def test_schedule_line(cfg) -> None:
    assert urgency.schedule_line(
        task(sched="hard", due=DUE, days="work", repeat="interval:30")
    ) == ("hard due 2026-09-15 days=work repeat=interval:30")
    assert urgency.schedule_line(weekend(skips=1, backlog=True, days="off")) == (
        "window FREQ=WEEKLY;BYDAY=SA,SU days=off skips=1 backlog"
    )


def test_pathological_daily_rule_does_not_hang(cfg) -> None:
    daily = task(sched="window", window_rrule="FREQ=DAILY", anchor=None)
    day = dt.date(2026, 9, 13)
    assert urgency.window_containing(daily, day, cfg) == (day, day)


def test_created_day_is_the_kb_timezone_date(cfg) -> None:
    late = task(created_at=dt.datetime(2026, 9, 13, 20, 0, tzinfo=UTC))  # 04:00 Sep 14 Manila
    assert urgency.created_day(late, cfg) == dt.date(2026, 9, 14)
