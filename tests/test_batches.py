"""撤离批次：容量校验、未到追踪。"""

import unittest

from src.shelter import CapacityExceeded
from tests.support import build_world


class EvacuationBatchTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh = self.w.b.register_shelter("满员点", capacity_total=1)
        self.people = [
            self.w.b.intake_person(
                received_location="村中", received_by_team="救援队一队",
                received_by_operator="队员甲", display_code=f"EV-{i}",
            )
            for i in range(3)
        ]
        self.batch = self.w.b.create_evacuation_batch(
            "BAT-9", origin_location="村中", team="救援队一队",
        )
        for pid in self.people:
            self.w.b.assign_to_batch(self.batch, pid)

    def test_arrival_respects_capacity_with_override(self) -> None:
        with self.assertRaises(CapacityExceeded):
            self.w.b.arrive_batch(self.batch, self.sh, arrived_person_ids=self.people)
        # 紧急安置显式放行，全部到达。
        self.w.b.arrive_batch(
            self.batch, self.sh, arrived_person_ids=self.people,
            allow_overflow=True,
        )
        self.assertEqual(
            self.w.b.shelter_occupancy()[self.sh]["total"], 3
        )

    def test_missing_person_marked_no_show(self) -> None:
        big = self.w.b.register_shelter("大点", capacity_total=50)
        arrived = self.people[:2]
        self.w.b.arrive_batch(self.batch, big, arrived_person_ids=arrived)
        statuses = {
            r["person_id"]: r["status"]
            for r in self.w.b.conn.execute(
                "SELECT person_id, status FROM batch_assignments"
            )
        }
        self.assertEqual(statuses[self.people[0]], "arrived")
        self.assertEqual(statuses[self.people[2]], "no_show")
        # 未到者没有产生入住移动。
        loc = self.w.b.current_location(self.people[2])
        self.assertIsNone(loc["shelter_id"])


if __name__ == "__main__":
    unittest.main()
