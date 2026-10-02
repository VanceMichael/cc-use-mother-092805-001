"""移动事件（转运/出院/返家/跨区调剂）对未执行安排的联动测试。"""

import unittest

from tests.support import build_world


class MovementCascadeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh_a = self.w.b.register_shelter("A 安置点", capacity_total=10)
        self.sh_b = self.w.b.register_shelter("B 安置点", capacity_total=10)
        self.sh_c = self.w.b.register_shelter("C 安置点", capacity_total=10)
        self.pid = self.w.b.intake_person(
            received_location="村口", received_by_team="救援队一队",
            received_by_operator="队员甲", shelter_id=self.sh_a,
            full_name="สมหญิง", provisional=True,
        )

    def _plans(self) -> dict[str, str]:
        rows = self.w.b.conn.execute(
            "SELECT type, status FROM arrangements"
        ).fetchall()
        return {r["type"]: r["status"] for r in rows}

    def test_transfer_follows_medication_plan_and_cancels_return(self) -> None:
        b = self.w.b
        b.create_arrangement(
            self.pid, "medication_pickup",
            planned_at="2026-10-02T08:00:00+00:00",
            from_shelter_id=self.sh_a, payload={"medicine": "胰岛素"},
            created_by=self.op["staff"],
        )
        b.create_arrangement(
            self.pid, "return_home",
            planned_at="2026-10-03T08:00:00+00:00",
            from_shelter_id=self.sh_a, created_by=self.op["coordinator"],
        )
        b.record_movement(
            self.pid, "transfer", to_shelter_id=self.sh_b,
            reason="A 点医疗条件不足", operator_id=self.op["coordinator"],
        )

        statuses = self._plans()
        # 返家安排被取消；药品领取被新一条接续，旧条目标 superseded。
        self.assertEqual(statuses["return_home"], "cancelled")
        rows = b.conn.execute(
            "SELECT * FROM arrangements WHERE type='medication_pickup' ORDER BY created_at"
        ).fetchall()
        self.assertEqual(rows[0]["status"], "superseded")
        self.assertEqual(rows[1]["status"], "planned")
        self.assertEqual(rows[1]["from_shelter_id"], self.sh_b)
        self.assertEqual(rows[1]["follows_arrangement_id"], rows[0]["id"])

        # 新安排记录了触发事件，链条可追溯。
        self.assertEqual(
            b.current_location(self.pid)["shelter_id"], self.sh_b
        )

    def test_return_home_cancels_transfer_but_keeps_reunion_visit(self) -> None:
        b = self.w.b
        b.create_arrangement(
            self.pid, "transfer",
            planned_at="2026-10-02T08:00:00+00:00",
            to_shelter_id=self.sh_b, created_by=self.op["coordinator"],
        )
        b.create_arrangement(
            self.pid, "reunion_visit",
            planned_at="2026-10-04T08:00:00+00:00",
            to_shelter_id=self.sh_c, created_by=self.op["child"],
        )
        b.record_movement(
            self.pid, "return_home",
            reason="洪水退去，整户返家", operator_id=self.op["coordinator"],
        )
        transfer_status = b.conn.execute(
            "SELECT status FROM arrangements WHERE type='transfer'"
        ).fetchone()["status"]
        self.assertEqual(transfer_status, "cancelled")
        visit_rows = b.conn.execute(
            "SELECT status FROM arrangements WHERE type='reunion_visit' ORDER BY rowid"
        ).fetchall()
        self.assertEqual([r["status"] for r in visit_rows], ["superseded", "planned"])

    def test_capacity_blocks_transfer_unless_override(self) -> None:
        b = self.w.b
        small = b.register_shelter("小安置点", capacity_total=1)
        other = b.intake_person(
            received_location="另一村", received_by_team="救援队二队",
            received_by_operator="队员乙", shelter_id=small,
            display_code="TMP-X1",
        )
        from src.shelter import CapacityExceeded

        with self.assertRaises(CapacityExceeded):
            b.record_movement(
                self.pid, "transfer", to_shelter_id=small,
                operator_id=self.op["coordinator"],
            )
        # 紧急情况主管可显式放行超员，并仍如实记录移动。
        b.record_movement(
            self.pid, "transfer", to_shelter_id=small,
            operator_id=self.op["coordinator"], allow_overflow=True,
        )
        self.assertEqual(b.current_location(self.pid)["shelter_id"], small)


if __name__ == "__main__":
    unittest.main()
