"""Minimal 5-field cron (minute hour day-of-month month day-of-week), evaluated in UTC.

Fields support: `*`, lists (`1,5`), ranges (`1-5`), steps (`*/15`, `10-30/5`). Day-of-week is 0-6 with 0=Sunday
(7 is also accepted as Sunday). As in classic cron, if both day-of-month and day-of-week are restricted, a day
matches when EITHER matches. No names (`mon`, `jan`), no `@daily` shortcuts, no timezones other than UTC.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

_RANGES = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 7)]
_NAMES = ["minute", "hour", "day-of-month", "month", "day-of-week"]


def _parse_field(text: str, lo: int, hi: int, name: str) -> set[int]:
    values: set[int] = set()
    for part in text.split(","):
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            if not step_s.isdigit() or int(step_s) < 1:
                raise ValueError(f"{name}: invalid step '{step_s}'")
            step = int(step_s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            a_s, _, b_s = part.partition("-")
            if not (a_s.isdigit() and b_s.isdigit()):
                raise ValueError(f"{name}: invalid range '{part}'")
            a, b = int(a_s), int(b_s)
        elif part.isdigit():
            a = int(part)
            b = hi if step > 1 else a      # "5/10" means 5-max/10, as in most crons
        else:
            raise ValueError(f"{name}: invalid value '{part}'")
        if a < lo or b > hi or a > b:
            raise ValueError(f"{name}: '{part}' out of range {lo}-{hi}")
        values.update(range(a, b + 1, step))
    return values


def parse(expr: str) -> tuple[set[int], set[int], set[int], set[int], set[int], bool, bool]:
    """Parse an expression. Raises ValueError with a readable message when invalid."""
    if not isinstance(expr, str):
        raise ValueError("cron expression must be a string")
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError("cron expression needs exactly 5 fields: minute hour day-of-month month day-of-week")
    sets = [_parse_field(p, lo, hi, n) for p, (lo, hi), n in zip(parts, _RANGES, _NAMES)]
    sets[4] = {0 if v == 7 else v for v in sets[4]}
    dom_restricted = parts[2] != "*" and not parts[2].startswith("*/")
    dow_restricted = parts[4] != "*" and not parts[4].startswith("*/")
    return (*sets, dom_restricted, dow_restricted)  # type: ignore[return-value]


def next_fire(expr: str, after: float) -> float:
    """First matching minute strictly after the given POSIX timestamp (UTC). Searches up to ~5 years ahead."""
    minutes, hours, doms, months, dows, dom_r, dow_r = parse(expr)
    t = datetime.fromtimestamp(after, tz=timezone.utc).replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = t + timedelta(days=366 * 5)
    while t < limit:
        if t.month not in months:
            t = (t.replace(day=1, hour=0, minute=0) + timedelta(days=32)).replace(day=1)
            continue
        dow = (t.weekday() + 1) % 7                      # python: Monday=0 -> cron: Sunday=0
        if dom_r and dow_r:
            day_ok = t.day in doms or dow in dows
        else:
            day_ok = t.day in doms and dow in dows
        if not day_ok:
            t = t.replace(hour=0, minute=0) + timedelta(days=1)
            continue
        if t.hour not in hours:
            t = t.replace(minute=0) + timedelta(hours=1)
            continue
        if t.minute not in minutes:
            t += timedelta(minutes=1)
            continue
        return t.timestamp()
    raise ValueError(f"cron expression '{expr}' never fires within 5 years")
