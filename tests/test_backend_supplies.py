"""物资批次守恒：发放、退回、转交、补发、调拨与未执行安排调整。"""

import unittest

from src.backend import ConflictError, ValidationError

from tests.helpers import make_service, make_site


class SupplyConservationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.svc, _ = make_service()
        _, self.shelter = make_site(self.svc)
        self.other = self.svc.register_shelter(
            "coord", "coordinator",
            community_id=self.svc.store.one("SELECT community_id AS c FROM shelters WHERE id = ?",
                                            (self.shelter,))["c"],
            name="二号安置点", capacity=100,
        )
        self.person = self.svc.register_person(
            "rec", "reception", display_name="A", age_band="adult", shelter_id=self.shelter
        )
        self.batch = self.svc.register_supply_batch(
            "log", "logistics", kind="infant", item="婴儿奶粉", quantity=20,
            unit="罐", shelter_id=self.shelter,
        )

    def test_distribute_and_conservation(self) -> None:
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=3
        )
        result = self.svc.verify_conservation("log", "logistics", self.batch)
        self.assertTrue(result["balanced"])
        self.assertEqual(result["distributed_net"], 3)
        self.assertEqual(result["on_shelves"], 17)
        ledger = self.svc.batch_ledger("log", "logistics", self.batch)
        self.assertEqual(ledger["stock_by_shelter"], {self.shelter: 17})
        self.assertEqual(ledger["movements"][0]["id"], movement)

    def test_distribute_beyond_stock_is_rejected(self) -> None:
        with self.assertRaises(ConflictError):
            self.svc.distribute(
                "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=21
            )

    def test_return_restores_stock(self) -> None:
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=5
        )
        self.svc.return_supplies("log", "logistics", movement_id=movement, quantity=2,
                                 reason="家属离点退回")
        result = self.svc.verify_conservation("log", "logistics", self.batch)
        self.assertEqual(result["distributed_net"], 3)
        self.assertEqual(result["on_shelves"], 17)
        with self.assertRaises(ValidationError):
            self.svc.return_supplies("log", "logistics", movement_id=movement, quantity=4,
                                     reason="超出已发数量")

    def test_hand_over_between_persons_keeps_stock(self) -> None:
        other_person = self.svc.register_person(
            "rec", "reception", display_name="B", age_band="adult", shelter_id=self.shelter
        )
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=4
        )
        self.svc.hand_over("log", "logistics", movement_id=movement,
                           to_person_id=other_person, quantity=3, reason="转交同室家属代管")
        result = self.svc.verify_conservation("log", "logistics", self.batch)
        self.assertTrue(result["balanced"])
        self.assertEqual(result["on_shelves"], 16)
        # 转交后剩余可处分数量为 1，再退回 2 应被拒绝
        with self.assertRaises(ValidationError):
            self.svc.return_supplies("log", "logistics", movement_id=movement, quantity=2,
                                     reason="数量不足")

    def test_hand_over_cannot_exceed_remaining_after_return(self) -> None:
        other_person = self.svc.register_person(
            "rec", "reception", display_name="B", age_band="adult", shelter_id=self.shelter
        )
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=4
        )
        self.svc.return_supplies("log", "logistics", movement_id=movement, quantity=3,
                                 reason="部分退回")
        with self.assertRaises(ValidationError):
            self.svc.hand_over("log", "logistics", movement_id=movement,
                               to_person_id=other_person, quantity=2, reason="超出可处分")

    def test_reissue_links_to_original_distribution(self) -> None:
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=2
        )
        reissued = self.svc.reissue("log", "logistics", movement_id=movement, quantity=2,
                                    reason="转运途中遗失，到达后补发")
        result = self.svc.verify_conservation("log", "logistics", self.batch)
        self.assertEqual(result["distributed_net"], 4)
        ledger = self.svc.batch_ledger("log", "logistics", self.batch)
        reissue_rows = [m for m in ledger["movements"] if m["movement_type"] == "reissue"]
        self.assertEqual(reissue_rows[0]["linked_movement_id"], movement)
        self.assertEqual(reissue_rows[0]["id"], reissued)

    def test_transfer_stock_moves_between_shelters(self) -> None:
        self.svc.transfer_stock("log", "logistics", batch_id=self.batch,
                                to_shelter_id=self.other, quantity=6, reason="跨点调剂")
        ledger = self.svc.batch_ledger("log", "logistics", self.batch)
        self.assertEqual(
            ledger["stock_by_shelter"], {self.shelter: 14, self.other: 6}
        )
        self.assertTrue(self.svc.verify_conservation("log", "logistics", self.batch)["balanced"])

    def test_ledger_is_append_only(self) -> None:
        movement = self.svc.distribute(
            "log", "logistics", batch_id=self.batch, person_id=self.person, quantity=2
        )
        self.svc.return_supplies("log", "logistics", movement_id=movement, quantity=1,
                                 reason="退回")
        ledger = self.svc.batch_ledger("log", "logistics", self.batch)
        types = [m["movement_type"] for m in ledger["movements"]]
        # 发放与退回都保留在台账中，不存在删除
        self.assertEqual(types, ["distribute", "return"])


