"""US Central Time clock. All decision cutoffs are CT."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from . import config as C


def now_ct() -> datetime:
    return datetime.now(timezone.utc).astimezone(C.CT)


def today_ct() -> str:
    return now_ct().strftime("%Y-%m-%d")


def _at(date_str: str, hhmm: str) -> datetime:
    parts = [int(x) for x in hhmm.split(":")]
    hh, mm = parts[0], parts[1]
    ss = parts[2] if len(parts) > 2 else 0
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return datetime(d.year, d.month, d.day, hh, mm, ss, tzinfo=C.CT)


def market_open(date_str: str, open_hhmm: str | None = None) -> datetime:
    return _at(date_str, open_hhmm or C.MARKET_OPEN_CT)


def decision_at(date_str: str, open_hhmm: str | None = None) -> datetime:
    return market_open(date_str, open_hhmm) + timedelta(minutes=C.DECISION_LAG_MIN)


def cancel_at(sent: datetime) -> datetime:
    return sent + timedelta(minutes=C.CANCEL_AFTER_MIN)


def before_decision(date_str: str, open_hhmm: str | None = None) -> bool:
    return now_ct() < decision_at(date_str, open_hhmm)


def poll_start(date_str: str) -> datetime:
    return _at(date_str, C.POLL_START_CT)


def json_deadline(date_str: str) -> datetime:
    return _at(date_str, C.JSON_DEADLINE_CT)


def in_poll_window(when: datetime | None = None) -> bool:
    when = when or now_ct()
    d = when.strftime("%Y-%m-%d")
    return poll_start(d) <= when <= json_deadline(d)


def past_json_deadline(date_str: str | None = None) -> bool:
    date_str = date_str or today_ct()
    return now_ct() > json_deadline(date_str)


def weekday_ct(when: datetime | None = None) -> bool:
    when = when or now_ct()
    return when.weekday() < 5  # Mon-Fri


def fmt(when: datetime | None) -> str:
    if when is None:
        return "never"
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(C.CT).strftime("%-I:%M:%S %p CT")


def event_date_tokens(date_str: str) -> list[str]:
    d = datetime.strptime(date_str, "%Y-%m-%d")
    return [
        d.strftime("%d%b%y").upper(),   # 15SEP26
        d.strftime("%y%b%d").upper(),
        d.strftime("%Y%m%d"),
        date_str,
        d.strftime("%d%b").upper(),
    ]
