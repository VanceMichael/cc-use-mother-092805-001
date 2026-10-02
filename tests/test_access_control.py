"""儿童、老人、医疗与无障碍资料的分岗访问测试。"""

import unittest

from src.shelter import PermissionDenied
from tests.support import build_world


class AccessControlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh = self.w.b.register_shelter("廊曼安置点", capacity_total=50)
        self.child = self.w.b.intake_person(
            received_location="寺庙", received_by_team="救援队一队",
            received_by_operator="队员甲", shelter_id=self.sh,
            display_code="TMP-CH", is_child=True, age_group="child",
        )
        self.grandpa = self.w.b.intake_person(
            received_location="桥头", received_by_team="救援队二队",
            received_by_operator="队员乙", shelter_id=self.sh,
            display_code="TMP-OLD", is_elder=True, age_group="elder",
        )

    def test_only_medical_role_sees_medical_records(self) -> None:
        b = self.w.b
        b.add_sensitive_record(
            self.grandpa, "medical", {"conditions": ["糖尿病"], "drug": "胰岛素"},
            operator_id=self.op["medical"],
        )
        data = b.read_sensitive_records(
            self.grandpa, "medical",
            operator_id=self.op["medical"], purpose="夜间给药",
        )
        self.assertEqual(data[0]["data"]["drug"], "胰岛素")

        for viewer in ("coordinator", "staff", "logistics", "rescue1", "child"):
            with self.assertRaises(PermissionDenied):
                b.read_sensitive_records(
                    self.grandpa, "medical",
                    operator_id=self.op[viewer], purpose="查看",
                )

    def test_child_records_limited_to_child_protection(self) -> None:
        b = self.w.b
        b.add_sensitive_record(
            self.child, "child", {"guardian_status": "失联", "school": "วัดโรงเรียน"},
            operator_id=self.op["child"],
        )
        self.assertTrue(b.read_sensitive_records(
            self.child, "child", operator_id=self.op["child"], purpose="团聚核实"
        ))
        with self.assertRaises(PermissionDenied):
            b.read_sensitive_records(
                self.child, "child", operator_id=self.op["medical"], purpose="看病顺便看"
            )

    def test_denied_access_is_logged(self) -> None:
        b = self.w.b
        with self.assertRaises(PermissionDenied):
            b.read_sensitive_records(
                self.child, "child", operator_id=self.op["rescue1"], purpose="找人"
            )
        row = b.conn.execute(
            """SELECT * FROM access_log WHERE person_id=? AND category='child'""",
            (self.child,),
        ).fetchone()
        self.assertFalse(row["granted"])
        self.assertEqual(row["purpose"], "找人")

    def test_shelter_staff_sees_accessibility_but_not_medical(self) -> None:
        b = self.w.b
        b.add_sensitive_record(
            self.grandpa, "accessibility", {"needs": "轮椅通道", "wheelchair": True},
            operator_id=self.op["staff"],
        )
        rec = b.read_sensitive_records(
            self.grandpa, "accessibility",
            operator_id=self.op["staff"], purpose="安排一层床位",
        )
        self.assertTrue(rec[0]["data"]["wheelchair"])
        # 长者照护岗也能看无障碍，医疗岗不能。
        self.assertTrue(b.read_sensitive_records(
            self.grandpa, "accessibility",
            operator_id=self.op["elder"], purpose="陪护安排",
        ))
        with self.assertRaises(PermissionDenied):
            b.read_sensitive_records(
                self.grandpa, "accessibility",
                operator_id=self.op["medical"], purpose="想看",
            )


if __name__ == "__main__":
    unittest.main()
