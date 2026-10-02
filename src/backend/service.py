"""安置与家庭团聚领域服务。

覆盖夜班交接最棘手的几类问题：

- 同一家人被多支救援队分别送走：家庭拆分事件记录原因与各成员去向；
- 无证件人员重复登记：先建临时档案，线索确认后再合并，原接收地点、
  时间和经办人继续可查；
- 物资发出后去向不明：发放/退回/转交/补发全部入账，批次守恒，禁止删除；
- 不同队伍重复回执：幂等处理，关键内容不一致交给主管核对；
- 转运、出院、返家、跨区调剂：未执行的安排随之调整，已发物资走平衡流程。
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any, Callable

from .access import (
    Role,
    Sensitivity,
    require_role,
    require_sensitivity_access,
    sensitivity_for_age_band,
    visible_sensitivities,
)
from .errors import (
    AccessDeniedError,
    ConflictError,
    ConservationError,
    NotFoundError,
    ValidationError,
)
from .store import Store

# 容量预警线：占用达到容量九成即预警
CAPACITY_ALERT_RATIO = 0.9

# 待办任务类型（重启后按原计划接续的四类）
TASK_CAPACITY_ALERT = "capacity_alert"
TASK_MISSING_MATCH = "missing_person_match"
TASK_MEDICINE_RESUPPLY = "medicine_resupply"
TASK_TRANSFER_CONFIRMATION = "transfer_confirmation"

# 需求类别 -> 默认敏感度
_NEED_CATEGORY_SENSITIVITY: dict[str, Sensitivity] = {
    "health": Sensitivity.MEDICAL,
    "mobility": Sensitivity.MEDICAL,
    "infant_care": Sensitivity.CHILD,
    "child_welfare": Sensitivity.CHILD,
    "elder_care": Sensitivity.ELDERLY,
    "other": Sensitivity.GENERAL,
}

_STAFF_ROLES = frozenset(Role)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _default_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _normalize_name(name: str) -> str:
    return "".join(name.split()).lower()


def _payload_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


class ShelterService:
    """领域服务入口；所有写操作在单事务内同时更新状态表与事件日志。"""

    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], datetime] | None = None,
        id_factory: Callable[[str], str] | None = None,
        capacity_alert_ratio: float = CAPACITY_ALERT_RATIO,
    ):
        self.store = store
        self.clock = clock or _utcnow
        self.id_factory = id_factory or _default_id
        self.capacity_alert_ratio = capacity_alert_ratio

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _now(self) -> str:
        return self.clock().isoformat()

    def _new_id(self, prefix: str) -> str:
        return self.id_factory(prefix)

    def _emit(
        self,
        event_type: str,
        *,
        actor: str,
        role: Role,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
        now: str,
    ) -> int:
        return self.store.append_event(
            event_type,
            actor=actor,
            role=role.value,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload,
            created_at=now,
        )

    def _get(self, table: str, row_id: str, label: str) -> dict[str, Any]:
        row = self.store.one(f"SELECT * FROM {table} WHERE id = ?", (row_id,))
        if row is None:
            raise NotFoundError(f"{label}不存在: {row_id}")
        return row

    def _person(self, person_id: str) -> dict[str, Any]:
        return self._get("persons", person_id, "人员")

    def _shelter(self, shelter_id: str) -> dict[str, Any]:
        return self._get("shelters", shelter_id, "安置点")

    def _family(self, family_id: str) -> dict[str, Any]:
        return self._get("families", family_id, "家庭")

    def _batch(self, batch_id: str) -> dict[str, Any]:
        return self._get("supply_batches", batch_id, "物资批次")

    def _resolve_person_id(self, person_id: str) -> str:
        """沿合并链找到当前有效档案。"""
        seen = set()
        current = person_id
        while True:
            row = self._person(current)
            if not row["merged_into"]:
                return current
            if current in seen:
                raise ConflictError(f"合并链存在环: {person_id}")
            seen.add(current)
            current = row["merged_into"]

    # ------------------------------------------------------------------
    # 待办任务（持久化，重启后续接）
    # ------------------------------------------------------------------

    def _ensure_task_tx(
        self, kind: str, subject_type: str, subject_id: str, payload: dict[str, Any], now: str
    ) -> bool:
        """确保同类同对象的任务存在；已存在则刷新内容，返回是否新建。"""
        existing = self.store.one(
            "SELECT * FROM pending_tasks WHERE kind = ? AND subject_type = ? AND subject_id = ?",
            (kind, subject_type, subject_id),
        )
        if existing is not None:
            if existing["status"] != "pending":
                self.store.conn.execute(
                    "UPDATE pending_tasks SET status = 'pending', payload = ?, updated_at = ? WHERE id = ?",
                    (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
                )
                return True
            self.store.conn.execute(
                "UPDATE pending_tasks SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload, ensure_ascii=False), now, existing["id"]),
            )
            return False
        self.store.conn.execute(
            "INSERT INTO pending_tasks(id, kind, subject_type, subject_id, payload, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)",
            (
                self._new_id("task"),
                kind,
                subject_type,
                subject_id,
                json.dumps(payload, ensure_ascii=False),
                now,
                now,
            ),
        )
        return True

    def _settle_tasks_tx(self, kind: str, subject_type: str, subject_id: str, status: str, now: str) -> None:
        self.store.conn.execute(
            "UPDATE pending_tasks SET status = ?, updated_at = ?"
            " WHERE kind = ? AND subject_type = ? AND subject_id = ? AND status = 'pending'",
            (status, now, kind, subject_type, subject_id),
        )

    def pending_tasks(self, actor: str, role: str) -> dict[str, list[dict[str, Any]]]:
        """按类型列出待办任务，供交接班与恢复使用。"""
        require_role(role, _STAFF_ROLES, "查看待办任务")
        rows = self.store.all(
            "SELECT * FROM pending_tasks WHERE status = 'pending' ORDER BY created_at, id"
        )
        grouped: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            row["payload"] = json.loads(row["payload"])
            grouped.setdefault(row["kind"], []).append(row)
        return grouped

    def complete_task(self, actor: str, role: str, *, task_id: str, note: str | None = None) -> None:
        parsed = require_role(role, _STAFF_ROLES, "办结任务")
        now = self._now()
        with self.store.tx():
            task = self._get("pending_tasks", task_id, "任务")
            if task["status"] != "pending":
                raise ConflictError(f"任务已处于 {task['status']} 状态")
            self.store.conn.execute(
                "UPDATE pending_tasks SET status = 'done', updated_at = ? WHERE id = ?",
                (now, task_id),
            )
            self._emit(
                "task_completed",
                actor=actor,
                role=parsed,
                subject_type=task["subject_type"],
                subject_id=task["subject_id"],
                payload={"task_id": task_id, "kind": task["kind"], "note": note},
                now=now,
            )

    # ------------------------------------------------------------------
    # 社区、安置点、撤离批次
    # ------------------------------------------------------------------

    def register_community(self, actor: str, role: str, *, name: str, province: str) -> str:
        parsed = require_role(role, {Role.COORDINATOR, Role.SUPERVISOR}, "登记社区")
        if not name.strip() or not province.strip():
            raise ValidationError("社区名称与府份不能为空")
        now = self._now()
        community_id = self._new_id("com")
        with self.store.tx():
            self.store.conn.execute(
                "INSERT INTO communities(id, name, province, status, created_at) VALUES (?, ?, ?, 'active', ?)",
                (community_id, name.strip(), province.strip(), now),
            )
            self._emit(
                "community_registered",
                actor=actor,
                role=parsed,
                subject_type="community",
                subject_id=community_id,
                payload={"name": name.strip(), "province": province.strip()},
                now=now,
            )
        return community_id

    def register_shelter(
        self, actor: str, role: str, *, community_id: str, name: str, capacity: int
    ) -> str:
        parsed = require_role(role, {Role.COORDINATOR, Role.SUPERVISOR}, "登记安置点")
        if capacity < 0:
            raise ValidationError("容量不能为负")
        now = self._now()
        shelter_id = self._new_id("she")
        with self.store.tx():
            self._get("communities", community_id, "社区")
            self.store.conn.execute(
                "INSERT INTO shelters(id, community_id, name, capacity, occupancy, status, created_at)"
                " VALUES (?, ?, ?, ?, 0, 'open', ?)",
                (shelter_id, community_id, name.strip(), capacity, now),
            )
            self._emit(
                "shelter_registered",
                actor=actor,
                role=parsed,
                subject_type="shelter",
                subject_id=shelter_id,
                payload={"community_id": community_id, "name": name.strip(), "capacity": capacity},
                now=now,
            )
        return shelter_id

    def create_evacuation_batch(
        self,
        actor: str,
        role: str,
        *,
        team: str,
        source_community_id: str,
        dest_shelter_id: str,
        planned_departure: str | None = None,
    ) -> str:
        parsed = require_role(
            role, {Role.RECEPTION, Role.COORDINATOR, Role.SUPERVISOR}, "创建撤离批次"
        )
        now = self._now()
        batch_id = self._new_id("evb")
        with self.store.tx():
            self._get("communities", source_community_id, "社区")
            self._shelter(dest_shelter_id)
            self.store.conn.execute(
                "INSERT INTO evacuation_batches(id, team, source_community_id, dest_shelter_id,"
                " status, planned_departure, created_at) VALUES (?, ?, ?, ?, 'planned', ?, ?)",
                (batch_id, team.strip(), source_community_id, dest_shelter_id, planned_departure, now),
            )
            self._emit(
                "evacuation_batch_created",
                actor=actor,
                role=parsed,
                subject_type="evacuation_batch",
                subject_id=batch_id,
                payload={
                    "team": team.strip(),
                    "source_community_id": source_community_id,
                    "dest_shelter_id": dest_shelter_id,
                    "planned_departure": planned_departure,
                },
                now=now,
            )
        return batch_id

    # ------------------------------------------------------------------
    # 容量预警
    # ------------------------------------------------------------------

    def _refresh_capacity_alert_tx(self, shelter_id: str, actor: str, role: Role, now: str) -> None:
        shelter = self._shelter(shelter_id)
        capacity = shelter["capacity"]
        occupancy = shelter["occupancy"]
        threshold = max(1, math.ceil(capacity * self.capacity_alert_ratio))
        over = occupancy >= threshold
        if capacity == 0:
            over = occupancy > 0
        if over:
            created = self._ensure_task_tx(
                TASK_CAPACITY_ALERT,
                "shelter",
                shelter_id,
                {
                    "shelter_name": shelter["name"],
                    "occupancy": occupancy,
                    "capacity": capacity,
                    "summary": f"安置点 {shelter['name']} 占用 {occupancy}/{capacity}",
                },
                now,
            )
            if created:
                self._emit(
                    "capacity_alert_raised",
                    actor=actor,
                    role=role,
                    subject_type="shelter",
                    subject_id=shelter_id,
                    payload={"occupancy": occupancy, "capacity": capacity},
                    now=now,
                )
        else:
            pending = self.store.one(
                "SELECT id FROM pending_tasks WHERE kind = ? AND subject_type = 'shelter'"
                " AND subject_id = ? AND status = 'pending'",
                (TASK_CAPACITY_ALERT, shelter_id),
            )
            if pending is not None:
                self._settle_tasks_tx(TASK_CAPACITY_ALERT, "shelter", shelter_id, "done", now)
                self._emit(
                    "capacity_alert_cleared",
                    actor=actor,
                    role=role,
                    subject_type="shelter",
                    subject_id=shelter_id,
                    payload={"occupancy": occupancy, "capacity": capacity},
                    now=now,
                )

    def _change_occupancy_tx(
        self, shelter_id: str, delta: int, actor: str, role: Role, now: str
    ) -> None:
        self.store.conn.execute(
            "UPDATE shelters SET occupancy = occupancy + ? WHERE id = ?", (delta, shelter_id)
        )
        row = self._shelter(shelter_id)
        if row["occupancy"] < 0:
            raise ConflictError(f"安置点 {row['name']} 占用数不能为负")
        self._refresh_capacity_alert_tx(shelter_id, actor, role, now)

    # ------------------------------------------------------------------
    # 人员登记与身份核验
    # ------------------------------------------------------------------

    def register_person(
        self,
        actor: str,
        role: str,
        *,
        display_name: str,
        age_band: str,
        shelter_id: str,
        sex: str | None = None,
        batch_id: str | None = None,
        id_document: str | None = None,
        note: str | None = None,
    ) -> str:
        """接收登记。无证件人员建立临时档案；发现疑似重复时自动生成线索。

        原接收地点、时间和经办人写入人员行并同时记入事件，合并后仍可查。
        """
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "接收登记")
        if not display_name.strip():
            raise ValidationError("姓名不能为空")
        if age_band not in {"infant", "child", "teen", "adult", "elderly"}:
            raise ValidationError(f"未知年龄段: {age_band}")
        now = self._now()
        person_id = self._new_id("per")
        sensitivity = sensitivity_for_age_band(age_band)
        is_temp = id_document is None
        with self.store.tx():
            shelter = self._shelter(shelter_id)
            if shelter["status"] != "open":
                raise ConflictError(f"安置点 {shelter['name']} 未开放")
            if batch_id is not None:
                self._get("evacuation_batches", batch_id, "撤离批次")
            if id_document is not None:
                existing = self.store.one(
                    "SELECT id FROM persons WHERE id_document = ? AND identity_status != 'merged'",
                    (id_document,),
                )
                if existing is not None:
                    raise ConflictError(f"证件号已登记在案: {existing['id']}")
            self.store.conn.execute(
                "INSERT INTO persons(id, display_name, name_normalized, sex, age_band, sensitivity,"
                " identity_status, is_temp, id_document, state, current_shelter_id,"
                " registered_shelter_id, registered_at, registered_by, batch_id, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'sheltered', ?, ?, ?, ?, ?, ?)",
                (
                    person_id,
                    display_name.strip(),
                    _normalize_name(display_name),
                    sex,
                    age_band,
                    sensitivity.value,
                    "temp_unverified" if is_temp else "verified",
                    1 if is_temp else 0,
                    id_document,
                    shelter_id,
                    shelter_id,
                    now,
                    actor,
                    batch_id,
                    now,
                ),
            )
            self._change_occupancy_tx(shelter_id, 1, actor, parsed, now)
            self._emit(
                "person_registered",
                actor=actor,
                role=parsed,
                subject_type="person",
                subject_id=person_id,
                payload={
                    "display_name": display_name.strip(),
                    "age_band": age_band,
                    "sex": sex,
                    "shelter_id": shelter_id,
                    "batch_id": batch_id,
                    "is_temp": is_temp,
                    "has_document": id_document is not None,
                    "note": note,
                    "reception": {"shelter_id": shelter_id, "at": now, "by": actor},
                },
                now=now,
            )
            if id_document is not None:
                self._record_identity_step_tx(
                    actor, parsed, person_id, "document_check", "passed", "登记时出示证件", now
                )
            self._detect_duplicates_tx(actor, parsed, person_id, now)
        return person_id

    def _detect_duplicates_tx(self, actor: str, role: Role, person_id: str, now: str) -> None:
        """通信恢复后重复登记的防线：同名同年龄段即生成疑似重复线索。"""
        person = self._person(person_id)
        duplicates = self.store.all(
            "SELECT id, display_name FROM persons WHERE name_normalized = ? AND age_band = ?"
            " AND id != ? AND identity_status != 'merged'",
            (person["name_normalized"], person["age_band"], person_id),
        )
        for dup in duplicates:
            clue_id = self._new_id("clu")
            self.store.conn.execute(
                "INSERT INTO tracing_clues(id, clue_type, subject_person_id, target_name,"
                " target_age_band, detail, status, submitted_by, created_at)"
                " VALUES (?, 'possible_duplicate', ?, ?, ?, ?, 'open', ?, ?)",
                (
                    clue_id,
                    person_id,
                    dup["display_name"],
                    person["age_band"],
                    f"系统发现同名同年龄段档案 {dup['id']}，疑似重复登记",
                    actor,
                    now,
                ),
            )
            self._emit(
                "clue_recorded",
                actor=actor,
                role=role,
                subject_type="clue",
                subject_id=clue_id,
                payload={
                    "clue_type": "possible_duplicate",
                    "subject_person_id": person_id,
                    "candidate_person_id": dup["id"],
                    "auto": True,
                },
                now=now,
            )
            self.store.conn.execute(
                "INSERT OR IGNORE INTO match_candidates(id, clue_id, person_id, score, status)"
                " VALUES (?, ?, ?, 1.0, 'pending')",
                (self._new_id("mch"), clue_id, dup["id"]),
            )

    def _record_identity_step_tx(
        self,
        actor: str,
        role: Role,
        person_id: str,
        step: str,
        outcome: str,
        detail: str | None,
        now: str,
    ) -> None:
        self.store.conn.execute(
            "INSERT INTO identity_verifications(id, person_id, step, outcome, detail, actor, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self._new_id("idv"), person_id, step, outcome, detail, actor, now),
        )
        self._emit(
            "identity_step_recorded",
            actor=actor,
            role=role,
            subject_type="person",
            subject_id=person_id,
            payload={"step": step, "outcome": outcome},
            now=now,
        )
        if outcome == "passed" and step in {"document_check", "clue_match", "community_witness"}:
            person = self._person(person_id)
            if person["identity_status"] == "temp_unverified":
                self.store.conn.execute(
                    "UPDATE persons SET identity_status = 'verified', is_temp = 0 WHERE id = ?",
                    (person_id,),
                )
                self._emit(
                    "person_verified",
                    actor=actor,
                    role=role,
                    subject_type="person",
                    subject_id=person_id,
                    payload={"via_step": step},
                    now=now,
                )

    def record_identity_step(
        self,
        actor: str,
        role: str,
        *,
        person_id: str,
        step: str,
        outcome: str,
        detail: str | None = None,
    ) -> None:
        """记录一次身份核验过程（证件、访谈、社区见证、线索比对）。"""
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "身份核验")
        if step not in {"document_check", "interview", "community_witness", "clue_match"}:
            raise ValidationError(f"未知核验步骤: {step}")
        if outcome not in {"passed", "failed", "inconclusive"}:
            raise ValidationError(f"未知核验结果: {outcome}")
        now = self._now()
        with self.store.tx():
            person = self._person(person_id)
            if person["identity_status"] == "merged":
                raise ConflictError("档案已合并，请在保留档案上核验")
            self._record_identity_step_tx(actor, parsed, person_id, step, outcome, detail, now)

    def reception_record(self, actor: str, role: str, person_id: str) -> dict[str, Any]:
        """原接收地点、时间和经办人；合并后的旧档案同样可查。"""
        require_role(role, _STAFF_ROLES, "查看接收记录")
        person = self._person(person_id)
        return {
            "person_id": person["id"],
            "identity_status": person["identity_status"],
            "merged_into": person["merged_into"],
            "registered_shelter_id": person["registered_shelter_id"],
            "registered_at": person["registered_at"],
            "registered_by": person["registered_by"],
            "batch_id": person["batch_id"],
        }

    def get_person(
        self, actor: str, role: str, person_id: str, *, include_sensitive: bool = False
    ) -> dict[str, Any]:
        """基本资料全员可见；敏感档案（儿童/老人）仅对应岗位可见。"""
        parsed = require_role(role, _STAFF_ROLES, "查看人员")
        person = self._person(person_id)
        result = {
            "id": person["id"],
            "display_name": person["display_name"],
            "age_band": person["age_band"],
            "sex": person["sex"],
            "identity_status": person["identity_status"],
            "is_temp": bool(person["is_temp"]),
            "state": person["state"],
            "current_shelter_id": person["current_shelter_id"],
            "sensitivity": person["sensitivity"],
            "merged_into": person["merged_into"],
        }
        if include_sensitive:
            require_sensitivity_access(parsed, person["sensitivity"], "查看敏感档案")
            result["id_document"] = person["id_document"]
            result["registered_by"] = person["registered_by"]
            result["registered_at"] = person["registered_at"]
            result["registered_shelter_id"] = person["registered_shelter_id"]
            result["verifications"] = self.store.all(
                "SELECT step, outcome, detail, actor, created_at FROM identity_verifications"
                " WHERE person_id = ? ORDER BY created_at",
                (person_id,),
            )
        return result

    # ------------------------------------------------------------------
    # 健康与无障碍需求（敏感资料）
    # ------------------------------------------------------------------

    def record_need(
        self,
        actor: str,
        role: str,
        *,
        person_id: str,
        category: str,
        detail: str,
        sensitivity: str | None = None,
    ) -> str:
        """记录健康/无障碍/照护需求；敏感度决定哪些岗位可见。"""
        parsed = require_role(role, _STAFF_ROLES, "记录需求")
        if category not in _NEED_CATEGORY_SENSITIVITY:
            raise ValidationError(f"未知需求类别: {category}")
        level = Sensitivity(sensitivity) if sensitivity else _NEED_CATEGORY_SENSITIVITY[category]
        require_sensitivity_access(parsed, level, "记录该类需求")
        if not detail.strip():
            raise ValidationError("需求内容不能为空")
        now = self._now()
        need_id = self._new_id("ned")
        with self.store.tx():
            person = self._person(self._resolve_person_id(person_id))
            self.store.conn.execute(
                "INSERT INTO needs(id, person_id, category, sensitivity, detail, status, recorded_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
                (need_id, person["id"], category, level.value, detail.strip(), actor, now),
            )
            # 事件只带类别与敏感度，不写具体细节，避免敏感内容进入广播式日志
            self._emit(
                "need_recorded",
                actor=actor,
                role=parsed,
                subject_type="person",
                subject_id=person["id"],
                payload={"need_id": need_id, "category": category, "sensitivity": level.value},
                now=now,
            )
        return need_id

    def list_needs(self, actor: str, role: str, person_id: str) -> list[dict[str, Any]]:
        """按岗位过滤需求列表；无权查看的类别直接不可见。"""
        parsed = require_role(role, _STAFF_ROLES, "查看需求")
        person = self._person(person_id)
        visible = {level.value for level in visible_sensitivities(parsed)}
        rows = self.store.all(
            "SELECT * FROM needs WHERE person_id = ? ORDER BY created_at", (person["id"],)
        )
        return [row for row in rows if row["sensitivity"] in visible]

    # ------------------------------------------------------------------
    # 家庭关系与拆分/团聚
    # ------------------------------------------------------------------

    def create_family(self, actor: str, role: str, *, name: str) -> str:
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "建立家庭")
        if not name.strip():
            raise ValidationError("家庭名称不能为空")
        now = self._now()
        family_id = self._new_id("fam")
        with self.store.tx():
            self.store.conn.execute(
                "INSERT INTO families(id, name, status, created_at) VALUES (?, ?, 'intact', ?)",
                (family_id, name.strip(), now),
            )
            self._emit(
                "family_created",
                actor=actor,
                role=parsed,
                subject_type="family",
                subject_id=family_id,
                payload={"name": name.strip()},
                now=now,
            )
        return family_id

    def add_family_member(
        self, actor: str, role: str, *, family_id: str, person_id: str, relation: str
    ) -> None:
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "添加家庭成员")
        now = self._now()
        with self.store.tx():
            self._family(family_id)
            person = self._person(self._resolve_person_id(person_id))
            existing = self.store.one(
                "SELECT * FROM family_members WHERE family_id = ? AND person_id = ?",
                (family_id, person["id"]),
            )
            if existing is not None and existing["active"]:
                raise ConflictError("该成员已在家庭中")
            if existing is not None:
                self.store.conn.execute(
                    "UPDATE family_members SET active = 1, left_at = NULL, relation = ?, joined_at = ?"
                    " WHERE family_id = ? AND person_id = ?",
                    (relation, now, family_id, person["id"]),
                )
            else:
                self.store.conn.execute(
                    "INSERT INTO family_members(family_id, person_id, relation, active, joined_at)"
                    " VALUES (?, ?, ?, 1, ?)",
                    (family_id, person["id"], relation, now),
                )
            self._emit(
                "family_member_added",
                actor=actor,
                role=parsed,
                subject_type="family",
                subject_id=family_id,
                payload={"person_id": person["id"], "relation": relation},
                now=now,
            )
            self._recompute_family_tx(actor, parsed, family_id, "member_added", now)

    def _family_members_tx(self, family_id: str) -> list[dict[str, Any]]:
        return self.store.all(
            "SELECT p.* FROM family_members fm JOIN persons p ON p.id = fm.person_id"
            " WHERE fm.family_id = ? AND fm.active = 1",
            (family_id,),
        )

    def _recompute_family_tx(
        self, actor: str, role: Role, family_id: str, trigger: str, now: str,
        reason: str | None = None,
    ) -> None:
        """根据成员当前所在安置点重算家庭状态，迁移时记录拆分/团聚事件。"""
        family = self._family(family_id)
        members = self._family_members_tx(family_id)
        sheltered = [m for m in members if m["state"] == "sheltered" and m["current_shelter_id"]]
        locations = sorted({m["current_shelter_id"] for m in sheltered})
        member_locations = {
            m["id"]: {
                "name": m["display_name"],
                "shelter_id": m["current_shelter_id"],
                "state": m["state"],
                "batch_id": m["batch_id"],
            }
            for m in members
        }
        if len(locations) == 1 and len(sheltered) > 1:
            # 多名成员真正共处同一安置点才算团聚；仅剩一人在册不算
            if family["status"] == "separated" and len(members) > 1:
                self.store.conn.execute(
                    "UPDATE families SET status = 'reunited' WHERE id = ?", (family_id,)
                )
                clue_ids = self._confirmed_clues_for_members_tx(members)
                self._emit(
                    "family_reunited",
                    actor=actor,
                    role=role,
                    subject_type="family",
                    subject_id=family_id,
                    payload={
                        "trigger": trigger,
                        "member_locations": member_locations,
                        "clue_ids": clue_ids,
                    },
                    now=now,
                )
                for clue_id in clue_ids:
                    self.store.conn.execute(
                        "UPDATE tracing_clues SET status = 'used' WHERE id = ? AND status = 'confirmed'",
                        (clue_id,),
                    )
        else:
            # 拆分状态：首次转入，或成员落点格局变化时，都记录新的拆分事件
            changed = family["status"] != "separated"
            if not changed:
                last = self.store.one(
                    "SELECT payload FROM events WHERE type = 'family_split' AND subject_id = ?"
                    " ORDER BY id DESC LIMIT 1",
                    (family_id,),
                )
                previous_locations = (
                    json.loads(last["payload"]).get("member_locations", {}) if last else {}
                )
                previous = {
                    pid: (info.get("shelter_id"), info.get("state"))
                    for pid, info in previous_locations.items()
                }
                current = {
                    pid: (info["shelter_id"], info["state"])
                    for pid, info in member_locations.items()
                }
                changed = previous != current
            if changed:
                self.store.conn.execute(
                    "UPDATE families SET status = 'separated' WHERE id = ?", (family_id,)
                )
                self._emit(
                    "family_split",
                    actor=actor,
                    role=role,
                    subject_type="family",
                    subject_id=family_id,
                    payload={
                        "trigger": trigger,
                        "reason": reason or "成员被送往不同安置点",
                        "member_locations": member_locations,
                    },
                    now=now,
                )

    def _confirmed_clues_for_members_tx(self, members: list[dict[str, Any]]) -> list[str]:
        """汇集成员（含被合并旧档案）已确认的线索，作为团聚依据。"""
        if not members:
            return []
        member_ids = [m["id"] for m in members]
        merged_ids = [
            row["id"]
            for row in self.store.all(
                "SELECT id FROM persons WHERE merged_into IN (%s)"
                % ",".join("?" for _ in member_ids),
                tuple(member_ids),
            )
        ]
        all_ids = member_ids + merged_ids
        placeholders = ",".join("?" for _ in all_ids)
        return [
            row["id"]
            for row in self.store.all(
                f"SELECT id FROM tracing_clues WHERE status IN ('confirmed', 'used')"
                f" AND subject_person_id IN ({placeholders})",
                tuple(all_ids),
            )
        ]

    # ------------------------------------------------------------------
    # 寻亲线索与档案合并
    # ------------------------------------------------------------------

    def record_clue(
        self,
        actor: str,
        role: str,
        *,
        clue_type: str,
        detail: str,
        subject_person_id: str | None = None,
        target_name: str | None = None,
        target_age_band: str | None = None,
    ) -> str:
        """登记寻亲线索；寻找类线索立即做一轮候选匹配并挂为待办。"""
        parsed = require_role(role, _STAFF_ROLES, "登记线索")
        if clue_type not in {"seeking_person", "found_person", "possible_duplicate", "location_report"}:
            raise ValidationError(f"未知线索类型: {clue_type}")
        if not detail.strip():
            raise ValidationError("线索内容不能为空")
        now = self._now()
        clue_id = self._new_id("clu")
        with self.store.tx():
            if subject_person_id is not None:
                self._person(subject_person_id)
            self.store.conn.execute(
                "INSERT INTO tracing_clues(id, clue_type, subject_person_id, target_name,"
                " target_age_band, detail, status, submitted_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                (
                    clue_id,
                    clue_type,
                    subject_person_id,
                    target_name,
                    target_age_band,
                    detail.strip(),
                    actor,
                    now,
                ),
            )
            self._emit(
                "clue_recorded",
                actor=actor,
                role=parsed,
                subject_type="clue",
                subject_id=clue_id,
                payload={
                    "clue_type": clue_type,
                    "subject_person_id": subject_person_id,
                    "target_name": target_name,
                    "auto": False,
                },
                now=now,
            )
            if clue_type == "seeking_person":
                self._run_matching_tx(clue_id)
                self._ensure_task_tx(
                    TASK_MISSING_MATCH,
                    "clue",
                    clue_id,
                    {
                        "target_name": target_name,
                        "summary": f"寻找 {target_name or '未知姓名'} 的线索待比对",
                    },
                    now,
                )
        return clue_id

    def _run_matching_tx(self, clue_id: str) -> list[str]:
        """按姓名（忽略空白、大小写）为寻找类线索刷新候选名单。"""
        clue = self._get("tracing_clues", clue_id, "线索")
        if not clue["target_name"]:
            return []
        normalized = _normalize_name(clue["target_name"])
        candidates = self.store.all(
            "SELECT id FROM persons WHERE name_normalized = ? AND identity_status != 'merged'",
            (normalized,),
        )
        found = []
        for person in candidates:
            if person["id"] == clue["subject_person_id"]:
                continue
            self.store.conn.execute(
                "INSERT OR IGNORE INTO match_candidates(id, clue_id, person_id, score, status)"
                " VALUES (?, ?, ?, 1.0, 'pending')",
                (self._new_id("mch"), clue_id, person["id"]),
            )
            found.append(person["id"])
        return found

    def confirm_clue(self, actor: str, role: str, *, clue_id: str) -> None:
        """主管确认线索；确认后才允许据此合并档案或认定团聚。"""
        parsed = require_role(role, {Role.SUPERVISOR}, "确认线索")
        now = self._now()
        with self.store.tx():
            clue = self._get("tracing_clues", clue_id, "线索")
            if clue["status"] != "open":
                raise ConflictError(f"线索当前状态为 {clue['status']}，不能确认")
            self.store.conn.execute(
                "UPDATE tracing_clues SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? WHERE id = ?",
                (actor, now, clue_id),
            )
            self._emit(
                "clue_confirmed",
                actor=actor,
                role=parsed,
                subject_type="clue",
                subject_id=clue_id,
                payload={"clue_type": clue["clue_type"]},
                now=now,
            )
            self._settle_tasks_tx(TASK_MISSING_MATCH, "clue", clue_id, "done", now)

    def reject_clue(self, actor: str, role: str, *, clue_id: str, reason: str) -> None:
        parsed = require_role(role, {Role.SUPERVISOR}, "驳回线索")
        now = self._now()
        with self.store.tx():
            clue = self._get("tracing_clues", clue_id, "线索")
            if clue["status"] != "open":
                raise ConflictError(f"线索当前状态为 {clue['status']}，不能驳回")
            self.store.conn.execute(
                "UPDATE tracing_clues SET status = 'rejected', confirmed_by = ?, confirmed_at = ? WHERE id = ?",
                (actor, now, clue_id),
            )
            self._emit(
                "clue_rejected",
                actor=actor,
                role=parsed,
                subject_type="clue",
                subject_id=clue_id,
                payload={"reason": reason},
                now=now,
            )
            self._settle_tasks_tx(TASK_MISSING_MATCH, "clue", clue_id, "cancelled", now)

    def merge_persons(
        self,
        actor: str,
        role: str,
        *,
        survivor_id: str,
        merged_id: str,
        clue_id: str,
        reason: str,
    ) -> None:
        """线索确认后合并档案。

        被合并档案保留原接收地点/时间/经办人，状态置为 merged；
        家庭成员、需求、未执行安排改指保留档案，已完成的发放记录保持原样
        （谁领取是历史事实，通过合并链归属到保留档案）。
        """
        parsed = require_role(role, {Role.SUPERVISOR}, "合并档案")
        if survivor_id == merged_id:
            raise ValidationError("不能与自身合并")
        now = self._now()
        with self.store.tx():
            survivor = self._person(survivor_id)
            merged = self._person(merged_id)
            if survivor["identity_status"] == "merged" or merged["identity_status"] == "merged":
                raise ConflictError("合并链上的档案不能再次合并")
            clue = self._get("tracing_clues", clue_id, "线索")
            if clue["status"] != "confirmed":
                raise ConflictError("线索未经主管确认，不能合并")
            involved = {clue["subject_person_id"], clue["target_name"]}
            candidate_ids = {
                row["person_id"]
                for row in self.store.all(
                    "SELECT person_id FROM match_candidates WHERE clue_id = ?", (clue_id,)
                )
            }
            pair = {survivor_id, merged_id}
            if not (pair & candidate_ids or clue["subject_person_id"] in pair):
                raise ValidationError("线索与待合并档案不对应: " + json.dumps(sorted(involved), ensure_ascii=False))
            self.store.conn.execute(
                "UPDATE persons SET identity_status = 'merged', merged_into = ? WHERE id = ?",
                (survivor_id, merged_id),
            )
            # 未执行安排与当前关系改指保留档案
            self.store.conn.execute(
                "UPDATE needs SET person_id = ? WHERE person_id = ?", (survivor_id, merged_id)
            )
            self.store.conn.execute(
                "UPDATE supply_movements SET person_id = ? WHERE person_id = ? AND status = 'planned'",
                (survivor_id, merged_id),
            )
            self.store.conn.execute(
                "UPDATE match_candidates SET person_id = ? WHERE person_id = ?",
                (survivor_id, merged_id),
            )
            for membership in self.store.all(
                "SELECT * FROM family_members WHERE person_id = ? AND active = 1", (merged_id,)
            ):
                clash = self.store.one(
                    "SELECT 1 FROM family_members WHERE family_id = ? AND person_id = ?",
                    (membership["family_id"], survivor_id),
                )
                if clash:
                    self.store.conn.execute(
                        "UPDATE family_members SET active = 0, left_at = ? WHERE family_id = ? AND person_id = ?",
                        (now, membership["family_id"], merged_id),
                    )
                else:
                    self.store.conn.execute(
                        "UPDATE family_members SET person_id = ? WHERE family_id = ? AND person_id = ?",
                        (survivor_id, membership["family_id"], merged_id),
                    )
            # 被合并者若仍在册，腾出床位并把在册状态并到保留档案
            if merged["state"] == "sheltered" and merged["current_shelter_id"]:
                if survivor["state"] == "sheltered":
                    self._change_occupancy_tx(merged["current_shelter_id"], -1, actor, parsed, now)
                else:
                    self.store.conn.execute(
                        "UPDATE persons SET state = 'sheltered', current_shelter_id = ? WHERE id = ?",
                        (merged["current_shelter_id"], survivor_id),
                    )
                self.store.conn.execute(
                    "UPDATE persons SET current_shelter_id = NULL, state = 'discharged' WHERE id = ?",
                    (merged_id,),
                )
            # 被合并者的待确认转运随之取消
            for transfer in self.store.all(
                "SELECT id FROM transfers WHERE person_id = ? AND status = 'planned'", (merged_id,)
            ):
                self.store.conn.execute(
                    "UPDATE transfers SET status = 'cancelled' WHERE id = ?", (transfer["id"],)
                )
                self._settle_tasks_tx(
                    TASK_TRANSFER_CONFIRMATION, "transfer", transfer["id"], "cancelled", now
                )
            self._emit(
                "persons_merged",
                actor=actor,
                role=parsed,
                subject_type="person",
                subject_id=survivor_id,
                payload={
                    "survivor_id": survivor_id,
                    "merged_id": merged_id,
                    "clue_id": clue_id,
                    "reason": reason,
                    "survivor_reception": {
                        "shelter_id": survivor["registered_shelter_id"],
                        "at": survivor["registered_at"],
                        "by": survivor["registered_by"],
                    },
                    "merged_reception": {
                        "shelter_id": merged["registered_shelter_id"],
                        "at": merged["registered_at"],
                        "by": merged["registered_by"],
                    },
                },
                now=now,
            )
            self._emit(
                "person_merged_away",
                actor=actor,
                role=parsed,
                subject_type="person",
                subject_id=merged_id,
                payload={"survivor_id": survivor_id, "clue_id": clue_id},
                now=now,
            )
            for row in self.store.all(
                "SELECT family_id FROM family_members WHERE person_id = ? AND active = 1",
                (survivor_id,),
            ):
                self._recompute_family_tx(actor, parsed, row["family_id"], "persons_merged", now)

    # ------------------------------------------------------------------
    # 转运、出院、返家、跨区调剂
    # ------------------------------------------------------------------

    def plan_transfer(
        self,
        actor: str,
        role: str,
        *,
        person_id: str,
        to_shelter_id: str,
        reason: str,
        reason_category: str = "other",
        kind: str = "transfer",
    ) -> str:
        """安排转运；确认前人员仍占用原安置点床位，确认时联动调整。"""
        parsed = require_role(
            role, {Role.RECEPTION, Role.COORDINATOR, Role.SUPERVISOR}, "安排转运"
        )
        now = self._now()
        with self.store.tx():
            return self._plan_transfer_tx(
                actor, parsed, person_id=person_id, to_shelter_id=to_shelter_id,
                reason=reason, reason_category=reason_category, kind=kind, now=now,
            )

    def _plan_transfer_tx(
        self,
        actor: str,
        parsed: Role,
        *,
        person_id: str,
        to_shelter_id: str,
        reason: str,
        reason_category: str,
        kind: str,
        now: str,
    ) -> str:
        if kind not in {"transfer", "reallocation"}:
            raise ValidationError(f"未知转运类型: {kind}")
        transfer_id = self._new_id("trf")
        person = self._person(self._resolve_person_id(person_id))
        if person["state"] != "sheltered":
            raise ConflictError("人员当前不在安置点，不能安排转运")
        if person["current_shelter_id"] == to_shelter_id:
            raise ValidationError("目标安置点与当前相同")
        self._shelter(to_shelter_id)
        existing = self.store.one(
            "SELECT id FROM transfers WHERE person_id = ? AND status = 'planned'",
            (person["id"],),
        )
        if existing is not None:
            raise ConflictError(f"已存在待确认转运: {existing['id']}")
        self.store.conn.execute(
            "INSERT INTO transfers(id, person_id, from_shelter_id, to_shelter_id, kind,"
            " reason, reason_category, status, planned_by, planned_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)",
            (
                transfer_id,
                person["id"],
                person["current_shelter_id"],
                to_shelter_id,
                kind,
                reason,
                reason_category,
                actor,
                now,
            ),
        )
        self._emit(
            "transfer_planned",
            actor=actor,
            role=parsed,
            subject_type="transfer",
            subject_id=transfer_id,
            payload={
                "person_id": person["id"],
                "from_shelter_id": person["current_shelter_id"],
                "to_shelter_id": to_shelter_id,
                "kind": kind,
                "reason": reason,
                "reason_category": reason_category,
            },
            now=now,
        )
        self._ensure_task_tx(
            TASK_TRANSFER_CONFIRMATION,
            "transfer",
            transfer_id,
            {
                "person_id": person["id"],
                "person_name": person["display_name"],
                "to_shelter_id": to_shelter_id,
                "summary": f"{person['display_name']} 的转运待确认",
            },
            now,
        )
        return transfer_id

    def confirm_transfer(self, actor: str, role: str, *, transfer_id: str) -> None:
        """确认转运到达：床位迁移、未执行发放取消、家庭状态重算。"""
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "确认转运")
        now = self._now()
        with self.store.tx():
            transfer = self._get("transfers", transfer_id, "转运")
            if transfer["status"] != "planned":
                raise ConflictError(f"转运当前状态为 {transfer['status']}")
            person = self._person(transfer["person_id"])
            if person["state"] != "sheltered":
                raise ConflictError("人员已离点，转运不能确认")
            self.store.conn.execute(
                "UPDATE transfers SET status = 'confirmed', confirmed_by = ?, confirmed_at = ? WHERE id = ?",
                (actor, now, transfer_id),
            )
            self.store.conn.execute(
                "UPDATE persons SET current_shelter_id = ? WHERE id = ?",
                (transfer["to_shelter_id"], person["id"]),
            )
            self._change_occupancy_tx(transfer["from_shelter_id"], -1, actor, parsed, now)
            self._change_occupancy_tx(transfer["to_shelter_id"], 1, actor, parsed, now)
            self._cancel_planned_movements_tx(
                actor, parsed, person["id"], "人员转运，原安置点的未执行发放取消", now,
                keep_shelter_id=transfer["to_shelter_id"],
            )
            self._emit(
                "transfer_confirmed",
                actor=actor,
                role=parsed,
                subject_type="transfer",
                subject_id=transfer_id,
                payload={
                    "person_id": person["id"],
                    "from_shelter_id": transfer["from_shelter_id"],
                    "to_shelter_id": transfer["to_shelter_id"],
                    "reason": transfer["reason"],
                    "reason_category": transfer["reason_category"],
                },
                now=now,
            )
            self._settle_tasks_tx(TASK_TRANSFER_CONFIRMATION, "transfer", transfer_id, "done", now)
            for row in self.store.all(
                "SELECT family_id FROM family_members WHERE person_id = ? AND active = 1",
                (person["id"],),
            ):
                self._recompute_family_tx(
                    actor, parsed, row["family_id"], "transfer_confirmed", now,
                    reason=transfer["reason"],
                )

    def cancel_transfer(self, actor: str, role: str, *, transfer_id: str, reason: str) -> None:
        parsed = require_role(
            role, {Role.RECEPTION, Role.COORDINATOR, Role.SUPERVISOR}, "取消转运"
        )
        now = self._now()
        with self.store.tx():
            transfer = self._get("transfers", transfer_id, "转运")
            if transfer["status"] != "planned":
                raise ConflictError(f"转运当前状态为 {transfer['status']}")
            self.store.conn.execute(
                "UPDATE transfers SET status = 'cancelled' WHERE id = ?", (transfer_id,)
            )
            self._emit(
                "transfer_cancelled",
                actor=actor,
                role=parsed,
                subject_type="transfer",
                subject_id=transfer_id,
                payload={"reason": reason},
                now=now,
            )
            self._settle_tasks_tx(TASK_TRANSFER_CONFIRMATION, "transfer", transfer_id, "cancelled", now)

    def _departure_tx(
        self,
        actor: str,
        role: Role,
        person_id: str,
        new_state: str,
        event_type: str,
        reason: str,
        now: str,
    ) -> None:
        """出院/返家公共逻辑：腾床位、取消未执行安排、重算家庭状态。"""
        person = self._person(self._resolve_person_id(person_id))
        if person["state"] != "sheltered":
            raise ConflictError("人员当前不在安置点")
        self.store.conn.execute(
            "UPDATE persons SET state = ?, current_shelter_id = NULL WHERE id = ?",
            (new_state, person["id"]),
        )
        self._change_occupancy_tx(person["current_shelter_id"], -1, actor, role, now)
        self._cancel_planned_movements_tx(actor, role, person["id"], f"人员{event_type}，未执行发放取消", now)
        for transfer in self.store.all(
            "SELECT id FROM transfers WHERE person_id = ? AND status = 'planned'", (person["id"],)
        ):
            self.store.conn.execute(
                "UPDATE transfers SET status = 'cancelled' WHERE id = ?", (transfer["id"],)
            )
            self._settle_tasks_tx(TASK_TRANSFER_CONFIRMATION, "transfer", transfer["id"], "cancelled", now)
        self._emit(
            event_type,
            actor=actor,
            role=role,
            subject_type="person",
            subject_id=person["id"],
            payload={"reason": reason, "from_shelter_id": person["current_shelter_id"]},
            now=now,
        )
        for row in self.store.all(
            "SELECT family_id FROM family_members WHERE person_id = ? AND active = 1",
            (person["id"],),
        ):
            self._recompute_family_tx(actor, role, row["family_id"], event_type, now, reason=reason)

    def discharge_person(self, actor: str, role: str, *, person_id: str, reason: str) -> None:
        """出院（如医疗后离点）。"""
        parsed = require_role(role, {Role.RECEPTION, Role.MEDICAL, Role.SUPERVISOR}, "办理出院")
        now = self._now()
        with self.store.tx():
            self._departure_tx(actor, parsed, person_id, "discharged", "person_discharged", reason, now)

    def return_home(self, actor: str, role: str, *, person_id: str, reason: str) -> None:
        """返家。"""
        parsed = require_role(role, {Role.RECEPTION, Role.SUPERVISOR}, "办理返家")
        now = self._now()
        with self.store.tx():
            self._departure_tx(actor, parsed, person_id, "returned_home", "person_returned_home", reason, now)

    def reallocate(
        self,
        actor: str,
        role: str,
        *,
        from_shelter_id: str,
        to_shelter_id: str,
        person_ids: list[str],
        reason: str,
    ) -> list[str]:
        """跨区调剂：协调员批量安排转运（单事务），接收方逐一确认。"""
        parsed = require_role(role, {Role.COORDINATOR, Role.SUPERVISOR}, "跨区调剂")
        if not person_ids:
            raise ValidationError("调剂名单不能为空")
        now = self._now()
        with self.store.tx():
            self._shelter(from_shelter_id)
            transfer_ids = []
            for person_id in person_ids:
                person = self._person(self._resolve_person_id(person_id))
                if person["current_shelter_id"] != from_shelter_id:
                    raise ConflictError(f"人员 {person_id} 不在调出安置点")
                transfer_ids.append(
                    self._plan_transfer_tx(
                        actor, parsed, person_id=person["id"], to_shelter_id=to_shelter_id,
                        reason=reason, reason_category="reallocation",
                        kind="reallocation", now=now,
                    )
                )
        return transfer_ids

    # ------------------------------------------------------------------
    # 物资批次与守恒
    # ------------------------------------------------------------------

    def register_supply_batch(
        self,
        actor: str,
        role: str,
        *,
        kind: str,
        item: str,
        quantity: int,
        unit: str,
        shelter_id: str,
        sensitivity: str | None = None,
        resupply_level: int = 0,
    ) -> str:
        parsed = require_role(role, {Role.LOGISTICS, Role.SUPERVISOR}, "登记物资批次")
        now = self._now()
        with self.store.tx():
            batch_id = self._register_supply_batch_tx(
                actor, parsed, kind=kind, item=item, quantity=quantity, unit=unit,
                shelter_id=shelter_id, sensitivity=sensitivity, resupply_level=resupply_level,
                now=now,
            )
        return batch_id

    def _register_supply_batch_tx(
        self,
        actor: str,
        role: Role,
        *,
        kind: str,
        item: str,
        quantity: int,
        unit: str,
        shelter_id: str,
        sensitivity: str | None,
        resupply_level: int,
        now: str,
    ) -> str:
        if kind not in {"medicine", "infant", "food", "hygiene", "other"}:
            raise ValidationError(f"未知物资类别: {kind}")
        if quantity <= 0:
            raise ValidationError("批次数量必须为正")
        level = sensitivity or (Sensitivity.MEDICAL.value if kind == "medicine" else Sensitivity.GENERAL.value)
        Sensitivity(level)  # 校验合法
        self._shelter(shelter_id)
        batch_id = self._new_id("bat")
        self.store.conn.execute(
            "INSERT INTO supply_batches(id, kind, item, quantity, unit, shelter_id, sensitivity,"
            " resupply_level, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?)",
            (batch_id, kind, item.strip(), quantity, unit.strip(), shelter_id, level, resupply_level, now),
        )
        self._emit(
            "supply_batch_registered",
            actor=actor,
            role=role,
            subject_type="supply_batch",
            subject_id=batch_id,
            payload={
                "kind": kind,
                "item": item.strip(),
                "quantity": quantity,
                "unit": unit.strip(),
                "shelter_id": shelter_id,
                "sensitivity": level,
            },
            now=now,
        )
        self._refresh_medicine_resupply_tx(batch_id, actor, role, now)
        return batch_id

    def _movements_tx(self, batch_id: str, completed_only: bool = True) -> list[dict[str, Any]]:
        if completed_only:
            return self.store.all(
                "SELECT * FROM supply_movements WHERE batch_id = ? AND status = 'completed'"
                " ORDER BY created_at, id",
                (batch_id,),
            )
        return self.store.all(
            "SELECT * FROM supply_movements WHERE batch_id = ? ORDER BY created_at, id", (batch_id,)
        )

    def batch_stock(self, batch_id: str) -> dict[str, int]:
        """按安置点计算批次现存（由移动流水推导，不直接改库存列）。"""
        batch = self._batch(batch_id)
        stock: dict[str, int] = {batch["shelter_id"]: batch["quantity"]}
        for mv in self._movements_tx(batch_id):
            if mv["movement_type"] in {"distribute", "reissue"}:
                stock[mv["from_shelter_id"]] = stock.get(mv["from_shelter_id"], 0) - mv["quantity"]
            elif mv["movement_type"] == "return":
                stock[mv["to_shelter_id"]] = stock.get(mv["to_shelter_id"], 0) + mv["quantity"]
            elif mv["movement_type"] == "transfer":
                stock[mv["from_shelter_id"]] = stock.get(mv["from_shelter_id"], 0) - mv["quantity"]
                stock[mv["to_shelter_id"]] = stock.get(mv["to_shelter_id"], 0) + mv["quantity"]
            elif mv["movement_type"] == "adjust":
                if mv["from_shelter_id"]:
                    stock[mv["from_shelter_id"]] = stock.get(mv["from_shelter_id"], 0) - mv["quantity"]
                if mv["to_shelter_id"]:
                    stock[mv["to_shelter_id"]] = stock.get(mv["to_shelter_id"], 0) + mv["quantity"]
            # handover 只在人员之间转移，不影响库存
        return {k: v for k, v in stock.items() if v != 0}

    def batch_remaining(self, batch_id: str) -> int:
        return sum(self.batch_stock(batch_id).values())

    def _require_batch_access(self, role: Role, batch: dict[str, Any], action: str) -> None:
        if batch["sensitivity"] != Sensitivity.GENERAL.value:
            allowed = {Role.MEDICAL, Role.LOGISTICS, Role.SUPERVISOR}
            if role not in allowed:
                raise AccessDeniedError(f"岗位 {role.value} 无权{action}敏感批次")

    def _record_movement_tx(
        self,
        actor: str,
        role: Role,
        *,
        batch_id: str,
        movement_type: str,
        quantity: int,
        now: str,
        person_id: str | None = None,
        from_shelter_id: str | None = None,
        to_shelter_id: str | None = None,
        to_person_id: str | None = None,
        reason: str | None = None,
        linked_movement_id: str | None = None,
        status: str = "completed",
    ) -> str:
        movement_id = self._new_id("mov")
        self.store.conn.execute(
            "INSERT INTO supply_movements(id, batch_id, movement_type, quantity, person_id,"
            " from_shelter_id, to_shelter_id, to_person_id, reason, linked_movement_id, status,"
            " actor, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                movement_id, batch_id, movement_type, quantity, person_id,
                from_shelter_id, to_shelter_id, to_person_id, reason, linked_movement_id,
                status, actor, now,
            ),
        )
        self._emit(
            f"supply_{movement_type}" + ("_planned" if status == "planned" else ""),
            actor=actor,
            role=role,
            subject_type="supply_batch",
            subject_id=batch_id,
            payload={
                "movement_id": movement_id,
                "quantity": quantity,
                "person_id": person_id,
                "from_shelter_id": from_shelter_id,
                "to_shelter_id": to_shelter_id,
                "to_person_id": to_person_id,
                "reason": reason,
                "linked_movement_id": linked_movement_id,
            },
            now=now,
        )
        return movement_id

    def _refresh_medicine_resupply_tx(self, batch_id: str, actor: str, role: Role, now: str) -> None:
        batch = self._batch(batch_id)
        if batch["kind"] != "medicine" or batch["resupply_level"] <= 0:
            return
        remaining = self.batch_remaining(batch_id)
        if remaining <= batch["resupply_level"]:
            created = self._ensure_task_tx(
                TASK_MEDICINE_RESUPPLY,
                "supply_batch",
                batch_id,
                {
                    "item": batch["item"],
                    "remaining": remaining,
                    "resupply_level": batch["resupply_level"],
                    "summary": f"药品 {batch['item']} 余量 {remaining}，低于补给线 {batch['resupply_level']}",
                },
                now,
            )
            if created:
                self._emit(
                    "medicine_resupply_needed",
                    actor=actor,
                    role=role,
                    subject_type="supply_batch",
                    subject_id=batch_id,
                    payload={"remaining": remaining, "resupply_level": batch["resupply_level"]},
                    now=now,
                )
        else:
            self._settle_tasks_tx(TASK_MEDICINE_RESUPPLY, "supply_batch", batch_id, "done", now)

    def distribute(
        self,
        actor: str,
        role: str,
        *,
        batch_id: str,
        person_id: str,
        quantity: int,
        reason: str | None = None,
    ) -> str:
        """现场发放：库存减少，记入领取人名下。"""
        parsed = require_role(
            role, {Role.LOGISTICS, Role.RECEPTION, Role.MEDICAL, Role.SUPERVISOR}, "发放物资"
        )
        if quantity <= 0:
            raise ValidationError("发放数量必须为正")
        now = self._now()
        with self.store.tx():
            batch = self._batch(batch_id)
            self._require_batch_access(parsed, batch, "发放")
            if batch["sensitivity"] == Sensitivity.MEDICAL.value and parsed not in {
                Role.MEDICAL, Role.SUPERVISOR,
            }:
                raise AccessDeniedError("医疗物资只能由医护或主管发放")
            person = self._person(self._resolve_person_id(person_id))
            if person["state"] != "sheltered":
                raise ConflictError("人员不在安置点，不能发放")
            stock = self.batch_stock(batch_id)
            at_shelter = stock.get(person["current_shelter_id"], 0)
            if at_shelter < quantity:
                raise ConflictError(
                    f"安置点现存 {at_shelter}，不足发放 {quantity}"
                )
            movement_id = self._record_movement_tx(
                actor, parsed, batch_id=batch_id, movement_type="distribute", quantity=quantity,
                person_id=person["id"], from_shelter_id=person["current_shelter_id"],
                reason=reason, now=now,
            )
            self._refresh_medicine_resupply_tx(batch_id, actor, parsed, now)
        return movement_id

    def plan_distribution(
        self,
        actor: str,
        role: str,
        *,
        batch_id: str,
        person_id: str,
        quantity: int,
        reason: str | None = None,
    ) -> str:
        """计划发放（如夜间定时药）；人员离点时自动取消。"""
        parsed = require_role(role, {Role.MEDICAL, Role.LOGISTICS, Role.SUPERVISOR}, "计划发放")
        if quantity <= 0:
            raise ValidationError("发放数量必须为正")
        now = self._now()
        with self.store.tx():
            batch = self._batch(batch_id)
            self._require_batch_access(parsed, batch, "计划发放")
            person = self._person(self._resolve_person_id(person_id))
            if person["state"] != "sheltered":
                raise ConflictError("人员不在安置点")
            movement_id = self._record_movement_tx(
                actor, parsed, batch_id=batch_id, movement_type="distribute", quantity=quantity,
                person_id=person["id"], from_shelter_id=person["current_shelter_id"],
                reason=reason, status="planned", now=now,
            )
        return movement_id

    def execute_planned_distribution(self, actor: str, role: str, *, movement_id: str) -> None:
        parsed = require_role(
            role, {Role.MEDICAL, Role.LOGISTICS, Role.SUPERVISOR}, "执行计划发放"
        )
        now = self._now()
        with self.store.tx():
            movement = self._get("supply_movements", movement_id, "发放计划")
            if movement["status"] != "planned":
                raise ConflictError("该发放计划不在待执行状态")
            person = self._person(movement["person_id"])
            if person["state"] != "sheltered":
                raise ConflictError("人员已离点，计划应已取消")
            stock = self.batch_stock(movement["batch_id"])
            if stock.get(person["current_shelter_id"], 0) < movement["quantity"]:
                raise ConflictError("现存不足，无法执行计划发放")
            self.store.conn.execute(
                "UPDATE supply_movements SET status = 'completed', actor = ?, created_at = ? WHERE id = ?",
                (actor, now, movement_id),
            )
            self._emit(
                "supply_distribute",
                actor=actor,
                role=parsed,
                subject_type="supply_batch",
                subject_id=movement["batch_id"],
                payload={
                    "movement_id": movement_id,
                    "quantity": movement["quantity"],
                    "person_id": person["id"],
                    "from_planned": True,
                },
                now=now,
            )
            self._refresh_medicine_resupply_tx(movement["batch_id"], actor, parsed, now)

    def _cancel_planned_movements_tx(
        self,
        actor: str,
        role: Role,
        person_id: str,
        reason: str,
        now: str,
        keep_shelter_id: str | None = None,
    ) -> None:
        """人员转运/离点时，取消其尚未执行的发放安排。"""
        planned = self.store.all(
            "SELECT * FROM supply_movements WHERE person_id = ? AND status = 'planned'",
            (person_id,),
        )
        for movement in planned:
            if keep_shelter_id and movement["from_shelter_id"] == keep_shelter_id:
                continue
            self.store.conn.execute(
                "UPDATE supply_movements SET status = 'cancelled' WHERE id = ?", (movement["id"],)
            )
            self._emit(
                "supply_distribution_cancelled",
                actor=actor,
                role=role,
                subject_type="supply_batch",
                subject_id=movement["batch_id"],
                payload={"movement_id": movement["id"], "person_id": person_id, "reason": reason},
                now=now,
            )

    def return_supplies(
        self,
        actor: str,
        role: str,
        *,
        movement_id: str,
        quantity: int,
        reason: str,
    ) -> str:
        """退回：已领取物资部分或全部退回库存。"""
        parsed = require_role(
            role, {Role.LOGISTICS, Role.RECEPTION, Role.MEDICAL, Role.SUPERVISOR}, "退回物资"
        )
        now = self._now()
        with self.store.tx():
            movement = self._get("supply_movements", movement_id, "发放记录")
            if movement["movement_type"] not in {"distribute", "reissue"} or movement["status"] != "completed":
                raise ValidationError("只能对已完成的发放办理退回")
            linked = self.store.all(
                "SELECT movement_type, quantity FROM supply_movements WHERE linked_movement_id = ?"
                " AND status = 'completed'",
                (movement_id,),
            )
            returned = sum(r["quantity"] for r in linked if r["movement_type"] == "return")
            handed = sum(r["quantity"] for r in linked if r["movement_type"] == "handover")
            if quantity <= 0 or returned + handed + quantity > movement["quantity"]:
                raise ValidationError("退回数量超出可处分数量（已扣除既往退回与转交）")
            person = self._person(movement["person_id"])
            to_shelter = person["current_shelter_id"] or movement["from_shelter_id"]
            new_id = self._record_movement_tx(
                actor, parsed, batch_id=movement["batch_id"], movement_type="return",
                quantity=quantity, person_id=movement["person_id"], to_shelter_id=to_shelter,
                reason=reason, linked_movement_id=movement_id, now=now,
            )
            self._refresh_medicine_resupply_tx(movement["batch_id"], actor, parsed, now)
        return new_id

    def hand_over(
        self,
        actor: str,
        role: str,
        *,
        movement_id: str,
        to_person_id: str,
        quantity: int,
        reason: str,
    ) -> str:
        """转交：已领取物资在人员之间转移（如家属代管），库存不变。"""
        parsed = require_role(
            role, {Role.LOGISTICS, Role.RECEPTION, Role.MEDICAL, Role.SUPERVISOR}, "转交物资"
        )
        now = self._now()
        with self.store.tx():
            movement = self._get("supply_movements", movement_id, "发放记录")
            if movement["movement_type"] not in {"distribute", "reissue"} or movement["status"] != "completed":
                raise ValidationError("只能转交已完成的发放")
            linked = self.store.all(
                "SELECT movement_type, quantity FROM supply_movements WHERE linked_movement_id = ?"
                " AND status = 'completed'",
                (movement_id,),
            )
            returned = sum(r["quantity"] for r in linked if r["movement_type"] == "return")
            handed = sum(r["quantity"] for r in linked if r["movement_type"] == "handover")
            if quantity <= 0 or returned + handed + quantity > movement["quantity"]:
                raise ValidationError("转交数量超出可处分数量（已扣除退回与既往转交）")
            target = self._person(self._resolve_person_id(to_person_id))
            new_id = self._record_movement_tx(
                actor, parsed, batch_id=movement["batch_id"], movement_type="handover",
                quantity=quantity, person_id=movement["person_id"], to_person_id=target["id"],
                reason=reason, linked_movement_id=movement_id, now=now,
            )
        return new_id

    def transfer_stock(
        self,
        actor: str,
        role: str,
        *,
        batch_id: str,
        to_shelter_id: str,
        quantity: int,
        reason: str,
    ) -> str:
        """库存跨安置点调拨；批次总量不变。"""
        parsed = require_role(role, {Role.LOGISTICS, Role.COORDINATOR, Role.SUPERVISOR}, "调拨物资")
        now = self._now()
        with self.store.tx():
            batch = self._batch(batch_id)
            self._require_batch_access(parsed, batch, "调拨")
            self._shelter(to_shelter_id)
            stock = self.batch_stock(batch_id)
            from_shelter = None
            for shelter_id, amount in stock.items():
                if shelter_id != to_shelter_id and amount >= quantity:
                    from_shelter = shelter_id
                    break
            if from_shelter is None:
                raise ConflictError("没有足够库存可调拨")
            new_id = self._record_movement_tx(
                actor, parsed, batch_id=batch_id, movement_type="transfer", quantity=quantity,
                from_shelter_id=from_shelter, to_shelter_id=to_shelter_id, reason=reason, now=now,
            )
        return new_id

    def reissue(
        self,
        actor: str,
        role: str,
        *,
        movement_id: str,
        quantity: int,
        reason: str,
    ) -> str:
        """补发：原发放无法兑现（遗失/留失）时重新发放，与原记录挂钩。"""
        parsed = require_role(
            role, {Role.LOGISTICS, Role.MEDICAL, Role.SUPERVISOR}, "补发物资"
        )
        now = self._now()
        with self.store.tx():
            movement = self._get("supply_movements", movement_id, "发放记录")
            if movement["movement_type"] not in {"distribute", "reissue"} or movement["status"] != "completed":
                raise ValidationError("只能基于已完成的发放补发")
            batch = self._batch(movement["batch_id"])
            self._require_batch_access(parsed, batch, "补发")
            person = self._person(movement["person_id"])
            if person["state"] != "sheltered":
                raise ConflictError("人员不在安置点，无法补发")
            stock = self.batch_stock(movement["batch_id"])
            if stock.get(person["current_shelter_id"], 0) < quantity:
                raise ConflictError("人员所在安置点现存不足，需先调拨")
            new_id = self._record_movement_tx(
                actor, parsed, batch_id=movement["batch_id"], movement_type="reissue",
                quantity=quantity, person_id=person["id"],
                from_shelter_id=person["current_shelter_id"], reason=reason,
                linked_movement_id=movement_id, now=now,
            )
            self._refresh_medicine_resupply_tx(movement["batch_id"], actor, parsed, now)
        return new_id

    def batch_ledger(self, actor: str, role: str, batch_id: str) -> dict[str, Any]:
        """批次台账：批次信息、现存分布与全部移动流水。"""
        parsed = require_role(role, _STAFF_ROLES, "查看批次台账")
        batch = self._batch(batch_id)
        self._require_batch_access(parsed, batch, "查看")
        return {
            "batch": batch,
            "stock_by_shelter": self.batch_stock(batch_id),
            "remaining": self.batch_remaining(batch_id),
            "movements": self._movements_tx(batch_id, completed_only=False),
        }

    def verify_conservation(self, actor: str, role: str, batch_id: str) -> dict[str, Any]:
        """守恒校验：批次总量 = 现存合计 + 在人员手中净发放量 + 损耗调整。"""
        parsed = require_role(role, _STAFF_ROLES, "校验批次守恒")
        batch = self._batch(batch_id)
        self._require_batch_access(parsed, batch, "校验")
        distributed = returned = lost = gained = 0
        for mv in self._movements_tx(batch_id):
            if mv["movement_type"] in {"distribute", "reissue"}:
                distributed += mv["quantity"]
            elif mv["movement_type"] == "return":
                returned += mv["quantity"]
            elif mv["movement_type"] == "adjust":
                if mv["from_shelter_id"]:
                    lost += mv["quantity"]
                if mv["to_shelter_id"]:
                    gained += mv["quantity"]
        in_hands = distributed - returned
        stock = self.batch_stock(batch_id)
        on_shelves = sum(stock.values())
        expected = batch["quantity"] + gained - lost - in_hands
        if on_shelves != expected or any(v < 0 for v in stock.values()):
            raise ConservationError(
                f"批次 {batch_id} 守恒校验失败: 现存 {on_shelves} != 应有 {expected}"
            )
        return {
            "batch_id": batch_id,
            "quantity": batch["quantity"],
            "distributed_net": in_hands,
            "lost": lost,
            "gained": gained,
            "on_shelves": on_shelves,
            "balanced": True,
        }

    # ------------------------------------------------------------------
    # 回执幂等与冲突上交
    # ------------------------------------------------------------------

    def submit_receipt(
        self,
        actor: str,
        role: str,
        *,
        kind: str,
        source_team: str,
        external_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """处理队伍回执。

        同一队伍同一回执号只处理一次；关键内容不一致时不重复执行，
        生成冲突记录交主管核对。
        """
        parsed = require_role(
            role, {Role.RECEPTION, Role.LOGISTICS, Role.COORDINATOR, Role.SUPERVISOR}, "提交回执"
        )
        if kind not in {"evacuation_arrival", "supply_delivery"}:
            raise ValidationError(f"未知回执类型: {kind}")
        digest = _payload_hash(payload)
        now = self._now()
        with self.store.tx():
            existing = self.store.one(
                "SELECT * FROM receipts WHERE source_team = ? AND external_id = ?",
                (source_team, external_id),
            )
            if existing is not None:
                if existing["payload_hash"] == digest:
                    return {"status": "duplicate", "receipt": existing["external_id"],
                            "result": json.loads(existing["result"])}
                conflict_id = self._new_id("cfl")
                self.store.conn.execute(
                    "INSERT INTO conflicts(id, source_team, external_id, kind, existing_payload,"
                    " incoming_payload, status, created_at) VALUES (?, ?, ?, ?, ?, ?, 'open', ?)",
                    (
                        conflict_id, source_team, external_id, kind,
                        existing["payload"], json.dumps(payload, ensure_ascii=False, sort_keys=True),
                        now,
                    ),
                )
                self._emit(
                    "receipt_conflict_raised",
                    actor=actor,
                    role=parsed,
                    subject_type="conflict",
                    subject_id=conflict_id,
                    payload={"kind": kind, "source_team": source_team, "external_id": external_id},
                    now=now,
                )
                return {"status": "conflict", "conflict_id": conflict_id}
            result = self._apply_receipt_tx(actor, parsed, kind, payload, now)
            self.store.conn.execute(
                "INSERT INTO receipts(source_team, external_id, kind, payload, payload_hash, result, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    source_team, external_id, kind,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True), digest,
                    json.dumps(result, ensure_ascii=False), now,
                ),
            )
            return {"status": "applied", "result": result}

    def _apply_receipt_tx(
        self, actor: str, role: Role, kind: str, payload: dict[str, Any], now: str
    ) -> dict[str, Any]:
        if kind == "supply_delivery":
            batch_id = self._register_supply_batch_tx(
                actor, role,
                kind=payload["kind"], item=payload["item"], quantity=int(payload["quantity"]),
                unit=payload["unit"], shelter_id=payload["shelter_id"],
                sensitivity=payload.get("sensitivity"),
                resupply_level=int(payload.get("resupply_level", 0)), now=now,
            )
            return {"batch_id": batch_id}
        if kind == "evacuation_arrival":
            batch = self._get("evacuation_batches", payload["batch_id"], "撤离批次")
            headcount = int(payload["arrived_headcount"])
            self.store.conn.execute(
                "UPDATE evacuation_batches SET status = 'arrived', arrived_at = ?,"
                " arrived_headcount = ? WHERE id = ?",
                (payload.get("arrived_at", now), headcount, batch["id"]),
            )
            registered = self.store.one(
                "SELECT COUNT(*) AS n FROM persons WHERE batch_id = ? AND identity_status != 'merged'",
                (batch["id"],),
            )["n"]
            discrepancy = headcount - registered
            self._emit(
                "evacuation_arrival_confirmed",
                actor=actor,
                role=role,
                subject_type="evacuation_batch",
                subject_id=batch["id"],
                payload={
                    "team": batch["team"],
                    "arrived_headcount": headcount,
                    "registered_count": registered,
                    "discrepancy": discrepancy,
                },
                now=now,
            )
            return {"batch_id": batch["id"], "discrepancy": discrepancy}
        raise ValidationError(f"未知回执类型: {kind}")

    def resolve_conflict(
        self,
        actor: str,
        role: str,
        *,
        conflict_id: str,
        decision: str,
        note: str | None = None,
    ) -> None:
        """主管核对冲突：保留原记录，或按来件补正（只追加，不改写历史）。"""
        parsed = require_role(role, {Role.SUPERVISOR}, "核对冲突")
        if decision not in {"keep_original", "apply_incoming"}:
            raise ValidationError(f"未知处理决定: {decision}")
        now = self._now()
        with self.store.tx():
            conflict = self._get("conflicts", conflict_id, "冲突")
            if conflict["status"] != "open":
                raise ConflictError("冲突已处理")
            incoming = json.loads(conflict["incoming_payload"])
            existing = json.loads(conflict["existing_payload"])
            correction: dict[str, Any] = {}
            if decision == "apply_incoming":
                if conflict["kind"] == "supply_delivery":
                    delta = int(incoming["quantity"]) - int(existing["quantity"])
                    if delta != 0:
                        batch_id = self._find_delivery_batch_tx(conflict)
                        self._record_movement_tx(
                            actor, parsed, batch_id=batch_id, movement_type="adjust",
                            quantity=abs(delta),
                            from_shelter_id=existing["shelter_id"] if delta < 0 else None,
                            to_shelter_id=existing["shelter_id"] if delta > 0 else None,
                            reason=f"回执冲突补正({conflict_id})", now=now,
                        )
                        correction = {"batch_id": batch_id, "delta": delta}
                elif conflict["kind"] == "evacuation_arrival":
                    batch = self._get("evacuation_batches", incoming["batch_id"], "撤离批次")
                    self.store.conn.execute(
                        "UPDATE evacuation_batches SET arrived_headcount = ? WHERE id = ?",
                        (int(incoming["arrived_headcount"]), batch["id"]),
                    )
                    self._emit(
                        "arrival_headcount_corrected",
                        actor=actor,
                        role=parsed,
                        subject_type="evacuation_batch",
                        subject_id=batch["id"],
                        payload={
                            "from": existing["arrived_headcount"],
                            "to": incoming["arrived_headcount"],
                            "conflict_id": conflict_id,
                        },
                        now=now,
                    )
                    correction = {"batch_id": batch["id"]}
            self.store.conn.execute(
                "UPDATE conflicts SET status = 'resolved', resolution = ?, resolved_by = ?,"
                " resolved_at = ? WHERE id = ?",
                (decision, actor, now, conflict_id),
            )
            self._emit(
                "conflict_resolved",
                actor=actor,
                role=parsed,
                subject_type="conflict",
                subject_id=conflict_id,
                payload={"decision": decision, "note": note, "correction": correction},
                now=now,
            )

    def _find_delivery_batch_tx(self, conflict: dict[str, Any]) -> str:
        """按回执的处理结果定位已入账批次（冲突补正用）。"""
        receipt = self.store.one(
            "SELECT result FROM receipts WHERE source_team = ? AND external_id = ?",
            (conflict["source_team"], conflict["external_id"]),
        )
        if receipt is None:
            raise NotFoundError("找不到冲突对应的回执")
        batch_id = json.loads(receipt["result"]).get("batch_id")
        if batch_id is None:
            raise NotFoundError("回执结果中没有批次信息")
        self._batch(batch_id)
        return batch_id
