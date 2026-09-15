import datetime as dt
from pathlib import Path

import pytest

from kb import calendar
from kb.config import load

# holidays 0.104, country PH, 2026: Ninoy Aquino Day Fri 08-21, National Heroes Day Mon 08-31
NINOY_AQUINO_DAY = dt.date(2026, 8, 21)
NATIONAL_HEROES_DAY = dt.date(2026, 8, 31)


@pytest.fixture
def cfg(tmp_path: Path):
    file = tmp_path / "config.toml"
    file.write_text('[calendar]\nextra_days_off = ["2026-09-16"]\n')
    return load(path=file, home=tmp_path)


@pytest.mark.parametrize(
    ("day", "working", "why"),
    [
        (NATIONAL_HEROES_DAY, False, "PH holiday on a Monday"),
        (NINOY_AQUINO_DAY, False, "PH holiday on a Friday"),
        (dt.date(2026, 9, 12), False, "Saturday"),
        (dt.date(2026, 9, 13), False, "Sunday"),
        (dt.date(2026, 9, 16), False, "extra day off (Wednesday)"),
        (dt.date(2026, 9, 14), True, "plain Monday"),
        (dt.date(2026, 9, 1), True, "Tuesday after the holiday"),
    ],
)
def test_is_working_day(cfg, day: dt.date, working: bool, why: str) -> None:
    assert calendar.is_working_day(day, cfg) is working, why


def test_holiday_names(cfg) -> None:
    assert calendar.holiday_name(NATIONAL_HEROES_DAY, cfg) == "National Heroes Day"
    assert calendar.holiday_name(dt.date(2026, 9, 16), cfg) == "extra day off"
    assert calendar.holiday_name(dt.date(2026, 9, 14), cfg) is None


def test_working_days_between_is_half_open_and_skips_holidays(cfg) -> None:
    anchor = dt.date(2026, 8, 20)  # Thursday
    # Aug 20, 24-28 = 6 working days; Aug 21 and Aug 31 are holidays, weekends drop out
    assert calendar.working_days_between(anchor, NATIONAL_HEROES_DAY, cfg) == 6
    assert calendar.working_days_between(anchor, dt.date(2026, 9, 1), cfg) == 6
    assert calendar.working_days_between(anchor, dt.date(2026, 9, 2), cfg) == 7
    assert calendar.working_days_between(anchor, anchor, cfg) == 0
    assert calendar.working_days_between(anchor, anchor - dt.timedelta(days=3), cfg) == 0
    # a Friday anchor has elapsed by Saturday; a Saturday anchor has not by Monday
    assert calendar.working_days_between(dt.date(2026, 9, 11), dt.date(2026, 9, 12), cfg) == 1
    assert calendar.working_days_between(dt.date(2026, 9, 12), dt.date(2026, 9, 14), cfg) == 0


def test_today_uses_the_configured_timezone(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("KB_TODAY", raising=False)
    manila = tmp_path / "manila.toml"
    manila.write_text('[general]\ntimezone = "Asia/Manila"\n')
    honolulu = tmp_path / "honolulu.toml"
    honolulu.write_text('[general]\ntimezone = "Pacific/Honolulu"\n')
    frozen = dt.datetime(2026, 9, 13, 20, 0, tzinfo=dt.UTC)  # 04:00 Sep 14 Manila, 10:00 Sep 13 HNL

    class FrozenDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return frozen.astimezone(tz) if tz else frozen

    monkeypatch.setattr(calendar.dt, "datetime", FrozenDateTime)
    assert calendar.today(load(path=manila, home=tmp_path)) == dt.date(2026, 9, 14)
    assert calendar.today(load(path=honolulu, home=tmp_path)) == dt.date(2026, 9, 13)


def test_kb_today_env_overrides(cfg, monkeypatch) -> None:
    monkeypatch.setenv("KB_TODAY", "2026-01-02")
    assert calendar.today(cfg) == dt.date(2026, 1, 2)


def test_local_date_and_day_start(cfg) -> None:
    at = dt.datetime(2026, 9, 13, 20, 0, tzinfo=dt.UTC)
    assert calendar.local_date(at, cfg) == dt.date(2026, 9, 14)
    start = calendar.day_start_utc(dt.date(2026, 9, 14), cfg)
    assert start == dt.datetime(2026, 9, 13, 16, 0, tzinfo=dt.UTC)
    assert calendar.local_date(start, cfg) == dt.date(2026, 9, 14)
