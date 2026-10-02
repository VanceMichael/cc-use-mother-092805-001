"""测试公共装置：确定性 ID 与可控时钟。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from src.backend import ShelterService, Store


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.moment = start or datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.moment

    def tick(self, minutes: int = 1) -> None:
        self.moment += timedelta(minutes=minutes)


class SequentialIds:
    def __init__(self) -> None:
        self.counters: dict[str, int] = {}

    def __call__(self, prefix: str) -> str:
        self.counters[prefix] = self.counters.get(prefix, 0) + 1
        return f"{prefix}-{self.counters[prefix]:04d}"


def make_service(db_path: str = ":memory:") -> tuple[ShelterService, FakeClock]:
    clock = FakeClock()
    service = ShelterService(Store(db_path), clock=clock, id_factory=SequentialIds())
    return service, clock


def make_site(service: ShelterService, capacity: int = 100) -> tuple[str, str]:
    """建一个社区 + 一个安置点，返回 (community_id, shelter_id)。"""
    community_id = service.register_community(
        "coord", "coordinator", name="湄林社区", province="清迈"
    )
    shelter_id = service.register_shelter(
        "coord", "coordinator", community_id=community_id, name="一号安置点", capacity=capacity
    )
    return community_id, shelter_id
