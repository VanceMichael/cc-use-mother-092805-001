"""可重启接续的值班任务调度。

四类任务全部持久化在 scheduled_jobs：
- capacity_warning   容量预警
- match_missing      失联/寻亲线索自动匹配候选
- medication_resupply 药品到期补给
- pending_transfer   待确认/到期转运接续

应用重启后重新构造 Scheduler（或直接 run_due），未执行的任务按原 run_at 继续；
周期任务带 idempotency_key，重启不会重复创建。
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from .clock import now_iso, parse_iso
from .service import Backend
from .store import dumps

DEFAULT_INTERVALS = {
    "capacity_warning": 3600,
    "match_missing": 1800,
    "medication_resupply": 300,
    "pending_transfer": 300,
}


class Scheduler:
    def __init__(self, backend: Backend) -> None:
        self.backend = backend
        self.conn = backend.conn

    # ---------------------------------------------------------- 任务登记

    def schedule(
        self, job_type: str, run_at: str, *,
        interval_seconds: int | None = None,
        payload: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        if idempotency_key:
            row = self.conn.execute(
                "SELECT id FROM scheduled_jobs WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if row:
                return row["id"]
        jid = f"job_{uuid.uuid4().hex[:16]}"
        ts = now_iso(self.backend.clock)
        self.conn.execute(
            """INSERT INTO scheduled_jobs
               (id, job_type, payload, run_at, interval_seconds, status,
                idempotency_key, run_count, created_at, updated_at)
               VALUES (?,?,?,?,?, 'scheduled', ?, 0, ?,?)""",
            (jid, job_type, dumps(payload or {}), run_at, interval_seconds,
             idempotency_key, ts, ts),
        )
        return jid

    def ensure_default_recurring(
        self, intervals: dict[str, int] | None = None, *, start_at: str | None = None
    ) -> dict[str, str]:
        """登记四类默认周期任务；已存在则沿用，重启调用安全。"""
        intervals = {**DEFAULT_INTERVALS, **(intervals or {})}
        first = start_at or now_iso(self.backend.clock)
        ids = {}
        for job_type, seconds in intervals.items():
            ids[job_type] = self.schedule(
                job_type, first, interval_seconds=seconds,
                idempotency_key=f"recurring:{job_type}",
            )
        return ids

    def pending_jobs(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT * FROM scheduled_jobs
               WHERE status IN ('scheduled','failed') ORDER BY run_at"""
        ).fetchall()
        return [dict(r) for r in rows]

    def due_arrangements(self) -> list[dict[str, Any]]:
        """交接视图：委托给 Backend，保证两处口径一致。"""
        return self.backend.due_arrangements()

    # ------------------------------------------------------------- 执行

    def run_due(self, *, commit: bool = True) -> list[dict[str, Any]]:
        """执行所有到期任务。每个任务独立成败，互不影响。

        应用在任务执行中途重启时任务可能停在 running：这里先把它们复位为
        scheduled，使“按原计划接续”在崩溃后仍然成立。
        """
        now = now_iso(self.backend.clock)
        recovered = self.conn.execute(
            """UPDATE scheduled_jobs
               SET status='scheduled',
                   last_error=COALESCE(last_error,'') || ' | 上次执行中断，已复位重试'
               WHERE status='running'"""
        ).rowcount
        if recovered:
            self.conn.commit()
        due = self.conn.execute(
            """SELECT * FROM scheduled_jobs
               WHERE status IN ('scheduled','failed') AND run_at <= ?
               ORDER BY run_at""",
            (now,),
        ).fetchall()
        results: list[dict[str, Any]] = []
        for job in due:
            result = self._run_one(job, now)
            results.append(result)
            if commit:
                self.conn.commit()
        return results

    def _run_one(self, job: sqlite3.Row, now: str) -> dict[str, Any]:
        self.conn.execute(
            "UPDATE scheduled_jobs SET status='running', updated_at=? WHERE id=?",
            (now, job["id"]),
        )
        handler = {
            "capacity_warning": self._job_capacity,
            "match_missing": self._job_match,
            "medication_resupply": self._job_medication,
            "pending_transfer": self._job_pending_transfer,
        }[job["job_type"]]
        try:
            output = handler(json_loads(job["payload"]))
        except Exception as exc:  # 任务失败保留待重试，不让调度整体中断
            self.conn.execute(
                """UPDATE scheduled_jobs
                   SET status='failed', last_error=?, updated_at=? WHERE id=?""",
                (f"{type(exc).__name__}: {exc}", now, job["id"]),
            )
            return {"job_id": job["id"], "job_type": job["job_type"], "ok": False,
                    "error": str(exc)}

        interval = job["interval_seconds"]
        if interval:
            # 周期任务：优先沿原计划时刻续排，错过太多则贴当前时间，保持节奏。
            planned_next = parse_iso(job["run_at"]) + timedelta(seconds=interval)
            if planned_next.isoformat(timespec="seconds") <= now:
                planned_next = parse_iso(now) + timedelta(seconds=interval)
            next_at = planned_next.isoformat(timespec="seconds")
            self.conn.execute(
                """UPDATE scheduled_jobs
                   SET status='scheduled', run_at=?, run_count=run_count+1,
                       last_error=NULL, updated_at=? WHERE id=?""",
                (next_at, now, job["id"]),
            )
        else:
            self.conn.execute(
                """UPDATE scheduled_jobs
                   SET status='done', run_count=run_count+1, last_error=NULL,
                       updated_at=? WHERE id=?""",
                (now, job["id"]),
            )
        return {"job_id": job["id"], "job_type": job["job_type"], "ok": True,
                "output": output}

    # --------------------------------------------------------- 四类任务

    def _job_capacity(self, payload: dict[str, Any]) -> dict[str, Any]:
        findings = self.backend.check_capacity()
        return {"findings": findings}

    def _job_match(self, payload: dict[str, Any]) -> dict[str, Any]:
        matched: list[dict[str, Any]] = []
        leads = self.conn.execute(
            "SELECT id, code FROM tracing_leads WHERE status='new'"
        ).fetchall()
        for lead in leads:
            candidates = self.backend.match_lead(
                lead["id"], min_score=int(payload.get("min_score", 40))
            )
            if not candidates:
                continue
            top = candidates[0]
            dedup = f"lead-candidates:{lead['id']}:{top['person_id']}:{top['score']}"
            self.backend._raise_alert(
                "match_missing", "info", "tracing_lead", lead["id"],
                f"线索 {lead['code']} 出现 {len(candidates)} 个候选，"
                f"最高匹配 {top['score']} 分（{top['display_code']}）",
                detail={"candidates": candidates},
                dedup_key=dedup,
            )
            matched.append({"lead_id": lead["id"], "candidates": candidates})
        return {"leads_with_candidates": len(matched)}

    def _job_medication(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_iso(self.backend.clock)
        due = self.conn.execute(
            """SELECT * FROM medication_regimens
               WHERE status='active' AND next_due_at <= ?""",
            (now,),
        ).fetchall()
        actions = []
        for regimen in due:
            existing = self.conn.execute(
                """SELECT id FROM arrangements
                   WHERE person_id=? AND type='medication_pickup'
                     AND status IN ('planned','awaiting_confirmation','in_progress')
                     AND json_extract(payload, '$.due_at')=?""",
                (regimen["person_id"], regimen["next_due_at"]),
            ).fetchone()
            if existing:
                continue
            location = self.backend.current_location(regimen["person_id"])
            shelter_id = location["shelter_id"]
            stock = None
            if shelter_id:
                stock = self.conn.execute(
                    """SELECT sb.id, sb.code FROM supply_batches sb
                       WHERE sb.shelter_id=? AND sb.item_name=?
                       ORDER BY sb.arrived_at""",
                    (shelter_id, regimen["medicine"]),
                ).fetchall()
                stock = [
                    s for s in stock
                    if self.backend.batch_balance(s["id"]) > 0
                ]
            if stock:
                batch = stock[0]
                self.backend.create_arrangement(
                    regimen["person_id"], "medication_pickup",
                    planned_at=regimen["next_due_at"],
                    from_shelter_id=shelter_id,
                    payload={
                        "regimen_id": regimen["id"],
                        "medicine": regimen["medicine"],
                        "batch_code": batch["code"],
                        "due_at": regimen["next_due_at"],
                    },
                    note="调度器按用药间隔生成的领取安排",
                )
                self.backend._raise_alert(
                    "medication_resupply", "warning", "person",
                    regimen["person_id"],
                    f"{regimen['medicine']} 已到补给时间，本安置点批次 {batch['code']} 可发",
                    detail={"regimen_id": regimen["id"], "batch_code": batch["code"]},
                    dedup_key=f"med-resupply:{regimen['id']}:{regimen['next_due_at']}",
                )
            else:
                self.backend._raise_alert(
                    "medication_resupply", "critical", "person",
                    regimen["person_id"],
                    f"{regimen['medicine']} 到期待补，当前安置点无库存，需要跨点调剂或采购",
                    detail={"regimen_id": regimen["id"], "shelter_id": shelter_id},
                    dedup_key=f"med-resupply:{regimen['id']}:{regimen['next_due_at']}",
                )
            actions.append({"regimen_id": regimen["id"], "in_stock": bool(stock)})
        return {"due_regimens": len(due), "actions": actions}

    def _job_pending_transfer(self, payload: dict[str, Any]) -> dict[str, Any]:
        now = now_iso(self.backend.clock)
        rows = self.conn.execute(
            """SELECT * FROM arrangements
               WHERE status IN ('planned','awaiting_confirmation')
                 AND planned_at <= ? ORDER BY planned_at""",
            (now,),
        ).fetchall()
        surfaced = []
        for arr in rows:
            if arr["status"] == "awaiting_confirmation":
                severity, message = "critical", "待确认转运已过计划时间，需立即核对"
            else:
                severity, message = "warning", "转运已到计划时间，请执行或改期"
            self.backend._raise_alert(
                "pending_transfer", severity, "arrangement", arr["id"], message,
                detail={"arrangement_id": arr["id"], "type": arr["type"],
                        "planned_at": arr["planned_at"]},
                dedup_key=f"pending-transfer:{arr['id']}:{arr['planned_at']}",
            )
            surfaced.append(arr["id"])
        return {"due_arrangements": surfaced}


def json_loads(value: str) -> dict[str, Any]:
    import json

    return json.loads(value or "{}")
