# 0002 Urgency is a function, not a column

Date: 2026-09-11
Status: accepted

## Context

Tasks carry one of four schedules: hard deadline, soft date, recurring window, or none
(age only). What the standup should show depends on today's date, the Philippine
working-day calendar and the record's events (snooze, skip, shown). A stored level goes
stale the moment the day changes.

## Decision

- `urgency(record, today)` is computed at query time from schedule + events + calendar.
- Levels: 0 silent, 1 whisper, 2 normal, 3 loud, 4 scream.
- Thresholds live in `config.toml` (`[escalation.hard]`, `[escalation.age]`,
  `[escalation.window]`); a record may override them with its own `escalation` property.
- Defaults: hard whisper T-14 / normal T-7 / loud T-2 calendar days, scream on and after
  the date; soft follows hard until the date, then stays normal and asks once to rebalance
  or close; age whisper 5 / normal 10 / loud 15 working days, no scream; window normal
  inside, silent outside, backlog after 2 skipped windows.
- "today" is the local date in the configured timezone (`Asia/Manila`), not the machine tz.
  Schedules are Neo4j `Date`; events carry UTC timestamps.

## Consequences

- No nightly job, no cached level to invalidate; `kb today` is always a fresh computation.
- The calendar (Mon-Fri minus PH holidays minus `extra_days_off`) is an input to the
  function and must be deterministic for a given date.
- Changing a threshold in config changes every record's level immediately.

## Alternatives considered

| Option | Why not |
| --- | --- |
| Stored `level` column updated by a cron/timer | Stale between runs; a second writer to keep consistent. |
| Escalation rules per record only | Duplicates the same numbers on every task; config default + override covers it. |
