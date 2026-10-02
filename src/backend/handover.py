"""交接班报告：家庭经历还原与班次待办摘要。

family_story 回答下一班最关心的三件事：家庭为何被拆分、各成员当时
在哪里、最后依据哪些线索团聚；shift_handover 汇总全站待办。
"""

from __future__ import annotations

import json
from typing import Any

from .access import Role, require_role
from .service import (
    ShelterService,
    TASK_CAPACITY_ALERT,
    TASK_MEDICINE_RESUPPLY,
    TASK_MISSING_MATCH,
    TASK_TRANSFER_CONFIRMATION,
)

_ALL_STAFF = frozenset(Role)


def _shelter_names(service: ShelterService) -> dict[str, str]:
    return {row["id"]: row["name"] for row in service.store.all("SELECT id, name FROM shelters")}


def _shelter_label(names: dict[str, str], shelter_id: str | None) -> str:
    if shelter_id is None:
        return "（不在安置点）"
    return names.get(shelter_id, shelter_id)


def family_story(service: ShelterService, actor: str, role: str, family_id: str) -> dict[str, Any]:
    """还原一个家庭从拆分到团聚的完整经历。"""
    require_role(role, _ALL_STAFF, "查看家庭经历")
    family = service._family(family_id)
    members = service._family_members_tx(family_id)
    member_ids = [m["id"] for m in members]
    merged_predecessors = service.store.all(
        "SELECT * FROM persons WHERE merged_into IN (%s)" % ",".join("?" for _ in member_ids),
        tuple(member_ids),
    ) if member_ids else []
    names = _shelter_names(service)

    member_views = []
    for m in members:
        member_views.append(
            {
                "person_id": m["id"],
                "display_name": m["display_name"],
                "relation": next(
                    row["relation"]
                    for row in service.store.all(
                        "SELECT relation FROM family_members WHERE family_id = ? AND person_id = ?",
                        (family_id, m["id"]),
                    )
                ),
                "state": m["state"],
                "current_shelter": _shelter_label(names, m["current_shelter_id"]),
                "reception": {
                    "shelter": _shelter_label(names, m["registered_shelter_id"]),
                    "at": m["registered_at"],
                    "by": m["registered_by"],
                    "batch_id": m["batch_id"],
                },
                "merged_aliases": [p["id"] for p in merged_predecessors if p["merged_into"] == m["id"]],
            }
        )

    # 相关事件：成员个人、家庭整体、成员转运、相关线索
    subject_pairs: list[tuple[str, str]] = [("family", family_id)]
    subject_pairs += [("person", pid) for pid in member_ids]
    subject_pairs += [("person", p["id"]) for p in merged_predecessors]
    transfer_ids = [
        row["id"]
        for row in service.store.all(
            "SELECT id FROM transfers WHERE person_id IN (%s)" % ",".join("?" for _ in member_ids),
            tuple(member_ids),
        )
    ] if member_ids else []
    subject_pairs += [("transfer", tid) for tid in transfer_ids]
    clue_ids = service._confirmed_clues_for_members_tx(members)
    open_clue_rows = service.store.all(
        "SELECT id FROM tracing_clues WHERE subject_person_id IN (%s)"
        % ",".join("?" for _ in member_ids),
        tuple(member_ids),
    ) if member_ids else []
    subject_pairs += [("clue", c) for c in {*clue_ids, *(r["id"] for r in open_clue_rows)}]

    events = _events_for_subjects(service, subject_pairs)
    timeline = [
        {
            "at": e["created_at"],
            "type": e["type"],
            "actor": e["actor"],
            "summary": _summarize_event(e, names),
        }
        for e in events
    ]

    split_events = [
        {"at": e["created_at"], "reason": e["payload"].get("reason"),
         "trigger": e["payload"].get("trigger"),
         "member_locations": e["payload"].get("member_locations")}
        for e in events if e["type"] == "family_split"
    ]
    reunifications = []
    for e in events:
        if e["type"] != "family_reunited":
            continue
        clues = []
        for clue_id in e["payload"].get("clue_ids", []):
            clue = service.store.one("SELECT * FROM tracing_clues WHERE id = ?", (clue_id,))
            if clue:
                clues.append(
                    {
                        "clue_id": clue["id"],
                        "clue_type": clue["clue_type"],
                        "detail": clue["detail"],
                        "confirmed_by": clue["confirmed_by"],
                        "confirmed_at": clue["confirmed_at"],
                    }
                )
        reunifications.append({"at": e["created_at"], "clues": clues})

    return {
        "family_id": family_id,
        "family_name": family["name"],
        "status": family["status"],
        "members": member_views,
        "split_events": split_events,
        "reunifications": reunifications,
        "timeline": timeline,
    }


