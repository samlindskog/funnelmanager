"""A tiny, dependency-free 5-field cron parser + next-fire calculator.

The agents scheduler (``app.scheduler``) needs to turn a recurring cron string
into the next ``next_run_at`` timestamp. Rather than add a runtime dependency
(``croniter``) that is not in every environment this imports in, we parse the
standard 5-field crontab syntax ourselves — it is small, well understood, and
unit-testable in isolation.

Supported per field: ``*``, ``a``, ``a,b,c``, ``a-b``, ``*/step``, ``a-b/step``.
Fields (in order): minute (0-59), hour (0-23), day-of-month (1-31),
month (1-12), day-of-week (0-6, Sunday=0; ``7`` also accepted as Sunday).

Semantics match Vixie cron: when **both** day-of-month and day-of-week are
restricted (neither is ``*``) a timestamp matches if it satisfies **either**
(the classic OR rule); when only one is restricted, only that one must match.

All timestamps are handled in **UTC**. ``cron_next`` steps minute-by-minute from
just after the reference instant, bounded to ~4 years so an unsatisfiable
expression (e.g. Feb-31) returns ``None`` instead of looping forever.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Field bounds: (min, max) inclusive.
_MINUTE = (0, 59)
_HOUR = (0, 23)
_DOM = (1, 31)
_MONTH = (1, 12)
_DOW = (0, 6)

# Bound the forward search so an impossible expression terminates (a little over
# four years covers every satisfiable standard cron, incl. Feb-29 yearly).
_MAX_LOOKAHEAD = timedelta(days=1461)


@dataclass(frozen=True)
class CronSpec:
    """A parsed 5-field cron expression as per-field value sets."""

    minutes: frozenset[int]
    hours: frozenset[int]
    doms: frozenset[int]
    months: frozenset[int]
    dows: frozenset[int]
    dom_restricted: bool
    dow_restricted: bool


def _parse_field(field: str, lo: int, hi: int, *, dow: bool = False) -> tuple[frozenset[int], bool]:
    """Parse one crontab field into (value-set, restricted?).

    ``restricted`` is False only for a bare ``*`` (matches every value) — used to
    apply the day-of-month/day-of-week OR rule. Raises ``ValueError`` on any value
    out of range or malformed token.
    """
    field = field.strip()
    if not field:
        raise ValueError("empty cron field")

    restricted = field != "*"
    values: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if not part:
            raise ValueError(f"empty item in cron field {field!r}")
        step = 1
        if "/" in part:
            base, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError as exc:
                raise ValueError(f"invalid step in {part!r}") from exc
            if step <= 0:
                raise ValueError(f"step must be positive in {part!r}")
        else:
            base = part

        if base == "*":
            start, end = lo, hi
        elif "-" in base:
            start_s, _, end_s = base.partition("-")
            try:
                start, end = int(start_s), int(end_s)
            except ValueError as exc:
                raise ValueError(f"invalid range {base!r}") from exc
        else:
            try:
                start = end = int(base)
            except ValueError as exc:
                raise ValueError(f"invalid value {base!r}") from exc

        if dow:
            # Day-of-week: 7 is an alias for Sunday (0). Accept 0..7 as INPUT and
            # expand the range in 0..7 space, THEN fold 7 -> 0 as a set member. This
            # makes ranges that cross the Sunday boundary correct: 0-7 / 1-7 both mean
            # every weekday, and 5-7 = {5,6,0} = Fri,Sat,Sun. (The old code normalized
            # 7->0 on the endpoints *before* the range walk, so 0-7 collapsed to {0}
            # — Sunday only, ~6x too rare — and 1-7 / 5-7 were rejected as
            # 'range start after end'.)
            if not (0 <= start <= 7 and 0 <= end <= 7):
                raise ValueError(f"day-of-week value out of range [0,7] in {base!r}")
            if start > end:
                # A genuinely backwards range (e.g. 5-1) is still invalid.
                raise ValueError(f"range start after end in {base!r}")
            for value in range(start, end + 1, step):
                values.add(0 if value == 7 else value)
            continue

        if start > end:
            raise ValueError(f"range start after end in {base!r}")
        for value in range(start, end + 1, step):
            if not (lo <= value <= hi):
                raise ValueError(f"value {value} out of range [{lo},{hi}] in field {field!r}")
            values.add(value)

    if not values:
        raise ValueError(f"cron field {field!r} matched no values")
    return frozenset(values), restricted


def parse_cron(expr: str) -> CronSpec:
    """Parse a 5-field cron expression. Raises ``ValueError`` if malformed."""
    if not isinstance(expr, str):
        raise ValueError("cron expression must be a string")
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(
            f"cron expression must have exactly 5 fields, got {len(fields)}: {expr!r}"
        )
    minutes, _ = _parse_field(fields[0], *_MINUTE)
    hours, _ = _parse_field(fields[1], *_HOUR)
    doms, dom_restricted = _parse_field(fields[2], *_DOM)
    months, _ = _parse_field(fields[3], *_MONTH)
    dows, dow_restricted = _parse_field(fields[4], *_DOW, dow=True)
    return CronSpec(
        minutes=minutes,
        hours=hours,
        doms=doms,
        months=months,
        dows=dows,
        dom_restricted=dom_restricted,
        dow_restricted=dow_restricted,
    )


def cron_is_valid(expr: str) -> bool:
    """True if ``expr`` parses as a 5-field cron expression."""
    try:
        parse_cron(expr)
        return True
    except ValueError:
        return False


def _matches(spec: CronSpec, t: datetime) -> bool:
    if t.minute not in spec.minutes:
        return False
    if t.hour not in spec.hours:
        return False
    if t.month not in spec.months:
        return False
    dom_ok = t.day in spec.doms
    # Python weekday(): Monday=0..Sunday=6. Cron dow: Sunday=0..Saturday=6.
    cron_dow = (t.weekday() + 1) % 7
    dow_ok = cron_dow in spec.dows
    if spec.dom_restricted and spec.dow_restricted:
        return dom_ok or dow_ok
    if spec.dom_restricted:
        return dom_ok
    if spec.dow_restricted:
        return dow_ok
    return True


def cron_next(expr: str | CronSpec, after: datetime) -> datetime | None:
    """Next UTC minute strictly after ``after`` that matches ``expr``.

    ``after`` may be naive (assumed UTC) or tz-aware. Returns a tz-aware UTC
    datetime truncated to the minute, or ``None`` if nothing matches within the
    bounded lookahead (an effectively-impossible expression).
    """
    spec = expr if isinstance(expr, CronSpec) else parse_cron(expr)
    if after.tzinfo is None:
        after = after.replace(tzinfo=timezone.utc)
    else:
        after = after.astimezone(timezone.utc)
    t = after.replace(second=0, microsecond=0) + timedelta(minutes=1)
    limit = after + _MAX_LOOKAHEAD
    while t <= limit:
        if _matches(spec, t):
            return t
        t += timedelta(minutes=1)
    return None


# How many consecutive fire times to sample when estimating a cron's tightest
# interval. 32 is enough to capture any sub-hour cluster (e.g. a 0-4 * * * burst)
# while staying cheap — each step is a bounded minute-walk.
_MIN_INTERVAL_SAMPLES = 32


def cron_min_interval_minutes(
    expr: str | CronSpec, *, after: datetime | None = None, samples: int = _MIN_INTERVAL_SAMPLES
) -> float | None:
    """The smallest gap (in minutes) between consecutive fires this cron implies.

    Used by the scheduler (P4 Denial-of-Wallet guard) to reject a recurring schedule
    that can fire more often than a configured floor — a prompt-injected
    ``* * * * *`` would otherwise plant an agent LLM turn every minute forever.

    Samples the next ``samples`` fire times from ``after`` and returns the minimum
    consecutive delta, so a burst pattern (e.g. ``0-4 9 * * *`` = five fires a minute
    apart, then sparse) is caught by its tightest cluster, not its average. Returns
    ``None`` when fewer than two fires exist in the bounded lookahead (a single/rare
    expression has no sub-interval to bound).
    """
    spec = expr if isinstance(expr, CronSpec) else parse_cron(expr)
    ref = after or datetime.now(timezone.utc)
    fires: list[datetime] = []
    cursor = ref
    for _ in range(max(2, samples)):
        nxt = cron_next(spec, cursor)
        if nxt is None:
            break
        fires.append(nxt)
        cursor = nxt
    if len(fires) < 2:
        return None
    return min(
        (fires[i + 1] - fires[i]).total_seconds() / 60.0 for i in range(len(fires) - 1)
    )


__all__ = [
    "CronSpec",
    "cron_is_valid",
    "cron_min_interval_minutes",
    "cron_next",
    "parse_cron",
]


# --------------------------------------------------------------------------
# Inline self-check (P11 floor): ``python -m app.cron`` exercises parsing +
# next-fire against known cases. Dependency-free; a fast smoke that the cron
# contract the scheduler relies on holds. Not a substitute for a full suite.
# --------------------------------------------------------------------------
if __name__ == "__main__":  # pragma: no cover
    _base = datetime(2026, 8, 6, 10, 30, tzinfo=timezone.utc)  # a Thursday

    def _eq(expr: str, expected_iso: str | None) -> None:
        got = cron_next(expr, _base)
        got_iso = got.isoformat() if got else None
        assert got_iso == expected_iso, f"{expr!r}: got {got_iso}, want {expected_iso}"

    _eq("* * * * *", "2026-08-06T10:31:00+00:00")
    _eq("*/15 * * * *", "2026-08-06T10:45:00+00:00")
    _eq("0 9 * * *", "2026-08-07T09:00:00+00:00")
    _eq("30 10 * * *", "2026-08-07T10:30:00+00:00")  # strictly after `after`
    _eq("0 0 1 * *", "2026-09-01T00:00:00+00:00")
    _eq("0 12 * * 1", "2026-08-10T12:00:00+00:00")  # next Monday
    _eq("0 0 29 2 *", "2028-02-29T00:00:00+00:00")  # next leap Feb 29
    _eq("0 0 * * 7", "2026-08-09T00:00:00+00:00")  # 7 == Sunday
    _eq("0 0 13 * 5", "2026-08-07T00:00:00+00:00")  # dom=13 OR dow=Fri -> Fri first

    # dow 7<->0 boundary + Sun-crossing ranges (fix #4). _base is a Thursday, so the
    # next midnight for any of these is Fri 2026-08-07 00:00 (Fri is in every set).
    _eq("0 0 * * 0-7", "2026-08-07T00:00:00+00:00")  # 0-7 = every day (was {0} only)
    _eq("0 0 * * 1-7", "2026-08-07T00:00:00+00:00")  # 1-7 = every day (was rejected)
    _eq("0 0 * * 5-7", "2026-08-07T00:00:00+00:00")  # Fri,Sat,Sun (was rejected)
    assert parse_cron("* * * * 0-7").dows == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("* * * * 1-7").dows == frozenset({0, 1, 2, 3, 4, 5, 6})
    assert parse_cron("* * * * 5-7").dows == frozenset({5, 6, 0})  # Fri,Sat,Sun
    assert parse_cron("* * * * 1-5").dows == frozenset({1, 2, 3, 4, 5})  # Mon-Fri intact
    # A Sun-crossing dow range must actually match those weekdays.
    _sat = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)  # a Saturday
    assert cron_next("0 0 * * 5-7", _sat) == datetime(2026, 8, 9, 0, 0, tzinfo=timezone.utc)  # Sun

    assert cron_is_valid("0-30/10 * * * *")
    assert sorted(parse_cron("0-30/10 * * * *").minutes) == [0, 10, 20, 30]
    for bad in ("bad", "60 * * * *", "* * * *", "* * * * 8", "5-1 * * * *"):
        assert not cron_is_valid(bad), f"{bad!r} should be invalid"

    # cron_min_interval_minutes (fix #2 DoW guard).
    assert cron_min_interval_minutes("* * * * *", after=_base) == 1.0
    assert cron_min_interval_minutes("*/15 * * * *", after=_base) == 15.0
    assert cron_min_interval_minutes("0-4 9 * * *", after=_base) == 1.0  # burst caught
    assert cron_min_interval_minutes("0 9 * * *", after=_base) == 1440.0  # daily
    assert cron_min_interval_minutes("0,30 9 * * *", after=_base) == 30.0

    print("app.cron self-check: ALL OK")
