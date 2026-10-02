"""重启恢复：容量预警、失联匹配、药品补给、待确认转运按原计划接续。"""

import tempfile
import unittest
from pathlib import Path

from src.backend import ShelterService, Store, recover_pending

from tests.helpers import FakeClock, SequentialIds, make_service, make_site


class RecoveryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmpdir.name) / "shelter.db")
        self.svc, _ = make_service(self.db_path)
        self.community, self.shelter = make_site(self.svc, capacity=2)
        self.other = self.svc.register_shelter(
            "coord", "coordinator", community_id=self.community, name="二号安置点", capacity=100
        )
        # 容量预警：2/2 住满
        self.p1 = self.svc.register_person("rec", "reception", display_name="A",
                                           age_band="adult", shelter_id=self.shelter)
        self.p2 = self.svc.register_person("rec", "reception", display_name="B",
                                           age_band="adult", shelter_id=self.shelter)
        # 失联匹配
        self.svc.record_clue("rec", "reception", clue_type="seeking_person",
                             detail="寻找老人 Somchai", target_name="Somchai")
        # 药品补给
        self.svc.register_supply_batch("log", "logistics", kind="medicine", item="胰岛素",
                                       quantity=2, unit="支", shelter_id=self.shelter,
                                       resupply_level=2)
        # 待确认转运
        self.svc.plan_transfer("rec", "reception", person_id=self.p1,
                               to_shelter_id=self.other, reason="医疗转运",
                               reason_category="medical")

    def tearDown(self) -> None:
        self.tmpdir.cleanup()

    def _reopen(self) -> ShelterService:
        """模拟应用重启：同一数据库文件重新打开。"""
        return ShelterService(Store(self.db_path), clock=FakeClock(), id_factory=SequentialIds())

    def test_all_four_task_kinds_survive_restart(self) -> None:
        before = self.svc.pending_tasks("sup", "supervisor")
        self.assertEqual(
            set(before),
            {"capacity_alert", "missing_person_match", "medicine_resupply", "transfer_confirmation"},
        )
        reopened = self._reopen()
        recovered = recover_pending(reopened)
        self.assertEqual(set(before), set(recovered))
        self.assertEqual(len(recovered["capacity_alert"]), 1)
        self.assertEqual(len(recovered["transfer_confirmation"]), 1)
        # 任务内容可执行：摘要里能看懂要做什么
        self.assertIn("占用", recovered["capacity_alert"][0]["payload"]["summary"])
        self.assertIn("待确认", recovered["transfer_confirmation"][0]["payload"]["summary"])

    def test_recovery_is_idempotent(self) -> None:
        reopened = self._reopen()
        first = recover_pending(reopened)
        second = recover_pending(reopened)
        self.assertEqual(
            sum(len(v) for v in first.values()), sum(len(v) for v in second.values())
        )

    def test_completed_work_does_not_come_back(self) -> None:
        # 转运确认后重启，待确认转运不再出现
        transfer = self.svc.store.one("SELECT id FROM transfers WHERE status = 'planned'")
        self.svc.confirm_transfer("rec", "reception", transfer_id=transfer["id"])
        reopened = self._reopen()
        recovered = recover_pending(reopened)
        self.assertNotIn("transfer_confirmation", recovered)
        # 容量预警随人员离开解除
        self.assertNotIn("capacity_alert", recovered)

    def test_matching_candidates_rebuilt_after_restart(self) -> None:
        # 通信恢复后同名人员登记，重启恢复时重新比对仍能找到
        self.svc.register_person("rec", "reception", display_name="Somchai",
                                 age_band="elderly", shelter_id=self.other)
        reopened = self._reopen()
        recover_pending(reopened)
        candidates = reopened.store.all("SELECT * FROM match_candidates")
        self.assertEqual(len(candidates), 1)


if __name__ == "__main__":
    unittest.main()
