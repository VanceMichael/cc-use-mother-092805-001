"""回执幂等与冲突上交主管。"""

import unittest

from src.backend import AccessDeniedError, ConflictError

from tests.helpers import make_service, make_site


class ReceiptIdempotencyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        self.community, self.shelter = make_site(self.svc)
        self.payload = {
            "kind": "food", "item": "饮用水", "quantity": 100,
            "unit": "瓶", "shelter_id": self.shelter,
        }

    def test_same_receipt_processed_only_once(self) -> None:
        first = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队甲", external_id="R-001", payload=self.payload,
        )
        self.assertEqual(first["status"], "applied")
        second = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队甲", external_id="R-001", payload=self.payload,
        )
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(second["result"], first["result"])
        # 只入账一个批次
        batches = self.svc.store.all("SELECT id FROM supply_batches")
        self.assertEqual(len(batches), 1)

    def test_conflicting_receipt_goes_to_supervisor(self) -> None:
        first = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队甲", external_id="R-002", payload=self.payload,
        )
        changed = dict(self.payload, quantity=120)
        conflict = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队甲", external_id="R-002", payload=changed,
        )
        self.assertEqual(conflict["status"], "conflict")
        # 未按冲突内容重复入账
        batches = self.svc.store.all("SELECT id FROM supply_batches")
        self.assertEqual(len(batches), 1)
        batch_id = first["result"]["batch_id"]
        self.assertEqual(
            self.svc.verify_conservation("log", "logistics", batch_id)["on_shelves"], 100
        )
        # 主管核对：按来件补正，数量调整为 120，守恒保持
        self.svc.resolve_conflict(
            "sup", "supervisor", conflict_id=conflict["conflict_id"],
            decision="apply_incoming", note="以仓库签收单为准",
        )
        result = self.svc.verify_conservation("log", "logistics", batch_id)
        self.assertEqual(result["on_shelves"], 120)
        self.assertEqual(result["gained"], 20)
        # 冲突只能处理一次
        with self.assertRaises(ConflictError):
            self.svc.resolve_conflict(
                "sup", "supervisor", conflict_id=conflict["conflict_id"],
                decision="keep_original",
            )

    def test_keep_original_leaves_batch_untouched(self) -> None:
        first = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队乙", external_id="R-003", payload=self.payload,
        )
        conflict = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队乙", external_id="R-003",
            payload=dict(self.payload, quantity=80),
        )
        self.svc.resolve_conflict(
            "sup", "supervisor", conflict_id=conflict["conflict_id"],
            decision="keep_original", note="来件笔误",
        )
        self.assertEqual(
            self.svc.verify_conservation("log", "logistics", first["result"]["batch_id"])["on_shelves"],
            100,
        )

    def test_resolve_conflict_requires_supervisor(self) -> None:
        self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队丙", external_id="R-004", payload=self.payload,
        )
        conflict = self.svc.submit_receipt(
            "log", "logistics", kind="supply_delivery",
            source_team="救援队丙", external_id="R-004",
            payload=dict(self.payload, quantity=90),
        )
        with self.assertRaises(AccessDeniedError):
            self.svc.resolve_conflict(
                "log", "logistics", conflict_id=conflict["conflict_id"],
                decision="keep_original",
            )

    def test_evacuation_arrival_receipt_and_discrepancy(self) -> None:
        batch = self.svc.create_evacuation_batch(
            "rec", "reception", team="救援队丁", source_community_id=self.community,
            dest_shelter_id=self.shelter,
        )
        self.svc.register_person("rec", "reception", display_name="A", age_band="adult",
                                 shelter_id=self.shelter, batch_id=batch)
        payload = {"batch_id": batch, "arrived_headcount": 3}
        first = self.svc.submit_receipt(
            "rec", "reception", kind="evacuation_arrival",
            source_team="救援队丁", external_id="A-100", payload=payload,
        )
        self.assertEqual(first["status"], "applied")
        # 回执 3 人，登记 1 人，差异 2 人记录在案
        self.assertEqual(first["result"]["discrepancy"], 2)
        again = self.svc.submit_receipt(
            "rec", "reception", kind="evacuation_arrival",
            source_team="救援队丁", external_id="A-100", payload=payload,
        )
        self.assertEqual(again["status"], "duplicate")
        row = self.svc.store.one("SELECT * FROM evacuation_batches WHERE id = ?", (batch,))
        self.assertEqual(row["status"], "arrived")
        self.assertEqual(row["arrived_headcount"], 3)


if __name__ == "__main__":
    unittest.main()
