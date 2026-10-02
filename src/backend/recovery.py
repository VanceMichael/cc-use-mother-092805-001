"""重启恢复：四类待办按原计划接续。

应用重启后调用 recover_pending：容量预警、失联匹配、药品补给、
待确认转运全部从持久化的任务表与当前状态重建，重复执行安全（幂等）。
"""

from __future__ import annotations

from typing import Any

from .access import Role
from .service import (
    ShelterService,
    TASK_CAPACITY_ALERT,
    TASK_MEDICINE_RESUPPLY,
    TASK_MISSING_MATCH,
    TASK_TRANSFER_CONFIRMATION,
)

RECOVERY_ACTOR = "system-recovery"


def recover_pending(service: ShelterService) -> dict[str, list[dict[str, Any]]]:
    """按当前状态补齐四类待办并返回全部待办任务。

    - 容量预警：占用达到预警线的安置点重新挂出；
    - 失联匹配：仍未结案的寻找类线索重新比对候选；
    - 药品补给：余量低于补给线的药品批次重新挂出；
    - 待确认转运：已安排未确认的转运继续提醒。
    """
    now = service._now()
    with service.store.tx():
        for shelter in service.store.all("SELECT * FROM shelters WHERE status = 'open'"):
            service._refresh_capacity_alert_tx(shelter["id"], RECOVERY_ACTOR, Role.SUPERVISOR, now)
        for transfer in service.store.all("SELECT * FROM transfers WHERE status = 'planned'"):
            person = service.store.one(
                "SELECT display_name FROM persons WHERE id = ?", (transfer["person_id"],)
            )
            service._ensure_task_tx(
                TASK_TRANSFER_CONFIRMATION,
                "transfer",
                transfer["id"],
                {
                    "person_id": transfer["person_id"],
                    "person_name": person["display_name"] if person else transfer["person_id"],
                    "to_shelter_id": transfer["to_shelter_id"],
                    "summary": f"转运 {transfer['id']} 待确认（重启后续接）",
                },
                now,
            )
        for clue in service.store.all(
            "SELECT * FROM tracing_clues WHERE clue_type = 'seeking_person' AND status = 'open'"
        ):
            service._run_matching_tx(clue["id"])
            service._ensure_task_tx(
                TASK_MISSING_MATCH,
                "clue",
                clue["id"],
                {
                    "target_name": clue["target_name"],
                    "summary": f"寻找 {clue['target_name'] or '未知姓名'} 的线索待比对（重启后续接）",
                },
                now,
            )
        for batch in service.store.all(
            "SELECT * FROM supply_batches WHERE kind = 'medicine' AND resupply_level > 0"
            " AND status = 'active'"
        ):
            service._refresh_medicine_resupply_tx(batch["id"], RECOVERY_ACTOR, Role.SUPERVISOR, now)
    return service.pending_tasks(RECOVERY_ACTOR, Role.SUPERVISOR.value)
