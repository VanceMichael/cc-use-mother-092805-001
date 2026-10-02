"""临时档案、身份核验与合并溯源测试。"""

import sqlite3
import unittest

from src.shelter import Conflict, NotFound
from tests.support import build_world


class IntakeMergeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh = self.w.b.register_shelter("廊曼安置点", capacity_total=50)

    def test_unidentified_gets_temp_profile(self) -> None:
        pid = self.w.b.intake_person(
            received_location="巴吞他尼寺庙",
            received_by_team="救援队一队",
            received_by_operator="队员甲",
            shelter_id=self.sh,
        )
        row = self.w.b._person(pid)
        self.assertEqual(row["status"], "unidentified")
        self.assertTrue(row["display_code"].startswith("TMP-"))

    def test_merge_keeps_original_reception_facts(self) -> None:
        b = self.w.b
        first = b.intake_person(
            received_location="寺庙", received_by_team="救援队一队",
            received_by_operator="队员甲", received_at="2026-10-01T08:00:00+00:00",
            shelter_id=self.sh, full_name=None, display_code="TMP-AAAA",
        )
        # 通信恢复后另一队重复登记（自称姓名）。
        dup = b.intake_person(
            received_location="学校", received_by_team="救援队二队",
            received_by_operator="队员乙", received_at="2026-10-01T15:00:00+00:00",
            shelter_id=self.sh, full_name="ประยุทธ์", provisional=True,
            display_code="TMP-BBBB",
        )
        vid = b.add_verification(dup, "witness", evidence_ref="邻居指认 W-7",
                                 confidence=95, created_by=self.op["coordinator"])
        b.decide_verification(vid, confirm=True, operator_id=self.op["coordinator"])
        b.merge_persons(first, dup, verification_id=vid,
                        reason="同村邻居与昵称均指向同一人",
                        operator_id=self.op["coordinator"])

        # 旧编号解析到保留档案，被合并档案仍整行可查。
        self.assertEqual(b.canonical_person(dup), first)
        merged_row = b._person(dup)
        self.assertEqual(merged_row["received_location"], "学校")
        self.assertEqual(merged_row["received_by_operator"], "队员乙")
        self.assertEqual(merged_row["merged_into_person_id"], first)

        # 原始接收事实被数据库触发器保护，任何更新尝试都失败。
        with self.assertRaises(sqlite3.IntegrityError):
            b.conn.execute(
                "UPDATE persons SET received_location='被篡改' WHERE id=?", (dup,)
            )

        # 通过被合并旧编号仍能定位到当前所在安置点（台账挂在原档案名下）。
        self.assertEqual(b.current_location(dup)["shelter_id"], self.sh)

    def test_merge_requires_confirmed_verification(self) -> None:
        b = self.w.b
        a = b.intake_person(received_location="x", received_by_team="t",
                            received_by_operator="o", shelter_id=self.sh,
                            display_code="TMP-1")
        c = b.intake_person(received_location="y", received_by_team="t",
                            received_by_operator="o", shelter_id=self.sh,
                            display_code="TMP-2")
        with self.assertRaises(Conflict):
            b.merge_persons(a, c, verification_id="不存在的核验", reason="猜的",
                            operator_id=self.op["coordinator"])
        pending = b.add_verification(c, "witness", confidence=30,
                                     created_by=self.op["coordinator"])
        with self.assertRaises(Conflict):
            b.merge_persons(a, c, verification_id=pending, reason="核验未完成",
                            operator_id=self.op["coordinator"])

    def test_ledger_rows_cannot_be_deleted(self) -> None:
        b = self.w.b
        pid = b.intake_person(received_location="x", received_by_team="t",
                              received_by_operator="o", shelter_id=self.sh,
                              display_code="TMP-D")
        batch = b.receive_supply_batch("B-1", "水", "general", "瓶", 5,
                                       shelter_id=self.sh,
                                       created_by=self.op["logistics"])
        with self.assertRaises(sqlite3.IntegrityError):
            b.conn.execute("DELETE FROM supply_batches WHERE id=?", (batch,))
        with self.assertRaises(sqlite3.IntegrityError):
            b.conn.execute("DELETE FROM persons WHERE id=?", (pid,))


if __name__ == "__main__":
    unittest.main()
