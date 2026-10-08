from datetime import UTC, datetime, timedelta

from dbbackup.config import Retention
from dbbackup.retention import select_keep


def ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def daily_backups(start: datetime, days: int, hour: int = 2) -> list[datetime]:
    return [(start + timedelta(days=i)).replace(hour=hour) for i in range(days)]


def test_empty():
    assert select_keep([], Retention()) == set()


def test_newest_is_always_kept_even_with_an_empty_policy():
    backups = daily_backups(ts(2026, 1, 1), 5)
    assert select_keep(backups, Retention(last=0, daily=0, weekly=0, monthly=0, yearly=0)) == {backups[-1]}


def test_daily_keeps_newest_per_day():
    morning, evening = ts(2026, 3, 10, 2), ts(2026, 3, 10, 20)
    previous = ts(2026, 3, 9, 2)
    keep = select_keep([previous, morning, evening], Retention(daily=2, weekly=0, monthly=0))
    assert keep == {evening, previous}


def test_last_keeps_n_newest_regardless_of_period():
    backups = [ts(2026, 3, 10, h) for h in (1, 2, 3, 4)]
    assert select_keep(backups, Retention(last=3, daily=0, weekly=0, monthly=0)) == set(backups[1:])


def test_gfs_over_a_year_of_dailies():
    backups = daily_backups(ts(2025, 1, 1), 400)  # 2025-01-01 .. 2026-02-04
    keep = select_keep(backups, Retention(daily=7, weekly=4, monthly=6, yearly=2))

    newest = backups[-1]
    dailies = {b for b in backups if newest - b < timedelta(days=7)}
    assert dailies <= keep

    # Weekly: the newest backup of each of the 4 newest ISO weeks.
    weeks = {}
    for b in backups:
        weeks[tuple(b.isocalendar())[:2]] = b
    newest_weeks = sorted(weeks.values())[-4:]
    assert set(newest_weeks) <= keep

    # Monthly: the last backup of each of the 6 newest months (the newest month's is the newest backup).
    months = {}
    for b in backups:
        months[(b.year, b.month)] = b
    assert set(sorted(months.values())[-6:]) <= keep
    assert ts(2025, 12, 31, 2) in keep
    assert ts(2025, 9, 30, 2) in keep

    # Yearly: 2026's newest, and 2025's last.
    assert ts(2025, 12, 31, 2) in keep

    # And nothing else.
    expected = dailies | set(newest_weeks) | set(sorted(months.values())[-6:]) | {ts(2025, 12, 31, 2), newest}
    assert keep == expected
    assert len(keep) < 20


def test_periods_with_no_backup_dont_count():
    # A two-month gap: "monthly=3" still keeps three backups, reaching further back.
    backups = [ts(2026, 1, 15), ts(2026, 4, 15), ts(2026, 5, 15)]
    assert select_keep(backups, Retention(daily=0, weekly=0, monthly=3)) == set(backups)


def test_iso_weeks_span_year_boundaries():
    # 2025-12-29 .. 2026-01-04 is ISO week 2026-W01: one week, so weekly=1 keeps only the newest.
    backups = daily_backups(ts(2025, 12, 29), 7)
    assert select_keep(backups, Retention(daily=0, weekly=1, monthly=0)) == {backups[-1]}


def test_duplicates_are_harmless():
    b = ts(2026, 1, 1)
    assert select_keep([b, b], Retention()) == {b}
