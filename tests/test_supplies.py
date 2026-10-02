"""物资批次守恒测试：发放、退回、转交、补发全部走只追加台账。"""

import sqlite3
import unittest

from src.shelter import Conflict, InsufficientStock
from tests.support import build_world


class SupplyLedgerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = build_world()
        self.op = self.w.seed_operators()
        self.sh = self.w.b.register_shelter("廊曼安置点", capacity_total=50)
        self.mom = self.w.b.intake_person(
            received_location="寺庙", received_by_team="救援队一队",
            received_by_operator="队员甲", shelter_id=self.sh,
            full_name="มาลี", provisional=True, display_code="P-MOM",
        )
        self.baby = self.w.b.intake_person(
            received_location="寺庙", received_by_team="救援队一队",
            received_by_operator="队员甲", shelter_id=self.sh,
            display_code="P-BABY", is_child=True, age_group="child",
        )
        self.formula = self.w.b.receive_supply_batch(
            "INF-MILK-9", "婴幼儿配方奶", "infant", "罐", 12,
            shelter_id=self.sh, source_team="物资组",
            created_by=self.op["logistics"],
        )
        self.meds = self.w.b.receive_supply_batch(
            "MED-OR-3", "口服补液盐", "medicine", "包", 20,
            shelter_id=self.sh, created_by=self.op["logistics"],
        )

    def test_issue_reduces_batch_and_increases_holding(self) -> None:
        b = self.w.b
        b.issue_supply(self.formula, self.mom, 2, operator_id=self.op["logistics"])
        self.assertEqual(b.batch_balance(self.formula), 10)
        self.assertEqual(b.person_holding(self.mom)[self.formula], 2)
        with self.assertRaises(InsufficientStock):
            b.issue_supply(self.formula, self.mom, 99, operator_id=self.op["logistics"])

    def test_return_restores_balance(self) -> None:
        b = self.w.b
        b.issue_supply(self.meds, self.mom, 4, operator_id=self.op["medical"])
        b.return_supply(self.meds, self.mom, 1, operator_id=self.op["medical"],
                        note="孩子出院，多领的退回")
        self.assertEqual(b.batch_balance(self.meds), 17)
        self.assertEqual(b.person_holding(self.mom)[self.meds], 3)
        with self.assertRaises(Conflict):
            b.return_supply(self.meds, self.mom, 99, operator_id=self.op["medical"])

    def test_transfer_between_persons_conserves_stock(self) -> None:
        b = self.w.b
        b.issue_supply(self.formula, self.mom, 3, operator_id=self.op["logistics"])
        balance_before = b.batch_balance(self.formula)
        out_id, in_id = b.transfer_supply(
            self.formula, self.mom, self.baby, 2,
            operator_id=self.op["logistics"], note="母亲转交给同点婴儿",
        )
        self.assertEqual(b.batch_balance(self.formula), balance_before)
        self.assertEqual(b.person_holding(self.mom)[self.formula], 1)
        self.assertEqual(b.person_holding(self.baby)[self.formula], 2)
        # 两行台账互相引用，原始发放仍在，没有被删除。
        rows = b.conn.execute(
            "SELECT entry_type, COUNT(*) AS n FROM stock_ledger GROUP BY entry_type"
        ).fetchall()
        counts = {r["entry_type"]: r["n"] for r in rows}
        self.assertEqual(counts["issue"], 1)
        self.assertEqual(counts["transfer_out"], 1)
        self.assertEqual(counts["transfer_in"], 1)

    def test_reissue_links_to_original_issue(self) -> None:
        b = self.w.b
        original = b.issue_supply(
            self.formula, self.mom, 2, operator_id=self.op["logistics"],
            note="首次领取",
        )
        # 转运中遗失，补发两罐；库存继续减少，但有补发链路。
        b.reissue_supply(
            self.formula, self.mom, 2, original_ledger_id=original,
            operator_id=self.op["logistics"], note="转运遗失补发",
        )
        self.assertEqual(b.batch_balance(self.formula), 8)
        self.assertEqual(b.person_holding(self.mom)[self.formula], 4)
        link = b.conn.execute(
            "SELECT ref_ledger_id FROM stock_ledger WHERE entry_type='reissue'"
        ).fetchone()
        self.assertEqual(link["ref_ledger_id"], original)
        with self.assertRaises(Exception):
            b.reissue_supply(
                self.formula, self.mom, 1, original_ledger_id="不存在",
                operator_id=self.op["logistics"],
            )

    def test_ledger_is_append_only(self) -> None:
        b = self.w.b
        lid = b.issue_supply(self.meds, self.mom, 1, operator_id=self.op["medical"])
        with self.assertRaises(sqlite3.IntegrityError):
            b.conn.execute("DELETE FROM stock_ledger WHERE id=?", (lid,))
        with self.assertRaises(sqlite3.IntegrityError):
            b.conn.execute(
                "UPDATE stock_ledger SET quantity=999 WHERE id=?", (lid,)
            )


if __name__ == "__main__":
    unittest.main()