def _events_for_subjects(
    service: ShelterService, pairs: list[tuple[str, str]]
) -> list[dict[str, Any]]:
    if not pairs:
        return []
    clause = " OR ".join("(subject_type = ? AND subject_id = ?)" for _ in pairs)
    params: list[str] = []
    for pair in pairs:
        params.extend(pair)
    rows = service.store.all(
        f"SELECT * FROM events WHERE {clause} ORDER BY id", tuple(params)
    )
    for row in rows:
        row["payload"] = json.loads(row["payload"])
    return rows


_EVENT_SUMMARY = {
    "person_registered": "接收登记",
    "identity_step_recorded": "身份核验",
    "person_verified": "身份确认",
    "persons_merged": "档案合并（保留方）",
    "person_merged_away": "档案合并（被并方）",
    "family_created": "建立家庭档案",
    "family_member_added": "家庭成员加入",
    "family_split": "家庭被拆分",
    "family_reunited": "家庭团聚",
    "clue_recorded": "登记线索",
    "clue_confirmed": "线索确认",
    "clue_rejected": "线索驳回",
    "transfer_planned": "安排转运",
    "transfer_confirmed": "转运到达",
    "transfer_cancelled": "转运取消",
    "person_discharged": "出院离点",
    "person_returned_home": "返家",
    "need_recorded": "记录需求",
    "supply_distribution_cancelled": "未执行发放取消",
}


def _summarize_event(event: dict[str, Any], names: dict[str, str]) -> str:
    label = _EVENT_SUMMARY.get(event["type"], event["type"])
    payload = event["payload"]
    extras: list[str] = []
    if event["type"] == "person_registered":
        extras.append("接收点=" + _shelter_label(names, payload.get("shelter_id")))
    elif event["type"] in {"transfer_planned", "transfer_confirmed"}:
        extras.append(
            f"{_shelter_label(names, payload.get('from_shelter_id'))}"
            f"→{_shelter_label(names, payload.get('to_shelter_id'))}"
        )
        if payload.get("reason"):
            extras.append("原因=" + str(payload["reason"]))
    elif event["type"] == "family_split" and payload.get("reason"):
        extras.append("原因=" + str(payload["reason"]))
    elif event["type"] == "persons_merged":
        extras.append(f"被并档案={payload.get('merged_id')}")
    elif event["type"] in {"person_discharged", "person_returned_home"} and payload.get("reason"):
        extras.append("原因=" + str(payload["reason"]))
    return "；".join([label, *extras]) if extras else label


def shift_handover(service: ShelterService, actor: str, role: str) -> dict[str, Any]:
    """班次交接摘要：待办、冲突、预警、未确认转运、药品补给、近日团聚。"""
    require_role(role, _ALL_STAFF, "查看交接摘要")
    names = _shelter_names(service)
    tasks = service.pending_tasks(actor, role)
    open_conflicts = service.store.all(
        "SELECT * FROM conflicts WHERE status = 'open' ORDER BY created_at"
    )
    planned_transfers = [
        {
            **row,
            "from_shelter": _shelter_label(names, row["from_shelter_id"]),
            "to_shelter": _shelter_label(names, row["to_shelter_id"]),
        }
        for row in service.store.all("SELECT * FROM transfers WHERE status = 'planned'")
    ]
    open_clues = service.store.all(
        "SELECT id, clue_type, target_name, detail, created_at FROM tracing_clues"
        " WHERE status = 'open' ORDER BY created_at"
    )
    recent_reunifications = service.store.all(
        "SELECT subject_id, payload, created_at FROM events WHERE type = 'family_reunited'"
        " ORDER BY id DESC LIMIT 20"
    )
    capacity_alerts = [
        {
            "shelter_id": t["subject_id"],
            "shelter": _shelter_label(names, t["subject_id"]),
            "detail": t["payload"],
        }
        for t in tasks.get(TASK_CAPACITY_ALERT, [])
    ]
    return {
        "generated_at": service._now(),
        "pending_tasks": tasks,
        "capacity_alerts": capacity_alerts,
        "medicine_resupply": tasks.get(TASK_MEDICINE_RESUPPLY, []),
        "missing_person_matches": tasks.get(TASK_MISSING_MATCH, []),
        "transfer_confirmations": tasks.get(TASK_TRANSFER_CONFIRMATION, []),
        "open_conflicts": open_conflicts,
        "planned_transfers": planned_transfers,
        "open_clues": open_clues,
        "recent_reunifications": recent_reunifications,
    }
