"""Shared helpers for local timestamps across agents and workspace artifacts."""

from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def local_timezone_name() -> str:
    return (
        os.environ.get("LOCAL_TIMEZONE", "").strip()
        or os.environ.get("AGENT_LOCAL_TIMEZONE", "").strip()
        or os.environ.get("TZ", "").strip()
    )


def now_local() -> datetime:
    tz_name = local_timezone_name()
    if tz_name:
        try:
            return datetime.now(ZoneInfo(tz_name))
        except ZoneInfoNotFoundError:
            pass
    return datetime.now().astimezone()


def local_iso_timestamp() -> str:
    return now_local().isoformat(timespec="seconds")


def local_clock_time() -> str:
    return now_local().strftime("%H:%M:%S")


def local_file_timestamp() -> str:
    return now_local().strftime("%Y%m%d-%H%M%S")