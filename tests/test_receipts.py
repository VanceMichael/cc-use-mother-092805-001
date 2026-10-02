"""不同队伍相同回执的幂等处理与关键内容冲突测试。"""

import unittest

from src.shelter import ReceiptConflict
from tests.support import build_world


class ReceiptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh = self.w.b.register_shelter("廊曼安置点", capacity_total=50)
        self.payload = {
            "kind": "supply_arrival",
            "batch_code": "INF-DIAPER-2",
            "item_name": "纸尿裤",
            "category": "infant",
            "unit": "包",
            "quantity_in": 30,
            "shelter_code": "廊曼安置点",
            "source_team": "救援队一队",
        }

    def test_same_receipt_from_two_teams_processed_once(self) -> None:
        b = self.w.b
        first = b.ingest_receipt(
            "RCP-1001", "救援队一队", self.payload,
            operator_id=self.op["logistics"],
        )
        second = b.ingest_receipt(
            "RCP-1001", "救援队三队", self.payload,
            operator_id=self.op["rescue3"],
        )
        self.assertEqual(first["status"], "processed")
        self.assertEqual(second["status"], "duplicate")
        # 库存只入一次。
        batch = b.conn.execute(
            "SELECT id FROM supply_batches WHERE code='INF-DIAPER-2'"
        ).fetchone()
        self.assertEqual(b.batch_balance(batch["id"]), 30)
        receipts = b.conn.execute(
            "SELECT status, COUNT(*) AS n FROM receipts WHERE code='RCP-1001' GROUP BY status"
        ).fetchall()
        self.assertEqual({r["status"]: r["n"] for r in receipts},
                         {"processed": 1, "duplicate": 1})

    def test_non_critical_difference_still_duplicate(self) -> None:
        b = self.w.b
        b.ingest_receipt("RCP-1002", "救援队一队", self.payload,
                         operator_id=self.op["logistics"])
        res = b.ingest_receipt(
            "RCP-1002", "救援队二队",
            {**self.payload, "source_team": "救援队二队", "note": "手写补注"},
            operator_id=self.op["rescue2"],
        )
        self.assertEqual(res["status"], "duplicate")
        self.assertEqual(b.open_receipt_conflicts(), [])

    def test_critical_conflict_goes_to_supervisor_queue(self) -> None:
        b = self.w.b
        b.ingest_receipt("RCP-1003", "救援队一队", self.payload,
                         operator_id=self.op["logistics"])
        with self.assertRaises(ReceiptConflict):
            b.ingest_receipt(
                "RCP-1003", "救援队二队",
                {**self.payload, "quantity_in": 8},
                operator_id=self.op["rescue2"],
            )
        conflicts = b.open_receipt_conflicts()
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0]["field"], "quantity_in")
        # 冲突不进账：库存仍是 30。
        batch = b.conn.execute(
            "SELECT id FROM supply_batches WHERE code='INF-DIAPER-2'"
        ).fetchone()
        self.assertEqual(b.batch_balance(batch["id"]), 30)
        # 产生了 critical 预警。
        self.assertTrue(any(
            a["type"] == "receipt_conflict" and a["severity"] == "critical"
            for a in b.open_alerts()
        ))

    def test_only_coordinator_resolves_conflict(self) -> None:
        b = self.w.b
        b.ingest_receipt("RCP-1004", "救援队一队", self.payload,
                         operator_id=self.op["logistics"])
        with self.assertRaises(ReceiptConflict):
            b.ingest_receipt(
                "RCP-1004", "救援队二队",
                {**self.payload, "quantity_in": 8},
                operator_id=self.op["rescue2"],
            )
        from src.shelter import PermissionDenied

        with self.assertRaises(PermissionDenied):
            b.resolve_receipt_conflict(
                b.open_receipt_conflicts()[0]["receipt_id"], "accept_new",
                operator_id=self.op["logistics"],
            )
        # 主管核对后选择维持原账（keep_existing）。
        result = b.resolve_receipt_conflict(
            b.open_receipt_conflicts()[0]["receipt_id"], "keep_existing",
            operator_id=self.op["coordinator"],
        )
        self.assertEqual(result["status"], "duplicate")
        self.assertEqual(b.open_receipt_conflicts(), [])
        self.assertFalse(any(
            a["type"] == "receipt_conflict" for a in b.open_alerts()
        ))

    def test_supervisor_accept_new_applies_payload(self) -> None:
        b = self.w.b
        payload = {**self.payload, "batch_code": "INF-DIAPER-3"}
        b.ingest_receipt("RCP-1005", "救援队一队", payload,
                         operator_id=self.op["logistics"])
        with self.assertRaises(ReceiptConflict):
            b.ingest_receipt(
                "RCP-1005", "救援队二队",
                {**payload, "quantity_in": 40},
                operator_id=self.op["rescue2"],
            )
        receipt_id = b.open_receipt_conflicts()[0]["receipt_id"]
        result = b.resolve_receipt_conflict(
            receipt_id, "accept_new", operator_id=self.op["coordinator"]
        )
        self.assertEqual(result["status"], "processed")
        batch = b.conn.execute(
            "SELECT id FROM supply_batches WHERE code='INF-DIAPER-3'"
        ).fetchone()
        # 主管确认实际到货 40 包：台账以冲抵行修正为 40，而非再入一笔 40。
        self.assertEqual(b.batch_balance(batch["id"]), 40)
        correction = b.conn.execute(
            "SELECT entry_type, quantity, note FROM stock_ledger "
            "WHERE entry_type LIKE 'correction%'"
        ).fetchone()
        self.assertEqual(correction["entry_type"], "correction_in")
        self.assertEqual(correction["quantity"], 10)
        self.assertIn("修正", correction["note"])


if __name__ == "__main__":
    unittest.main()
