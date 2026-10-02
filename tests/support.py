"""测试辅助：可控时钟与常用场景搭建。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.shelter import Backend, Scheduler, connect
from src.shelter.clock import Clock


class SimClock:
    """测试用固定时钟，可推进。"""

    def __init__(self, start: datetime | None = None) -> None:
        self.t = start or datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)

    def iso(self) -> str:
        return self.t.isoformat(timespec="seconds")


def build_world(path: str | Path = ":memory:", clock: Clock | None = None) -> "World":
    conn = connect(path)
    backend = Backend(conn, clock or SimClock())
    scheduler = Scheduler(backend)
    return World(conn, backend, scheduler, backend.clock)


class World:
    def __init__(self, conn, backend: Backend, scheduler: Scheduler, clock: SimClock) -> None:
        self.conn = conn
        self.b = backend
        self.s = scheduler
        self.clock = clock

    def commit(self) -> None:
        self.conn.commit()

    # -- 常用岗位经办人 --
    def seed_operators(self) -> dict[str, str]:
        ids = {
            "medical": self.b.register_operator("MED-1", "นพ.สมชาย", "medical", team="医疗队甲"),
            "child": self.b.register_operator("CP-1", "พี่เลี้ยงเด็ก", "child_protection", team="儿童保护组"),
            "elder": self.b.register_operator("EL-1", "ผู้ดูแลผู้สูงอายุ", "elder_care", team="长者照护组"),
            "staff": self.b.register_operator("SH-1", "เจ้าหน้าที่ศูนย์", "shelter_staff", team="安置点值班室"),
            "logistics": self.b.register_operator("LG-1", "พัสดุ", "logistics", team="物资组"),
            "rescue1": self.b.register_operator("RS-1", "กู้ภัยชุด 1", "rescue", team="救援队一队"),
            "rescue2": self.b.register_operator("RS-2", "กู้ภัยชุด 2", "rescue", team="救援队二队"),
            "rescue3": self.b.register_operator("RS-3", "กู้ภัยชุด 3", "rescue", team="救援队三队"),
            "coordinator": self.b.register_operator("CO-1", "หัวหน้าเวร", "coordinator", team="夜班协调"),
        }
        return ids
