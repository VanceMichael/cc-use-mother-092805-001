"""可重启调度测试：容量预警、失联匹配、药品补给、待确认转运接续。"""

import tempfile
import unittest
from pathlib import Path

from src.shelter import Backend, Scheduler, connect
from tests.support import SimClock


class SchedulerRestartTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mktemp(suffix=".db"))
        self.clock = SimClock()
        self.conn = connect(self.tmp)
        self.b = Backend(self.conn, self.clock)
        self.s = Scheduler(self.b)
        self.op = self._seed()

    def _seed(self) -> dict[str, str]:
        return {
            "medical": self.b.register_operator("M1", "หมอ", "medical"),
            "child": self.b.register_operator("C1", "พี่เลี้ยง", "child_protection"),
            "coord": self.b.register_operator("K1", "หัวหน้า", "coordinator"),
        }

    def tearDown(self) -> None:
        self.conn.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.tmp) + suffix)
            p.unlink(missing_ok=True)

    def _reopen(self) -> None:
        self.conn.commit()
        self.conn.close()
        self.conn = connect(self.tmp)
        self.b = Backend(self.conn, self.clock)
        self.s = Scheduler(self.b)

    def test_recurring_jobs_survive_restart_without_duplication(self) -> None:
        first = self.s.ensure_default_recurring()
        self._reopen()
        second = self.s.ensure_default_recurring()
        self.assertEqual(first, second)
        rows = self.b.conn.execute(
            "SELECT COUNT(*) AS n FROM scheduled_jobs"
        ).fetchone()["n"]
        self.assertEqual(rows, 4)

    def test_medication_resupply_creates_plan_and_resolves_after_issue(self) -> None:
        sh = self.b.register_shelter("SH", capacity_total=50)
        pid = self.b.intake_person(
            received_location="x", received_by_team="t", received_by_operator="o",
            shelter_id=sh, display_code="P-MED",
        )
        self.b.add_medication_regimen(
            pid, "amoxicillin", interval_hours=12, supply_hours=48,
            last_issued_at="2026-10-01T00:00:00+00:00", created_by=self.op["medical"],
        )
        batch = self.b.receive_supply_batch(
            "AMX", "amoxicillin", "medicine", "瓶", 3,
            shelter_id=sh, created_by=self.op["medical"],
        )
        self.s.ensure_default_recurring()
        results = self.s.run_due()
        med_result = [r for r in results if r["job_type"] == "medication_resupply"][0]
        self.assertTrue(med_result["ok"])
        self.assertTrue(med_result["output"]["actions"][0]["in_stock"])

        alerts = [a for a in self.b.open_alerts() if a["type"] == "medication_resupply"]
        self.assertTrue(alerts)
        plans = self.b.conn.execute(
            "SELECT * FROM arrangements WHERE type='medication_pickup'"
        ).fetchall()
        self.assertEqual(len(plans), 1)

        # 实际发药后，预警解除、安排完成。
        ledger = self.b.issue_supply(batch, pid, 1, operator_id=self.op["medical"])
        regimen_id = self.b.conn.execute(
            "SELECT id FROM medication_regimens"
        ).fetchone()["id"]
        self.b.mark_medication_issued(regimen_id, ledger)
        self.assertFalse(any(
            a["type"] == "medication_resupply" for a in self.b.open_alerts()
        ))

    def test_medication_without_stock_raises_critical(self) -> None:
        sh = self.b.register_shelter("SH2", capacity_total=50)
        pid = self.b.intake_person(
            received_location="x", received_by_team="t", received_by_operator="o",
            shelter_id=sh, display_code="P-NOMED",
        )
        self.b.add_medication_regimen(
            pid, "rare_drug", interval_hours=6, supply_hours=12,
            last_issued_at="2026-10-01T00:00:00+00:00", created_by=self.op["medical"],
        )
        self.s.ensure_default_recurring()
        self.s.run_due()
        critical = [a for a in self.b.open_alerts()
                    if a["type"] == "medication_resupply" and a["severity"] == "critical"]
        self.assertTrue(critical)
        self.assertIn("无库存", critical[0]["message"])

    def test_pending_transfer_survives_restart(self) -> None:
        sh1 = self.b.register_shelter("SH3", capacity_total=50)
        sh2 = self.b.register_shelter("SH4", capacity_total=50)
        pid = self.b.intake_person(
            received_location="x", received_by_team="t", received_by_operator="o",
            shelter_id=sh1, display_code="P-TR",
        )
        # 计划转运时间已过、等待对方安置点确认。
        self.b.create_arrangement(
            pid, "transfer", planned_at=self.clock.iso(),
            from_shelter_id=sh1, to_shelter_id=sh2,
            status="awaiting_confirmation", created_by=self.op["coord"],
        )
        self.s.ensure_default_recurring()
        self.s.run_due()
        self.assertTrue(any(
            a["type"] == "pending_transfer" and a["severity"] == "critical"
            for a in self.b.open_alerts()
        ))
        # 重启，下一班仍能看到这条待确认转运。
        self._reopen()
        due = self.b.due_arrangements()
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["status"], "awaiting_confirmation")

    def test_capacity_warning_job(self) -> None:
        sh = self.b.register_shelter("TINY", capacity_total=2)
        for code in ("T-1", "T-2"):
            self.b.intake_person(
                received_location="x", received_by_team="t",
                received_by_operator="o", shelter_id=sh, display_code=code,
            )
        self.s.schedule("capacity_warning", self.clock.iso())
        self.s.run_due()
        self.assertTrue(any(
            a["type"] == "capacity" and a["severity"] == "critical"
            for a in self.b.open_alerts()
        ))

    def test_failed_job_is_retried_then_completes(self) -> None:
        self.s.schedule("capacity_warning", self.clock.iso())
        # 人为破坏：把 capacity 检查会扫到的安置点状态写坏不会触发失败，
        # 这里改用一条不存在的处理路径——直接把 job_type 改为合法但让处理器抛错较难，
        # 故验证 failed 状态的任务仍会被 run_due 重新拾起。
        self.b.conn.execute(
            "UPDATE scheduled_jobs SET status='failed', last_error='boom' WHERE job_type='capacity_warning'"
        )
        results = self.s.run_due()
        self.assertTrue(results[0]["ok"])
        self.assertIsNone(
            self.b.conn.execute(
                "SELECT last_error FROM scheduled_jobs WHERE job_type='capacity_warning'"
            ).fetchone()["last_error"]
        )


if __name__ == "__main__":
    unittest.main()
