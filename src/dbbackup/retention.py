"""Grandfather-father-son retention.

Semantics match restic/borg ``--keep-*``: for each kind of period (day, ISO week,
month, year), walk the backups newest first and keep the newest backup in each of
the last N distinct periods that *have* a backup. ``last`` keeps the N newest
backups outright. The union of everything selected is kept, and the newest backup
is always kept, whatever the policy says.

Periods are computed in UTC, the timezone backup timestamps are recorded in.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime

from .config import Retention

_PERIODS: tuple[tuple[str, Callable[[datetime], tuple]], ...] = (
    ("daily", lambda t: (t.year, t.month, t.day)),
    ("weekly", lambda t: tuple(t.isocalendar())[:2]),
    ("monthly", lambda t: (t.year, t.month)),
    ("yearly", lambda t: (t.year,)),
)


def select_keep(timestamps: Iterable[datetime], policy: Retention) -> set[datetime]:
    newest_first = sorted(set(timestamps), reverse=True)
    if not newest_first:
        return set()

    keep = {newest_first[0]}
    keep.update(newest_first[: policy.last])

    for attr, period_of in _PERIODS:
        wanted = getattr(policy, attr)
        if wanted <= 0:
            continue
        seen: set[tuple] = set()
        for ts in newest_first:
            period = period_of(ts)
            if period in seen:
                continue
            seen.add(period)
            keep.add(ts)
            if len(seen) >= wanted:
                break

    return keep