class PlannedDistributionAdjustmentTest(unittest.TestCase):
    """转运、出院、返家、跨区调剂时，尚未执行的安排随之调整。"""

    def setUp(self) -> None:
        self.svc, _ = make_service()
        self.community, self.shelter = make_site(self.svc)
        self.other = self.svc.register_shelter(
            "coord", "coordinator", community_id=self.community, name="二号安置点", capacity=100
        )
        self.person = self.svc.register_person(
            "rec", "reception", display_name="A", age_band="adult", shelter_id=self.shelter
        )
        self.batch = self.svc.register_supply_batch(
            "log", "logistics", kind="medicine", item="降压药", quantity=10,
            unit="盒", shelter_id=self.shelter,
        )

    def _planned(self) -> str:
        return self.svc.plan_distribution(
            "med", "medical", batch_id=self.batch, person_id=self.person, quantity=1,
            reason="夜间定时药",
        )

    def _status(self, movement_id: str) -> str:
        return self.svc.store.one(
            "SELECT status FROM supply_movements WHERE id = ?", (movement_id,)
        )["status"]

    def test_planned_distribution_executes_normally(self) -> None:
        movement = self._planned()
        self.svc.execute_planned_distribution("med", "medical", movement_id=movement)
        self.assertEqual(self._status(movement), "completed")
        self.assertEqual(
            self.svc.verify_conservation("med", "medical", self.batch)["distributed_net"], 1
        )

    def test_transfer_cancels_planned_distribution(self) -> None:
        movement = self._planned()
        transfer = self.svc.plan_transfer("rec", "reception", person_id=self.person,
                                          to_shelter_id=self.other, reason="医疗转运",
                                          reason_category="medical")
        self.svc.confirm_transfer("rec", "reception", transfer_id=transfer)
        self.assertEqual(self._status(movement), "cancelled")
        self.assertEqual(
            self.svc.verify_conservation("med", "medical", self.batch)["distributed_net"], 0
        )

    def test_discharge_cancels_planned_distribution_and_transfer(self) -> None:
        movement = self._planned()
        transfer = self.svc.plan_transfer("rec", "reception", person_id=self.person,
                                          to_shelter_id=self.other, reason="观察",
                                          reason_category="medical")
        self.svc.discharge_person("med", "medical", person_id=self.person, reason="治疗后出院")
        self.assertEqual(self._status(movement), "cancelled")
        self.assertEqual(
            self.svc.store.one("SELECT status FROM transfers WHERE id = ?", (transfer,))["status"],
            "cancelled",
        )
        # 出院后床位释放
        self.assertEqual(
            self.svc.store.one("SELECT occupancy FROM shelters WHERE id = ?", (self.shelter,))["occupancy"],
            0,
        )

    def test_return_home_cancels_planned_distribution(self) -> None:
        movement = self._planned()
        self.svc.return_home("rec", "reception", person_id=self.person, reason="水位退去返家")
        self.assertEqual(self._status(movement), "cancelled")

    def test_reallocation_moves_group_and_adjusts(self) -> None:
        second = self.svc.register_person(
            "rec", "reception", display_name="B", age_band="adult", shelter_id=self.shelter
        )
        movement = self._planned()
        transfers = self.svc.reallocate(
            "coord", "coordinator", from_shelter_id=self.shelter, to_shelter_id=self.other,
            person_ids=[self.person, second], reason="安置点修缮，跨区调剂",
        )
        self.assertEqual(len(transfers), 2)
        for transfer_id in transfers:
            self.svc.confirm_transfer("rec", "reception", transfer_id=transfer_id)
        self.assertEqual(self._status(movement), "cancelled")
        self.assertEqual(
            self.svc.store.one("SELECT occupancy FROM shelters WHERE id = ?", (self.other,))["occupancy"],
            2,
        )


class MedicineResupplyTest(unittest.TestCase):
    def test_low_stock_raises_and_recovers_task(self) -> None:
        svc, _ = make_service()
        _, shelter = make_site(svc)
        person = svc.register_person("rec", "reception", display_name="A", age_band="adult",
                                     shelter_id=shelter)
        batch = svc.register_supply_batch(
            "log", "logistics", kind="medicine", item="胰岛素", quantity=5,
            unit="支", shelter_id=shelter, resupply_level=3,
        )
        svc.distribute("med", "medical", batch_id=batch, person_id=person, quantity=2)
        tasks = svc.pending_tasks("sup", "supervisor")
        self.assertEqual(len(tasks.get("medicine_resupply", [])), 1)
        # 退回使余量回到补给线以上，任务自动办结
        movement = svc.store.one(
            "SELECT id FROM supply_movements WHERE batch_id = ? AND movement_type = 'distribute'",
            (batch,),
        )
        svc.return_supplies("med", "medical", movement_id=movement["id"], quantity=2,
                            reason="医嘱调整退回")
        tasks = svc.pending_tasks("sup", "supervisor")
        self.assertEqual(tasks.get("medicine_resupply", []), [])


if __name__ == "__main__":
    unittest.main()
