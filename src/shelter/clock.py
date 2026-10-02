"""时间工具：内部统一存带时区的 ISO 字符串，便于字符串比较与重启后续排。"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

Clock = Callable[[], datetime]


def utc_clock() -> Clock:
    return lambda: datetime.now(timezone.utc)


def now_iso(clock: Clock) -> str:
    return clock().astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_iso(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def normalize_iso(value: str) -> str:
    return parse_iso(value).isoformat(timespec="seconds")
