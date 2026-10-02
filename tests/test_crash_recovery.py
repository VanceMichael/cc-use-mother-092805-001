"""任务在 running 状态时应用被杀，重启后必须按原计划接续。"""

import unittest

from tests.support import build_world


class CrashRecoveryTest(unittest.TestCase):
    def test_running_job_is_resumed_after_restart(self) -> None:
        w = build_world()
        jid = w.s.schedule("capacity_warning", w.clock.iso())
        # 模拟应用在任务执行中崩溃：状态停在 running。
        w.conn.execute(
            "UPDATE scheduled_jobs SET status='running' WHERE id=?", (jid,)
        )
        w.commit()
        results = w.s.run_due()
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        # 周期任务（未设间隔的为一次性）此处为一次性，执行后 done。
        row = w.conn.execute(
            "SELECT status, run_count FROM scheduled_jobs WHERE id=?", (jid,)
        ).fetchone()
        self.assertEqual(row["status"], "done")
        self.assertEqual(row["run_count"], 1)

    def test_recurring_running_job_keeps_schedule(self) -> None:
        w = build_world()
        jid = w.s.schedule("capacity_warning", w.clock.iso(), interval_seconds=3600)
        w.conn.execute(
            "UPDATE scheduled_jobs SET status='running' WHERE id=?", (jid,)
        )
        w.s.run_due()
        row = w.conn.execute(
            "SELECT status, run_count, run_at FROM scheduled_jobs WHERE id=?", (jid,)
        ).fetchone()
        self.assertEqual(row["status"], "scheduled")
        self.assertEqual(row["run_count"], 1)
        self.assertTrue(row["run_at"] > w.clock.iso())


if __name__ == "__main__":
    unittest.main()
