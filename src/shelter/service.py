"""安置与家庭团聚后端领域服务。

一个 Backend 实例绑定一个 SQLite 连接与时钟，覆盖：
社区/家庭/人员临时档案与合并、身份核验、撤离批次、移动与安排联动、
安置点容量、分岗敏感资料、物资只追加台账（退/转/补）、寻亲线索与团聚依据、
回执幂等与主管冲突队列。所有关键动作写审计日志。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import timedelta
from typing import Any, Iterable

from .access import SENSITIVE_CATEGORIES, can_access
from .clock import Clock, now_iso, parse_iso, utc_clock
from .errors import (
    CapacityExceeded,
    Conflict,
    InsufficientStock,
    NotFound,
    PermissionDenied,
    ReceiptConflict,
)
from .store import dumps


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


# 各移动类型对在途安排的调整策略。
_CASCADE_RULES: dict[str, dict[str, str]] = {
    "transfer": {
        "transfer": "follow",          # 后续转运改写为从新安置点出发
        "cross_district": "follow",
        "medication_pickup": "follow", # 药品领取改到新安置点
        "reunion_visit": "follow",
        "discharge": "cancel",
        "return_home": "cancel",
    },
    "cross_district": {
        "transfer": "follow",
        "cross_district": "follow",
        "medication_pickup": "follow",
        "reunion_visit": "follow",
        "discharge": "cancel",
        "return_home": "cancel",
    },
    "discharge": {
        "transfer": "cancel",
        "cross_district": "cancel",
        "discharge": "cancel",
        "medication_pickup": "follow",
        "reunion_visit": "follow",
        "return_home": "cancel",
    },
    "return_home": {
        "transfer": "cancel",
        "cross_district": "cancel",
        "discharge": "cancel",
        "medication_pickup": "cancel",
        "reunion_visit": "follow",
        "return_home": "cancel",
    },
}

# 回执类型 -> 判断“关键内容”的字段。
_RECEIPT_CRITICAL_FIELDS: dict[str, tuple[str, ...]] = {
    "supply_arrival": ("item_name", "category", "unit", "quantity_in", "shelter_code"),
    "supply_issue": ("batch_code", "person_code", "quantity"),
}


class Backend:
    def __init__(self, conn: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.conn = conn
        self.clock = clock or utc_clock()

    # ------------------------------------------------------------------ 基础

    def _now(self) -> str:
        return now_iso(self.clock)

    def _audit(
        self,
        action: str,
        entity_type: str,
        entity_id: str | None,
        *,
        operator_id: str | None = None,
        before: Any = None,
        after: Any = None,
        reason: str | None = None,
        role: str | None = None,
    ) -> None:
        operator = operator_id
        if role is None and operator_id:
            row = self.conn.execute(
                "SELECT role FROM operators WHERE id = ?", (operator_id,)
            ).fetchone()
            role = row["role"] if row else None
        self.conn.execute(
            """INSERT INTO audit_log
               (id, at, operator_id, role, action, entity_type, entity_id,
                before_state, after_state, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                _new_id("aud"),
                self._now(),
                operator,
                role,
                action,
                entity_type,
                entity_id,
                dumps(before) if before is not None else None,
                dumps(after) if after is not None else None,
                reason,
            ),
        )

    def _operator(self, operator_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM operators WHERE id = ? AND active = 1", (operator_id,)
        ).fetchone()
        if row is None:
            raise NotFound("经办人不存在或已停用")
        return row

    def _require_role(self, operator_id: str, roles: Iterable[str]) -> sqlite3.Row:
        row = self._operator(operator_id)
        if row["role"] not in set(roles):
            raise PermissionDenied(f"岗位 {row['role']} 无权执行该操作")
        return row

    def register_operator(
        self, code: str, name: str, role: str, *, team: str | None = None
    ) -> str:
        if role not in (
            "medical",
            "child_protection",
            "elder_care",
            "shelter_staff",
            "logistics",
            "rescue",
            "coordinator",
        ):
            raise ValueError(f"未知岗位: {role}")
        oid = _new_id("op")
        self.conn.execute(
            """INSERT INTO operators (id, code, name, role, team, active, created_at)
               VALUES (?,?,?,?,?,1,?)""",
            (oid, code, name, role, team, self._now()),
        )
        return oid

    def register_community(
        self, name: str, *, district: str | None = None, province: str | None = None,
        created_by: str | None = None,
    ) -> str:
        cid = _new_id("com")
        self.conn.execute(
            """INSERT INTO communities (id, name, district, province, created_at, created_by)
               VALUES (?,?,?,?,?,?)""",
            (cid, name, district, province, self._now(), created_by),
        )
        return cid

    def create_family(
        self, *, community_id: str | None = None, note: str | None = None,
        created_by: str | None = None,
    ) -> str:
        fid = _new_id("fam")
        self.conn.execute(
            """INSERT INTO families (id, community_id, status, note, created_at, created_by)
               VALUES (?,?, 'active', ?,?,?)""",
            (fid, community_id, note, self._now(), created_by),
        )
        return fid

    def register_shelter(
        self, name: str, *, district: str | None = None,
        capacity_total: int = 0, capacity_accessible: int = 0,
    ) -> str:
        sid = _new_id("shl")
        self.conn.execute(
            """INSERT INTO shelters
               (id, name, district, capacity_total, capacity_accessible, status, created_at)
               VALUES (?,?,?,?,?, 'active', ?)""",
            (sid, name, district, capacity_total, capacity_accessible, self._now()),
        )
        return sid

    # ------------------------------------------------- 人员临时档案与家庭关系

    def intake_person(
        self,
        *,
        received_location: str,
        received_by_team: str,
        received_by_operator: str,
        received_at: str | None = None,
        shelter_id: str | None = None,
        family_id: str | None = None,
        full_name: str | None = None,
        nickname: str | None = None,
        age_group: str | None = None,
        is_child: bool = False,
        is_elder: bool = False,
        language: str | None = None,
        display_code: str | None = None,
        provisional: bool = False,
    ) -> str:
        """接收人员。身份不明者建 unidentified 临时档案；自称身份未核用 provisional。

        原始接收地点、时间、经办队伍与经办人落库后即不可更改（数据库触发器强制）。
        """
        received_at = received_at or self._now()
        pid = _new_id("per")
        display_code = display_code or f"TMP-{uuid.uuid4().hex[:8].upper()}"
        # 提供了姓名或自称身份即进入待核状态，否则为身份不明临时档案。
        status = "provisional" if (full_name or provisional) else "unidentified"
        self.conn.execute(
            """INSERT INTO persons
               (id, family_id, display_code, status, full_name, nickname, gender,
                age_group, language, is_child, is_elder,
                received_at, received_location, received_by_team,
                received_by_operator, shelter_at_intake_id, created_at)
               VALUES (?,?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?)""",
            (
                pid, family_id, display_code, status, full_name, nickname,
                age_group, language, int(is_child), int(is_elder),
                received_at, received_location, received_by_team,
                received_by_operator, shelter_id, self._now(),
            ),
        )
        if shelter_id is not None:
            self._record_movement_rows(
                pid, "intake", None, shelter_id, received_at,
                reason="接收入住", operator_id=None,
            )
        self._audit("intake", "person", pid, after={"display_code": display_code})
        return pid

    def add_relationship(
        self, person_id: str, other_person_id: str, relation: str, *,
        source: str, confirmed: bool = False, created_by: str | None = None,
    ) -> str:
        rid = _new_id("rel")
        self.conn.execute(
            """INSERT INTO family_relationships
               (id, person_id, other_person_id, relation, confirmed, source,
                created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?)""",
            (rid, person_id, other_person_id, relation, int(confirmed), source,
             self._now(), created_by),
        )
        return rid

    # --------------------------------------------------------- 身份核验与合并

    def add_verification(
        self, person_id: str, method: str, *,
        evidence_ref: str | None = None, confidence: int = 0,
        created_by: str | None = None, note: str | None = None,
    ) -> str:
        vid = _new_id("ver")
        self.conn.execute(
            """INSERT INTO identity_verifications
               (id, person_id, method, evidence_ref, confidence, status,
                verifier_id, created_at, note)
               VALUES (?,?,?,?,?, 'pending', NULL, ?,?)""",
            (vid, person_id, method, evidence_ref, confidence, self._now(), note),
        )
        return vid

    def decide_verification(
        self, verification_id: str, *, confirm: bool, operator_id: str,
        note: str | None = None,
    ) -> None:
        row = self.conn.execute(
            "SELECT * FROM identity_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
        if row is None:
            raise NotFound("核验记录不存在")
        if row["status"] != "pending":
            raise Conflict("核验已作出结论")
        self.conn.execute(
            """UPDATE identity_verifications
               SET status = ?, verifier_id = ?, decided_at = ?, note = ?
               WHERE id = ?""",
            ("confirmed" if confirm else "rejected", operator_id, self._now(),
             note, verification_id),
        )
        if confirm:
            self.conn.execute(
                "UPDATE persons SET status = 'verified' WHERE id = ?",
                (row["person_id"],),
            )
        self._audit(
            "verification_decision", "identity_verification", verification_id,
            operator_id=operator_id, before={"status": "pending"},
            after={"status": "confirmed" if confirm else "rejected"}, reason=note,
        )

    def merge_persons(
        self, kept_person_id: str, merged_person_id: str, *,
        verification_id: str, reason: str, operator_id: str,
        adopt_family: bool = True,
    ) -> str:
        """线索确认后合并档案。

        - 必须提供已确认的身份核验作为合并依据；
        - 被合并档案整行保留（含原始接收地点/时间/经办人），只落合并指针；
        - 挂在被合并档案下的移动、物资、线索、关系都继续可查；
        - 所有查询通过 canonical_person 解析到保留档案。
        """
        if kept_person_id == merged_person_id:
            raise Conflict("不能与自身合并")
        kept = self._person(kept_person_id)
        merged = self._person(merged_person_id)
        if kept["merged_into_person_id"] or merged["merged_into_person_id"]:
            raise Conflict("档案已被合并，请先解析到当前档案")
        if not verification_id:
            raise Conflict("合并必须依据已确认的核验或线索，不能无据合并")
        v = self.conn.execute(
            "SELECT * FROM identity_verifications WHERE id = ?", (verification_id,)
        ).fetchone()
        if v is None or v["status"] != "confirmed":
            raise Conflict("合并必须依据已确认的核验或线索")

        before = {"merged_display_code": merged["display_code"]}
        self.conn.execute(
            """UPDATE persons
               SET merged_into_person_id = ?, merged_at = ?, merge_reason = ?
               WHERE id = ?""",
            (kept_person_id, self._now(), reason, merged_person_id),
        )
        if adopt_family and merged["family_id"] and not kept["family_id"]:
            self.conn.execute(
                "UPDATE persons SET family_id = ? WHERE id = ?",
                (merged["family_id"], kept_person_id),
            )
        if merged["status"] == "verified" and kept["status"] != "verified":
            self.conn.execute(
                "UPDATE persons SET status='verified' WHERE id=?",
                (kept_person_id,),
            )
        mid = _new_id("mrg")
        self.conn.execute(
            """INSERT INTO person_merges
               (id, kept_person_id, merged_person_id, verification_id, reason,
                at, operator_id)
               VALUES (?,?,?,?,?,?,?)""",
            (mid, kept_person_id, merged_person_id, verification_id, reason,
             self._now(), operator_id),
        )
        self._audit(
            "person_merge", "person", merged_person_id, operator_id=operator_id,
            before=before, after={"merged_into": kept_person_id}, reason=reason,
        )
        return mid

    def _person(self, person_id: str) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT * FROM persons WHERE id = ?", (person_id,)
        ).fetchone()
        if row is None:
            raise NotFound("人员档案不存在")
        return row

    def canonical_person(self, person_id: str) -> str:
        """沿合并指针解析到当前保留档案。"""
        seen: set[str] = set()
        current = person_id
        while True:
            row = self.conn.execute(
                "SELECT merged_into_person_id FROM persons WHERE id = ?", (current,)
            ).fetchone()
            if row is None:
                raise NotFound(f"人员档案不存在: {person_id}")
            if row["merged_into_person_id"] is None:
                return current
            if current in seen:
                raise Conflict("合并指针出现循环")
            seen.add(current)
            current = row["merged_into_person_id"]

    def find_person_by_code(self, display_code: str) -> str:
        row = self.conn.execute(
            "SELECT id FROM persons WHERE display_code = ?", (display_code,)
        ).fetchone()
        if row is None:
            raise NotFound(f"找不到编号 {display_code} 的档案")
        return row["id"]

    # ------------------------------------------------------------- 撤离批次

    def create_evacuation_batch(
        self, code: str, *, origin_location: str, team: str,
        vehicle_ref: str | None = None, destination_shelter_id: str | None = None,
        note: str | None = None,
    ) -> str:
        bid = _new_id("bat")
        self.conn.execute(
            """INSERT INTO evacuation_batches
               (id, code, origin_location, team, vehicle_ref,
                destination_shelter_id, status, created_at, note)
               VALUES (?,?,?,?,?,?, 'planned', ?,?)""",
            (bid, code, origin_location, team, vehicle_ref,
             destination_shelter_id, self._now(), note),
        )
        return bid

    def assign_to_batch(self, batch_id: str, person_id: str) -> str:
        aid = _new_id("bas")
        self.conn.execute(
            """INSERT INTO batch_assignments
               (id, batch_id, person_id, status, created_at)
               VALUES (?,?,?, 'assigned', ?)""",
            (aid, batch_id, person_id, self._now()),
        )
        return aid

    def depart_batch(self, batch_id: str, *, departed_at: str | None = None) -> None:
        self.conn.execute(
            "UPDATE evacuation_batches SET status='in_transit', departed_at=? WHERE id=?",
            (departed_at or self._now(), batch_id),
        )

    def arrive_batch(
        self, batch_id: str, shelter_id: str, *,
        arrived_at: str | None = None, arrived_person_ids: Iterable[str] | None = None,
        allow_overflow: bool = False,
    ) -> None:
        """批次到达：登记安置点入住移动；未点名者标 no_show，便于追查分流。"""
        at = arrived_at or self._now()
        batch = self.conn.execute(
            "SELECT * FROM evacuation_batches WHERE id = ?", (batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("撤离批次不存在")
        arrived_set = set(arrived_person_ids or [])
        assignments = self.conn.execute(
            "SELECT * FROM batch_assignments WHERE batch_id = ?", (batch_id,)
        ).fetchall()
        arriving = [
            a for a in assignments
            if not arrived_set or a["person_id"] in arrived_set
        ]
        arriving_ids = {a["id"] for a in arriving}
        if arriving:
            self._check_capacity(shelter_id, len(arriving),
                                 allow_overflow=allow_overflow)
        for a in assignments:
            if a["id"] not in arriving_ids:
                self.conn.execute(
                    "UPDATE batch_assignments SET status='no_show' WHERE id=?", (a["id"],)
                )
                continue
            self.conn.execute(
                "UPDATE batch_assignments SET status='arrived', boarded_at=COALESCE(boarded_at,?), arrived_at=? WHERE id=?",
                (batch["departed_at"] or at, at, a["id"]),
            )
            self._record_movement_rows(
                a["person_id"], "intake", None, shelter_id, at,
                batch_id=batch_id, reason=f"随批次 {batch['code']} 到达入住",
            )
        self.conn.execute(
            """UPDATE evacuation_batches
               SET status='arrived', arrived_at=?, destination_shelter_id=?
               WHERE id=?""",
            (at, shelter_id, batch_id),
        )

    # ----------------------------------------------------- 移动与安排联动

    def record_movement(
        self, person_id: str, type_: str, *,
        to_shelter_id: str | None = None, effective_at: str | None = None,
        batch_id: str | None = None, reason: str | None = None,
        operator_id: str | None = None, allow_overflow: bool = False,
    ) -> str:
        """登记转运/出院/返家/跨区调剂，并联动调整尚未执行的安排。"""
        if type_ not in ("transfer", "cross_district", "discharge", "return_home"):
            raise ValueError("record_movement 只处理转运、出院、返家、跨区调剂")
        current = self.current_location(person_id)
        from_shelter = current["shelter_id"]
        at = effective_at or self._now()

        if to_shelter_id is not None and type_ in ("transfer", "cross_district"):
            self._check_capacity(to_shelter_id, 1, allow_overflow=allow_overflow)

        event_id = self._record_movement_rows(
            person_id, type_, from_shelter, to_shelter_id, at,
            batch_id=batch_id, reason=reason, operator_id=operator_id,
        )
        self._cascade_arrangements(person_id, type_, from_shelter, to_shelter_id, at, event_id)
        self._audit(
            "movement", "person_movement", event_id, operator_id=operator_id,
            after={"type": type_, "from": from_shelter, "to": to_shelter_id},
            reason=reason,
        )
        return event_id

    def _record_movement_rows(
        self, person_id: str, type_: str, from_shelter: str | None,
        to_shelter: str | None, at: str, *,
        batch_id: str | None = None, reason: str | None = None,
        operator_id: str | None = None, arrangement_id: str | None = None,
    ) -> str:
        mid = _new_id("mov")
        self.conn.execute(
            """INSERT INTO person_movements
               (id, person_id, type, from_shelter_id, to_shelter_id, effective_at,
                batch_id, arrangement_id, reason, recorded_by, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (mid, person_id, type_, from_shelter, to_shelter, at, batch_id,
             arrangement_id, reason, operator_id, self._now()),
        )
        return mid

    def _cascade_arrangements(
        self, person_id: str, movement_type: str,
        from_shelter: str | None, to_shelter: str | None, at: str, event_id: str,
    ) -> None:
        rules = _CASCADE_RULES[movement_type]
        pending = self.conn.execute(
            """SELECT * FROM arrangements
               WHERE person_id = ? AND status IN ('planned','awaiting_confirmation')""",
            (person_id,),
        ).fetchall()
        for arr in pending:
            action = rules.get(arr["type"])
            if action is None:
                continue
            if action == "cancel":
                self._resolve_alert(
                    f"pending-transfer:{arr['id']}:{arr['planned_at']}"
                )
                self.conn.execute(
                    """UPDATE arrangements
                       SET status='cancelled', event_id=?,
                           note=COALESCE(note,'') || ?
                       WHERE id=?""",
                    (event_id, f"｜因 {at} {movement_type} 事件取消", arr["id"]),
                )
                self._audit(
                    "arrangement_cancelled", "arrangement", arr["id"],
                    before={"type": arr["type"], "status": arr["status"]},
                    reason=f"移动事件 {event_id} 联动取消",
                )
            elif action == "follow":
                self._resolve_alert(
                    f"pending-transfer:{arr['id']}:{arr['planned_at']}"
                )
                self.conn.execute(
                    "UPDATE arrangements SET status='superseded', event_id=? WHERE id=?",
                    (event_id, arr["id"]),
                )
                new_payload = json.loads(arr["payload"] or "{}")
                new_payload.update({"superseded_by_event": event_id})
                new_id = _new_id("arr")
                new_to = to_shelter if arr["type"] in ("transfer", "cross_district") else arr["to_shelter_id"]
                self.conn.execute(
                    """INSERT INTO arrangements
                       (id, person_id, type, status, planned_at, from_shelter_id,
                        to_shelter_id, payload, event_id, follows_arrangement_id,
                        created_at, created_by, note)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (new_id, person_id, arr["type"], arr["status"], arr["planned_at"],
                     to_shelter or from_shelter, new_to, dumps(new_payload), event_id,
                     arr["id"], self._now(), arr["created_by"],
                     f"由 {arr['id']} 因 {movement_type} 接续调整"),
                )
                self._audit(
                    "arrangement_superseded", "arrangement", arr["id"],
                    after={"new_arrangement": new_id},
                    reason=f"移动事件 {event_id} 联动改派",
                )

    def execute_arrangement(
        self, arrangement_id: str, *, operator_id: str | None = None,
        executed_at: str | None = None,
    ) -> str | None:
        """执行一条安排：转运/跨区类会真正产生移动事件并再次联动后续安排。"""
        arr = self.conn.execute(
            "SELECT * FROM arrangements WHERE id=?", (arrangement_id,)
        ).fetchone()
        if arr is None:
            raise NotFound("安排不存在")
        if arr["status"] not in ("planned", "awaiting_confirmation", "in_progress"):
            raise Conflict(f"安排状态为 {arr['status']}，不能执行")
        if arr["type"] in ("transfer", "cross_district"):
            movement_id = self.record_movement(
                arr["person_id"], arr["type"],
                to_shelter_id=arr["to_shelter_id"],
                effective_at=executed_at,
                reason=f"执行安排 {arrangement_id}",
                operator_id=operator_id,
            )
        else:
            movement_id = None
        self.conn.execute(
            """UPDATE arrangements
               SET status='done', executed_at=? WHERE id=?""",
            (executed_at or self._now(), arrangement_id),
        )
        self._resolve_alert(
            f"pending-transfer:{arrangement_id}:{arr['planned_at']}"
        )
        self._audit("arrangement_executed", "arrangement", arrangement_id,
                    operator_id=operator_id, after={"movement": movement_id})
        return movement_id

    def create_arrangement(
        self, person_id: str, type_: str, *, planned_at: str,
        from_shelter_id: str | None = None, to_shelter_id: str | None = None,
        payload: dict[str, Any] | None = None,
        status: str = "planned", created_by: str | None = None,
        note: str | None = None,
    ) -> str:
        if type_ not in (
            "transfer", "cross_district", "discharge", "return_home",
            "medication_pickup", "reunion_visit",
        ):
            raise ValueError("未知安排类型")
        aid = _new_id("arr")
        self.conn.execute(
            """INSERT INTO arrangements
               (id, person_id, type, status, planned_at, from_shelter_id,
                to_shelter_id, payload, created_at, created_by, note)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (aid, person_id, type_, status, planned_at, from_shelter_id,
             to_shelter_id, dumps(payload or {}), self._now(), created_by, note),
        )
        return aid

    # ------------------------------------------------------------- 安置点容量

    def _canonical_map(self) -> dict[str, str]:
        rows = self.conn.execute(
            "SELECT id, merged_into_person_id FROM persons"
        ).fetchall()
        mapping = {r["id"]: r["id"] for r in rows}
        for pid in list(mapping):
            seen: set[str] = set()
            current = pid
            while True:
                row = self.conn.execute(
                    "SELECT merged_into_person_id FROM persons WHERE id=?", (current,)
                ).fetchone()
                target = row["merged_into_person_id"] if row else None
                if not target:
                    mapping[pid] = current
                    break
                if current in seen:
                    break
                seen.add(current)
                current = target
        return mapping

    def shelter_occupancy(self) -> dict[str, dict[str, int]]:
        """各安置点当前在住人数（含无障碍需求人数），由移动台账推导。"""
        canon = self._canonical_map()
        latest: dict[str, sqlite3.Row] = {}
        for row in self.conn.execute(
            # rowid 随插入单调递增，作为同秒事件的稳定次序兜底。
            "SELECT * FROM person_movements ORDER BY effective_at, created_at, rowid"
        ):
            pid = canon.get(row["person_id"], row["person_id"])
            latest[pid] = row
        counts: dict[str, dict[str, int]] = defaultdict(lambda: {"total": 0, "accessible": 0})
        in_shelter = {pid for pid, r in latest.items() if r["to_shelter_id"]}
        for pid, row in latest.items():
            sid = row["to_shelter_id"]
            if sid:
                counts[sid]["total"] += 1
        if in_shelter:
            marks = self.conn.execute(
                f"""SELECT person_id FROM sensitive_records
                    WHERE category='accessibility' AND person_id IN
                    ({','.join('?' * len(in_shelter))})""",
                tuple(in_shelter),
            ).fetchall()
            for m in marks:
                pid = canon.get(m["person_id"], m["person_id"])
                loc = latest.get(pid)
                if loc and loc["to_shelter_id"]:
                    counts[loc["to_shelter_id"]]["accessible"] += 1
        return dict(counts)

    def _check_capacity(
        self, shelter_id: str, adding: int, *, allow_overflow: bool
    ) -> None:
        shelter = self.conn.execute(
            "SELECT * FROM shelters WHERE id=?", (shelter_id,)
        ).fetchone()
        if shelter is None:
            raise NotFound("安置点不存在")
        current = self.shelter_occupancy().get(shelter_id, {}).get("total", 0)
        if current + adding > shelter["capacity_total"] and not allow_overflow:
            raise CapacityExceeded(
                f"安置点 {shelter['name']} 容量 {shelter['capacity_total']}，"
                f"当前 {current}，再增 {adding} 将超员"
            )

    def current_location(self, person_id: str) -> dict[str, Any]:
        pid = self.canonical_person(person_id)
        rows = self.conn.execute(
            """SELECT * FROM person_movements
               WHERE person_id IN (
                   SELECT id FROM persons WHERE id=?
                   UNION ALL SELECT id FROM persons WHERE merged_into_person_id=?
               )
               ORDER BY effective_at DESC, created_at DESC, rowid DESC""",
            (pid, pid),
        ).fetchall()
        if not rows:
            return {"shelter_id": None, "type": None, "at": None}
        r = rows[0]
        return {
            "shelter_id": r["to_shelter_id"],
            "type": r["type"],
            "at": r["effective_at"],
            "movement_id": r["id"],
        }

    # ------------------------------------------------------- 敏感资料与授权

    def add_sensitive_record(
        self, person_id: str, category: str, data: dict[str, Any], *,
        operator_id: str,
    ) -> str:
        operator = self._operator(operator_id)
        if category not in SENSITIVE_CATEGORIES:
            raise ValueError("未知敏感资料类别")
        if not can_access(operator["role"], category):
            raise PermissionDenied(f"岗位 {operator['role']} 不能登记{category}资料")
        rid = _new_id("sen")
        self.conn.execute(
            """INSERT INTO sensitive_records
               (id, person_id, category, data, created_at, created_by)
               VALUES (?,?,?,?,?,?)""",
            (rid, person_id, category, dumps(data), self._now(), operator_id),
        )
        return rid

    def read_sensitive_records(
        self, person_id: str, category: str, *, operator_id: str, purpose: str,
    ) -> list[dict[str, Any]]:
        """读取敏感资料；岗位不符则拒绝并同样留痕。"""
        operator = self._operator(operator_id)
        granted = can_access(operator["role"], category)
        self.conn.execute(
            """INSERT INTO access_log
               (id, operator_id, role, person_id, category, purpose, granted, at)
               VALUES (?,?,?,?,?,?,?,?)""",
            (_new_id("acl"), operator_id, operator["role"], person_id, category,
             purpose, int(granted), self._now()),
        )
        if not granted:
            raise PermissionDenied(f"岗位 {operator['role']} 无权查看{category}资料")
        rows = self.conn.execute(
            """SELECT * FROM sensitive_records
               WHERE person_id=? AND category=? ORDER BY created_at""",
            (person_id, category),
        ).fetchall()
        return [
            {"id": r["id"], "data": json.loads(r["data"]), "created_at": r["created_at"]}
            for r in rows
        ]

    # ----------------------------------------------------------- 物资守恒台账

    def receive_supply_batch(
        self, code: str, item_name: str, category: str, unit: str,
        quantity_in: int, *, arrived_at: str | None = None,
        shelter_id: str | None = None, source_team: str | None = None,
        created_by: str | None = None, note: str | None = None,
    ) -> str:
        if quantity_in <= 0:
            raise ValueError("入库数量必须为正")
        batch_id = _new_id("sup")
        self.conn.execute(
            """INSERT INTO supply_batches
               (id, code, shelter_id, item_name, category, unit, quantity_in,
                source_team, arrived_at, created_by, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (batch_id, code, shelter_id, item_name, category, unit, quantity_in,
             source_team, arrived_at or self._now(), created_by, note, self._now()),
        )
        self._post_ledger(
            batch_id, "receive", quantity_in, at=arrived_at,
            operator_id=created_by, note="入库",
        )
        return batch_id

    def _post_ledger(
        self, batch_id: str, entry_type: str, quantity: int, *,
        person_id: str | None = None, ref_ledger_id: str | None = None,
        receipt_id: str | None = None, at: str | None = None,
        operator_id: str | None = None, note: str | None = None,
    ) -> str:
        lid = _new_id("led")
        self.conn.execute(
            """INSERT INTO stock_ledger
               (id, batch_id, entry_type, person_id, quantity, ref_ledger_id,
                receipt_id, at, operator_id, note, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (lid, batch_id, entry_type, person_id, quantity, ref_ledger_id,
             receipt_id, at or self._now(), operator_id, note, self._now()),
        )
        return lid

    def batch_balance(self, batch_id: str) -> int:
        """批次在库结余：入库与退回为正，发放/补发为负；人员间转交成对抵消。"""
        sign = {
            "receive": 1, "return": 1, "transfer_in": 1, "correction_in": 1,
            "issue": -1, "reissue": -1, "transfer_out": -1, "correction_out": -1,
        }
        total = 0
        for r in self.conn.execute(
            "SELECT entry_type, quantity FROM stock_ledger WHERE batch_id=?",
            (batch_id,),
        ):
            total += sign[r["entry_type"]] * r["quantity"]
        return total

    def person_holding(self, person_id: str) -> dict[str, int]:
        """某人当前持有的各物资批次数量，由台账推导。"""
        pid = self.canonical_person(person_id)
        alias_ids = [r["id"] for r in self.conn.execute(
            "SELECT id FROM persons WHERE id=? OR merged_into_person_id=?", (pid, pid)
        )]
        sign = {
            "issue": 1, "reissue": 1, "transfer_in": 1,
            "return": -1, "transfer_out": -1, "receive": 0,
            "correction_in": 0, "correction_out": 0,
        }
        holding: dict[str, int] = defaultdict(int)
        if alias_ids:
            placeholders = ",".join("?" * len(alias_ids))
            for r in self.conn.execute(
                f"""SELECT batch_id, entry_type, quantity FROM stock_ledger
                    WHERE person_id IN ({placeholders})""",
                tuple(alias_ids),
            ):
                holding[r["batch_id"]] += sign[r["entry_type"]] * r["quantity"]
        return {k: v for k, v in holding.items() if v != 0}

    def issue_supply(
        self, batch_id: str, person_id: str, quantity: int, *,
        operator_id: str | None = None, note: str | None = None,
        receipt_id: str | None = None, ref_ledger_id: str | None = None,
        entry_type: str = "issue",
    ) -> str:
        if quantity <= 0:
            raise ValueError("发放数量必须为正")
        if entry_type not in ("issue", "reissue"):
            raise ValueError("发放类型只能是 issue 或 reissue")
        if self.batch_balance(batch_id) < quantity:
            raise InsufficientStock("该批次库存不足以发放")
        lid = self._post_ledger(
            batch_id, entry_type, quantity, person_id=person_id,
            ref_ledger_id=ref_ledger_id, receipt_id=receipt_id,
            operator_id=operator_id, note=note,
        )
        self._audit("supply_issue", "stock_ledger", lid, operator_id=operator_id,
                    after={"batch": batch_id, "person": person_id, "qty": quantity})
        return lid

    def return_supply(
        self, batch_id: str, person_id: str, quantity: int, *,
        operator_id: str | None = None, note: str | None = None,
    ) -> str:
        """已领取物资退回：个人持有减少，批次库存回补。"""
        if self.person_holding(person_id).get(batch_id, 0) < quantity:
            raise Conflict("该人员并未持有这么多该批次物资，无法退回")
        lid = self._post_ledger(
            batch_id, "return", quantity, person_id=person_id,
            operator_id=operator_id, note=note,
        )
        self._audit("supply_return", "stock_ledger", lid, operator_id=operator_id,
                    reason=note)
        return lid

    def transfer_supply(
        self, batch_id: str, from_person_id: str, to_person_id: str, quantity: int, *,
        operator_id: str | None = None, note: str | None = None,
    ) -> tuple[str, str]:
        """已领取物资转交给另一人：成对登记，库存不变，持有关系平移，不删除原发放。"""
        if from_person_id == to_person_id:
            raise Conflict("转交对象不能是同一人")
        if self.person_holding(from_person_id).get(batch_id, 0) < quantity:
            raise Conflict("转交人持有数量不足")
        out_id = self._post_ledger(
            batch_id, "transfer_out", quantity, person_id=from_person_id,
            operator_id=operator_id, note=f"转交：{note or ''}",
        )
        in_id = self._post_ledger(
            batch_id, "transfer_in", quantity, person_id=to_person_id,
            ref_ledger_id=out_id, operator_id=operator_id,
            note=f"收到转交：{note or ''}",
        )
        self._audit("supply_transfer", "stock_ledger", out_id, operator_id=operator_id,
                    after={"to_person": to_person_id, "qty": quantity}, reason=note)
        return out_id, in_id

    def reissue_supply(
        self, batch_id: str, person_id: str, quantity: int, *,
        original_ledger_id: str, operator_id: str | None = None,
        note: str | None = None,
    ) -> str:
        """补发：必须挂接原发放行，台账保留两次发放痕迹。"""
        original = self.conn.execute(
            "SELECT * FROM stock_ledger WHERE id=?", (original_ledger_id,)
        ).fetchone()
        if original is None or original["entry_type"] not in ("issue", "reissue"):
            raise NotFound("原发放记录不存在")
        return self.issue_supply(
            batch_id, person_id, quantity, operator_id=operator_id,
            note=f"补发（原发放 {original_ledger_id}）：{note or ''}",
            ref_ledger_id=original_ledger_id, entry_type="reissue",
        )

    # ------------------------------------------------------------- 用药与补给

    def add_medication_regimen(
        self, person_id: str, medicine: str, interval_hours: int, supply_hours: int, *,
        dosage: str | None = None, last_issued_at: str | None = None,
        last_ledger_id: str | None = None, prescriber: str | None = None,
        created_by: str,
    ) -> str:
        self._require_role(created_by, ("medical",))
        issued = parse_iso(last_issued_at or self._now())
        next_due = (issued + timedelta(hours=interval_hours)).isoformat(timespec="seconds")
        rid = _new_id("med")
        self.conn.execute(
            """INSERT INTO medication_regimens
               (id, person_id, medicine, dosage, interval_hours, supply_hours,
                last_ledger_id, last_issued_at, next_due_at, status, prescriber,
                created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?,?, 'active', ?,?,?)""",
            (rid, person_id, medicine, dosage, interval_hours, supply_hours,
             last_ledger_id, last_issued_at or self._now(), next_due, prescriber,
             self._now(), created_by),
        )
        return rid

    def mark_medication_issued(self, regimen_id: str, ledger_id: str) -> None:
        regimen = self.conn.execute(
            "SELECT * FROM medication_regimens WHERE id=?", (regimen_id,)
        ).fetchone()
        if regimen is None:
            raise NotFound("用药方案不存在")
        next_due = (
            parse_iso(self._now()) + timedelta(hours=regimen["interval_hours"])
        ).isoformat(timespec="seconds")
        self.conn.execute(
            """UPDATE medication_regimens
               SET last_ledger_id=?, last_issued_at=?, next_due_at=?
               WHERE id=?""",
            (ledger_id, self._now(), next_due, regimen_id),
        )
        self._resolve_alert(f"med-resupply:{regimen_id}:{regimen['next_due_at']}")
        due_rows = self.conn.execute(
            """SELECT id, planned_at FROM arrangements
               WHERE json_extract(payload, '$.regimen_id')=?
                 AND status IN ('planned','awaiting_confirmation','in_progress')""",
            (regimen_id,),
        ).fetchall()
        for row in due_rows:
            self._resolve_alert(f"pending-transfer:{row['id']}:{row['planned_at']}")
        self.conn.execute(
            """UPDATE arrangements SET status='done', executed_at=?
               WHERE json_extract(payload, '$.regimen_id')=?
                 AND status IN ('planned','awaiting_confirmation','in_progress')""",
            (self._now(), regimen_id),
        )

    # --------------------------------------------------------- 寻亲线索与团聚

    def create_tracing_lead(
        self, *, sought_name: str | None = None,
        sought_detail: dict[str, Any] | None = None,
        seeker_person_id: str | None = None, contact: str | None = None,
        source: str, source_team: str | None = None,
        created_by: str | None = None,
    ) -> str:
        lid = _new_id("lead")
        code = f"LEAD-{uuid.uuid4().hex[:8].upper()}"
        self.conn.execute(
            """INSERT INTO tracing_leads
               (id, code, seeker_person_id, sought_name, sought_detail, contact,
                source, source_team, status, created_at, created_by)
               VALUES (?,?,?,?,?,?,?,?, 'new', ?,?)""",
            (lid, code, seeker_person_id, sought_name,
             dumps(sought_detail or {}), contact, source, source_team,
             self._now(), created_by),
        )
        return lid

    def match_lead(self, lead_id: str, *, min_score: int = 40) -> list[dict[str, Any]]:
        """对一条线索打分并生成候选（proposed），不自动认定。"""
        lead = self.conn.execute(
            "SELECT * FROM tracing_leads WHERE id=?", (lead_id,)
        ).fetchone()
        if lead is None:
            raise NotFound("线索不存在")
        detail = json.loads(lead["sought_detail"] or "{}")
        results: list[dict[str, Any]] = []
        for person in self.conn.execute(
            "SELECT * FROM persons WHERE merged_into_person_id IS NULL"
        ):
            score, reasons = self._score_person(person, lead, detail)
            if score < min_score:
                continue
            existing = self.conn.execute(
                "SELECT * FROM lead_links WHERE lead_id=? AND person_id=?",
                (lead_id, person["id"]),
            ).fetchone()
            if existing:
                # 候选已由周期匹配或此前手动匹配生成：沿用既有结论，不重复造记录。
                if existing["status"] != "rejected":
                    results.append({
                        "link_id": existing["id"], "person_id": person["id"],
                        "display_code": person["display_code"],
                        "score": existing["score"],
                        "reasons": json.loads(existing["reasons"]),
                        "status": existing["status"],
                    })
                continue
            link_id = _new_id("lnk")
            self.conn.execute(
                """INSERT INTO lead_links
                   (id, lead_id, person_id, score, reasons, status, created_at)
                   VALUES (?,?,?,?,?, 'proposed', ?)""",
                (link_id, lead_id, person["id"], score, dumps(reasons), self._now()),
            )
            results.append({
                "link_id": link_id, "person_id": person["id"],
                "display_code": person["display_code"], "score": score, "reasons": reasons,
            })
        results.sort(key=lambda x: x["score"], reverse=True)
        return results

    def _score_person(
        self, person: sqlite3.Row, lead: sqlite3.Row, detail: dict[str, Any]
    ) -> tuple[int, list[str]]:
        score = 0
        reasons: list[str] = []
        wanted_name = (lead["sought_name"] or "").strip()
        if wanted_name and person["full_name"]:
            if wanted_name == person["full_name"]:
                score += 40
                reasons.append("姓名一致")
        if detail.get("nickname") and person["nickname"] == detail["nickname"]:
            score += 20
            reasons.append("昵称一致")
        if detail.get("age_group") and person["age_group"] == detail["age_group"]:
            score += 15
            reasons.append("年龄层一致")
        if detail.get("language") and person["language"] == detail["language"]:
            score += 5
            reasons.append("语言一致")
        family = self.conn.execute(
            "SELECT community_id FROM families WHERE id=?",
            (person["family_id"],),
        ).fetchone() if person["family_id"] else None
        if detail.get("community_id") and family and family["community_id"] == detail["community_id"]:
            score += 15
            reasons.append("原属社区一致")
        # 关系线索双向识别：母亲→child 与 孩子→mother 应互为佐证。
        reverse_of = {
            "father": ("child",), "mother": ("child",), "child": ("father", "mother"),
            "spouse": ("spouse",), "sibling": ("sibling",),
        }
        for rel_name, bonus in (("father", 25), ("mother", 25), ("child", 25),
                                ("spouse", 25), ("sibling", 10)):
            related_name = detail.get(f"relative_{rel_name}")
            if not related_name:
                continue
            hit = self.conn.execute(
                """SELECT 1 FROM family_relationships fr JOIN persons p ON p.id = fr.person_id
                   WHERE fr.other_person_id=? AND fr.relation=? AND p.full_name=?
                   UNION ALL
                   SELECT 1 FROM family_relationships fr JOIN persons p ON p.id = fr.other_person_id
                   WHERE fr.person_id=? AND fr.relation=? AND p.full_name=?""",
                (person["id"], rel_name, related_name,
                 person["id"], rel_name, related_name),
            ).fetchone()
            if not hit:
                for rev in reverse_of.get(rel_name, ()):
                    hit = self.conn.execute(
                        """SELECT 1 FROM family_relationships fr
                           JOIN persons named ON named.id = fr.person_id
                           WHERE fr.other_person_id=? AND fr.relation=?
                             AND named.full_name=?""",
                        (person["id"], rev, related_name),
                    ).fetchone()
                    if hit:
                        break
            if hit:
                score += bonus
                reasons.append(f"登记关系中存在{rel_name} {related_name}")
        # 同队不同时间点的交叉佐证
        if lead["source_team"] and person["received_by_team"] == lead["source_team"]:
            score += 5
            reasons.append("接收队伍与线索来源一致")
        return min(score, 100), reasons

    def accept_match(
        self, link_id: str, *, operator_id: str,
    ) -> None:
        """经办岗位接受候选：线索进入 matched，仍需团聚确认环节。"""
        self._require_role(operator_id, (
            "coordinator", "child_protection", "shelter_staff", "elder_care",
        ))
        link = self.conn.execute(
            "SELECT * FROM lead_links WHERE id=?", (link_id,)
        ).fetchone()
        if link is None:
            raise NotFound("候选匹配不存在")
        if link["status"] != "proposed":
            raise Conflict("候选已有结论")
        self.conn.execute(
            "UPDATE lead_links SET status='accepted' WHERE id=?", (link_id,)
        )
        self.conn.execute(
            """UPDATE tracing_leads
               SET status='matched', matched_person_id=?
               WHERE id=? AND status='new'""",
            (link["person_id"], link["lead_id"]),
        )
        self._audit("match_accepted", "lead_link", link_id, operator_id=operator_id)

    def reject_match(self, link_id: str, *, operator_id: str) -> None:
        self._require_role(operator_id, (
            "coordinator", "child_protection", "shelter_staff", "elder_care",
        ))
        self.conn.execute(
            "UPDATE lead_links SET status='rejected' WHERE id=?", (link_id,)
        )
        self._audit("match_rejected", "lead_link", link_id, operator_id=operator_id)

    def confirm_reunion(
        self, lead_id: str, *, family_id: str, operator_id: str,
        note: str | None = None,
    ) -> str:
        """主管依据已接受的线索候选作出团聚决定；线索与决定互相留证。"""
        self._require_role(operator_id, ("coordinator", "child_protection"))
        lead = self.conn.execute(
            "SELECT * FROM tracing_leads WHERE id=?", (lead_id,)
        ).fetchone()
        if lead is None:
            raise NotFound("线索不存在")
        accepted = self.conn.execute(
            "SELECT COUNT(*) AS n FROM lead_links WHERE lead_id=? AND status='accepted'",
            (lead_id,),
        ).fetchone()["n"]
        if accepted < 1:
            raise Conflict("团聚确认必须至少依据一条已接受的匹配线索")
        decision_id = _new_id("reu")
        person_id = lead["matched_person_id"]
        self.conn.execute(
            """INSERT INTO reunion_decisions
               (id, family_id, person_id, decided_by, decided_at, note)
               VALUES (?,?,?,?,?,?)""",
            (decision_id, family_id, person_id, operator_id, self._now(), note),
        )
        self.conn.execute(
            "INSERT INTO reunion_evidence (decision_id, lead_id) VALUES (?,?)",
            (decision_id, lead_id),
        )
        self.conn.execute(
            """UPDATE tracing_leads
               SET status='confirmed', decided_at=?, decided_by=?
               WHERE id=?""",
            (self._now(), operator_id, lead_id),
        )
        # 团聚后归入家庭（若尚未归属）。
        self.conn.execute(
            "UPDATE persons SET family_id=? WHERE id=? AND family_id IS NULL",
            (family_id, person_id),
        )
        self._audit(
            "reunion_confirmed", "reunion_decision", decision_id,
            operator_id=operator_id, after={"family": family_id, "person": person_id},
            reason=note,
        )
        return decision_id

    def family_timeline(self, family_id: str) -> list[dict[str, Any]]:
        """家庭完整时间线：每个成员的原始接收、批次、移动、合并、团聚依据。

        下一班据此说明：家庭为何被拆分、成员当时在哪里、最后依据哪些线索团聚。
        """
        events: list[dict[str, Any]] = []
        members = self.conn.execute(
            "SELECT * FROM persons WHERE family_id=?", (family_id,)
        ).fetchall()
        aliases = self.conn.execute(
            """SELECT m.*, p.display_code AS merged_code
               FROM person_merges m JOIN persons p ON p.id = m.merged_person_id
               WHERE m.kept_person_id IN
                   (SELECT id FROM persons WHERE family_id=?)""",
            (family_id,),
        ).fetchall()

        for person in list(members):
            events.append({
                "at": person["received_at"], "kind": "intake",
                "person_id": person["id"], "display_code": person["display_code"],
                "detail": {
                    "location": person["received_location"],
                    "team": person["received_by_team"],
                    "operator": person["received_by_operator"],
                    "status_at_intake": person["status"],
                },
            })
        for a in aliases:
            merged = self.conn.execute(
                "SELECT * FROM persons WHERE id=?", (a["merged_person_id"],)
            ).fetchone()
            events.append({
                "at": merged["received_at"], "kind": "intake",
                "person_id": a["kept_person_id"],
                "display_code": merged["display_code"],
                "detail": {
                    "location": merged["received_location"],
                    "team": merged["received_by_team"],
                    "operator": merged["received_by_operator"],
                    "via_merged_profile": True,
                },
            })
            events.append({
                "at": a["at"], "kind": "merge",
                "person_id": a["kept_person_id"],
                "display_code": merged["display_code"],
                "detail": {"reason": a["reason"], "merged_into": a["kept_person_id"]},
            })

        member_codes = {m["id"]: m["display_code"] for m in members}
        if members:
            placeholders = ",".join("?" * len(members))
            for mv in self.conn.execute(
                f"""SELECT pm.*, eb.code AS batch_code FROM person_movements pm
                    LEFT JOIN evacuation_batches eb ON eb.id = pm.batch_id
                    WHERE pm.person_id IN ({placeholders})
                    ORDER BY pm.effective_at""",
                [m["id"] for m in members],
            ):
                events.append({
                    "at": mv["effective_at"], "kind": "movement",
                    "person_id": mv["person_id"],
                    "display_code": member_codes.get(mv["person_id"]),
                    "detail": {
                        "type": mv["type"], "from": mv["from_shelter_id"],
                        "to": mv["to_shelter_id"], "reason": mv["reason"],
                        "batch_code": mv["batch_code"],
                    },
                })
        for d in self.conn.execute(
            """SELECT rd.*, (
                   SELECT GROUP_CONCAT(lead_id) FROM reunion_evidence
                   WHERE decision_id = rd.id
               ) AS lead_ids
               FROM reunion_decisions rd WHERE rd.family_id=?
               ORDER BY rd.decided_at""",
            (family_id,),
        ):
            person = self.conn.execute(
                "SELECT display_code FROM persons WHERE id=?", (d["person_id"],)
            ).fetchone()
            events.append({
                "at": d["decided_at"], "kind": "reunion",
                "person_id": d["person_id"],
                "display_code": person["display_code"] if person else None,
                "detail": {"decision_id": d["id"], "lead_ids": d["lead_ids"],
                           "note": d["note"]},
            })
        events.sort(key=lambda e: (e["at"], e["kind"]))
        return events

    def family_handoff(self, family_id: str) -> dict[str, Any]:
        """夜班交接视图：为何拆分、各成员现在何处、最后依据哪些线索团聚。"""
        members = self.conn.execute(
            "SELECT * FROM persons WHERE family_id=?", (family_id,)
        ).fetchall()
        member_view = []
        for m in members:
            location = self.current_location(m["id"])
            shelter_name = None
            if location["shelter_id"]:
                sh = self.conn.execute(
                    "SELECT name FROM shelters WHERE id=?",
                    (location["shelter_id"],),
                ).fetchone()
                shelter_name = sh["name"] if sh else None
            member_view.append({
                "person_id": m["id"], "display_code": m["display_code"],
                "status": m["status"], "full_name": m["full_name"],
                "is_child": bool(m["is_child"]), "is_elder": bool(m["is_elder"]),
                "received_at": m["received_at"],
                "received_location": m["received_location"],
                "received_by_team": m["received_by_team"],
                "received_by_operator": m["received_by_operator"],
                "current_shelter_id": location["shelter_id"],
                "current_shelter_name": shelter_name,
                "last_movement_type": location["type"],
                "last_movement_at": location["at"],
            })
        decisions = self.conn.execute(
            """SELECT rd.*, GROUP_CONCAT(re.lead_id) AS lead_ids
               FROM reunion_decisions rd
               LEFT JOIN reunion_evidence re ON re.decision_id = rd.id
               WHERE rd.family_id=? GROUP BY rd.id ORDER BY rd.decided_at""",
            (family_id,),
        ).fetchall()
        reunion_view = []
        for d in decisions:
            leads = []
            for lead_id in (d["lead_ids"] or "").split(","):
                lead = self.conn.execute(
                    "SELECT code, source, source_team, matched_person_id FROM tracing_leads WHERE id=?",
                    (lead_id,),
                ).fetchone()
                if lead:
                    leads.append(dict(lead))
            reunion_view.append({
                "decision_id": d["id"], "decided_at": d["decided_at"],
                "decided_by": d["decided_by"], "note": d["note"], "leads": leads,
            })
        return {
            "family_id": family_id,
            "members": member_view,
            "separation": self.separation_summary(family_id),
            "timeline": self.family_timeline(family_id),
            "reunions": reunion_view,
        }

    def due_arrangements(self) -> list[dict[str, Any]]:
        """交接视图：所有尚未执行、已到点的安排（只返回最新一条接续链）。"""
        now = self._now()
        rows = self.conn.execute(
            """SELECT a.*, p.display_code FROM arrangements a
               JOIN persons p ON p.id = a.person_id
               WHERE a.status IN ('planned','awaiting_confirmation','in_progress')
                 AND a.planned_at <= ?
               ORDER BY a.planned_at""",
            (now,),
        ).fetchall()
        return [dict(r) for r in rows]

    def separation_summary(self, family_id: str) -> dict[str, Any]:
        """说明家庭为何被拆分：逐成员的撤离批次、队伍、去向，并给出汇总结论。"""
        members = self.conn.execute(
            "SELECT id, display_code FROM persons WHERE family_id=?", (family_id,)
        ).fetchall()
        if not members:
            return {"legs": [], "explanations": []}
        ids = [m["id"] for m in members]
        code_of = {m["id"]: m["display_code"] for m in members}
        placeholders = ",".join("?" * len(ids))
        legs = self.conn.execute(
            f"""SELECT pm.person_id, pm.to_shelter_id,
                      eb.id AS batch_id, eb.code AS batch_code, eb.team,
                      eb.origin_location, pm.effective_at
               FROM person_movements pm
               JOIN evacuation_batches eb ON eb.id = pm.batch_id
               WHERE pm.person_id IN ({placeholders})
               ORDER BY pm.effective_at""",
            ids,
        ).fetchall()
        leg_list = [
            {
                "person_id": r["person_id"], "display_code": code_of.get(r["person_id"]),
                "batch_code": r["batch_code"], "team": r["team"],
                "origin_location": r["origin_location"],
                "shelter_id": r["to_shelter_id"], "at": r["effective_at"],
            }
            for r in legs
        ]
        teams = {r["team"] for r in legs}
        shelters = {r["to_shelter_id"] for r in legs}
        explanations: list[str] = []
        if len(teams) > 1:
            explanations.append(
                f"家庭成员由 {len(teams)} 支救援队（{'、'.join(sorted(teams))}）"
                "分别撤离，批次不同导致最初分散"
            )
        if len(shelters) > 1:
            explanations.append(
                f"成员最初被送往 {len(shelters)} 个不同安置点："
                + "；".join(
                    f"{leg['display_code']} 经 {leg['team']} 批次 {leg['batch_code']} "
                    f"到 {leg['shelter_id']}"
                    for leg in leg_list
                )
            )
        if not explanations and leg_list:
            explanations.append("家庭成员由同一批次送达同一安置点，未发生撤离拆分")
        return {"legs": leg_list, "explanations": explanations}

    # ------------------------------------------------------------- 回执幂等

    @staticmethod
    def _payload_hash(payload: dict[str, Any]) -> str:
        return hashlib.sha256(
            dumps(payload).encode("utf-8")
        ).hexdigest()

    def ingest_receipt(
        self, code: str, source_team: str, payload: dict[str, Any], *,
        operator_id: str | None = None,
    ) -> dict[str, Any]:
        """接收救援队回执。

        - 编号与内容均相同：记 duplicate，只处理一次；
        - 编号相同但关键内容不一致：不记账，落冲突队列并预警，交主管核对；
        - 首次见到：应用副作用（入库/发放）后置 processed。
        """
        payload_hash = self._payload_hash(payload)
        existing = self.conn.execute(
            "SELECT * FROM receipts WHERE code=? ORDER BY received_at", (code,)
        ).fetchone()
        rid = _new_id("rcp")

        if existing is not None:
            if existing["payload_hash"] == payload_hash:
                self.conn.execute(
                    """INSERT INTO receipts
                       (id, code, source_team, payload_hash, payload, status,
                        applied_entity, applied_id, received_at, ingested_by)
                       VALUES (?,?,?,?,?, 'duplicate', ?, ?, ?, ?)""",
                    (rid, code, source_team, payload_hash, dumps(payload),
                     existing["applied_entity"], existing["applied_id"],
                     self._now(), operator_id),
                )
                return {"status": "duplicate", "reference_receipt": existing["id"]}

            conflicts = self._diff_critical(existing, payload)
            if not conflicts:
                self.conn.execute(
                    """INSERT INTO receipts
                       (id, code, source_team, payload_hash, payload, status,
                        applied_entity, applied_id, received_at, ingested_by)
                       VALUES (?,?,?,?,?, 'duplicate', ?, ?, ?, ?)""",
                    (rid, code, source_team, payload_hash, dumps(payload),
                     existing["applied_entity"], existing["applied_id"],
                     self._now(), operator_id),
                )
                return {"status": "duplicate", "reference_receipt": existing["id"],
                        "note": "非关键字段差异，关键内容一致"}

            self.conn.execute(
                """INSERT INTO receipts
                   (id, code, source_team, payload_hash, payload, status,
                    received_at, ingested_by)
                   VALUES (?,?,?,?,?, 'conflict', ?,?)""",
                (rid, code, source_team, payload_hash, dumps(payload),
                 self._now(), operator_id),
            )
            for field, old_value, new_value in conflicts:
                self.conn.execute(
                    """INSERT INTO receipt_conflicts
                       (id, receipt_id, existing_receipt_id, field, new_value,
                        old_value, status, created_at)
                       VALUES (?,?,?,?,?,?,'open',?)""",
                    (_new_id("cnf"), rid, existing["id"], field,
                     dumps(new_value), dumps(old_value), self._now()),
                )
            dedup = f"receipt-conflict:{code}"
            self._raise_alert(
                "receipt_conflict", "critical", "receipt", rid,
                f"回执 {code} 关键内容不一致，需主管核对",
                detail={"fields": [c[0] for c in conflicts], "team": source_team},
                dedup_key=dedup,
            )
            self._audit("receipt_conflict", "receipt", rid, operator_id=operator_id,
                        after={"fields": [c[0] for c in conflicts]})
            raise ReceiptConflict(
                f"回执 {code} 在字段 {[c[0] for c in conflicts]} 上与已存回执不一致"
            )

        entity, entity_id = self._apply_receipt_payload(payload, rid, operator_id)
        self.conn.execute(
            """INSERT INTO receipts
               (id, code, source_team, payload_hash, payload, status,
                applied_entity, applied_id, received_at, ingested_by)
               VALUES (?,?,?,?,?, 'processed', ?, ?, ?, ?)""",
            (rid, code, source_team, payload_hash, dumps(payload),
             entity, entity_id, self._now(), operator_id),
        )
        return {"status": "processed", "applied_entity": entity,
                "applied_id": entity_id, "receipt_id": rid}

    def _diff_critical(
        self, existing: sqlite3.Row, new_payload: dict[str, Any]
    ) -> list[tuple[str, Any, Any]]:
        old_payload = json.loads(existing["payload"])
        kind = old_payload.get("kind")
        if kind != new_payload.get("kind"):
            return [("kind", kind, new_payload.get("kind"))]
        fields = _RECEIPT_CRITICAL_FIELDS.get(kind, ())
        out = []
        for field in fields:
            if old_payload.get(field) != new_payload.get(field):
                out.append((field, old_payload.get(field), new_payload.get(field)))
        return out

    def _apply_receipt_payload(
        self, payload: dict[str, Any], receipt_id: str,
        operator_id: str | None,
    ) -> tuple[str, str]:
        kind = payload.get("kind")
        if kind == "supply_arrival":
            shelter_id = None
            if payload.get("shelter_code"):
                row = self.conn.execute(
                    "SELECT id FROM shelters WHERE name=?", (payload["shelter_code"],)
                ).fetchone()
                shelter_id = row["id"] if row else None
            existing_batch = self.conn.execute(
                "SELECT id FROM supply_batches WHERE code=?",
                (payload["batch_code"],),
            ).fetchone()
            if existing_batch:
                # 同编号批次已在库（例如实物先到、回执后补），幂等返回既有批次。
                return "supply_batch", existing_batch["id"]
            batch_id = self.receive_supply_batch(
                payload["batch_code"], payload["item_name"], payload["category"],
                payload["unit"], int(payload["quantity_in"]),
                arrived_at=payload.get("arrived_at"), shelter_id=shelter_id,
                source_team=payload.get("source_team"), created_by=operator_id,
                note=f"回执 {receipt_id} 自动入账",
            )
            return "supply_batch", batch_id
        if kind == "supply_issue":
            person_id = self.find_person_by_code(payload["person_code"])
            batch = self.conn.execute(
                "SELECT id FROM supply_batches WHERE code=?", (payload["batch_code"],)
            ).fetchone()
            if batch is None:
                raise NotFound(f"回执引用的物资批次 {payload['batch_code']} 不存在")
            lid = self.issue_supply(
                batch["id"], person_id, int(payload["quantity"]),
                operator_id=operator_id, receipt_id=receipt_id,
                note=f"回执 {receipt_id} 自动发放",
            )
            return "stock_ledger", lid
        raise ValueError(f"不支持自动入账的回执类型: {kind}")

    def _apply_supervisor_correction(
        self, payload: dict[str, Any], receipt_id: str, operator_id: str
    ) -> tuple[str, str]:
        """主管核对冲突回执后确认新数字。

        已按旧回执入库的批次不重复入库，而是追加一条 correction_in/out
        把在库结余修正到确认后的数量；批次尚不存在则正常入库。
        """
        if payload.get("kind") != "supply_arrival":
            # 发放等其他类型直接按新内容执行。
            return self._apply_receipt_payload(payload, receipt_id, operator_id)
        confirmed_qty = int(payload["quantity_in"])
        existing = self.conn.execute(
            "SELECT * FROM supply_batches WHERE code=?",
            (payload["batch_code"],),
        ).fetchone()
        if existing is None:
            return self._apply_receipt_payload(payload, receipt_id, operator_id)
        recorded_in = self.conn.execute(
            """SELECT COALESCE(SUM(CASE WHEN entry_type IN
                       ('receive','correction_in','return','transfer_in')
                       THEN quantity ELSE 0 END), 0)
                   - COALESCE(SUM(CASE WHEN entry_type IN
                       ('issue','reissue','transfer_out','correction_out')
                       THEN quantity ELSE 0 END), 0) AS n
               FROM stock_ledger WHERE batch_id=?""",
            (existing["id"],),
        ).fetchone()["n"]
        delta = confirmed_qty - recorded_in
        if delta != 0:
            entry = "correction_in" if delta > 0 else "correction_out"
            self._post_ledger(
                existing["id"], entry, abs(delta), receipt_id=receipt_id,
                operator_id=operator_id,
                note=f"主管核对回执 {receipt_id} 后修正：{recorded_in} → {confirmed_qty}",
            )
        self._audit(
            "supply_correction", "supply_batch", existing["id"],
            operator_id=operator_id,
            before={"recorded_in": recorded_in}, after={"confirmed_in": confirmed_qty},
        )
        return "supply_batch", existing["id"]

    def open_receipt_conflicts(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            """SELECT rc.*, r.code AS receipt_code FROM receipt_conflicts rc
               JOIN receipts r ON r.id = rc.receipt_id
               WHERE rc.status='open' ORDER BY rc.created_at"""
        ).fetchall()
        return [dict(r) for r in rows]

    def resolve_receipt_conflict(
        self, conflict_receipt_id: str, resolution: str, *, operator_id: str,
    ) -> dict[str, Any]:
        """主管核对冲突回执：accept_new 按新内容入账，keep_existing 维持原账。"""
        self._require_role(operator_id, ("coordinator",))
        if resolution not in ("accept_new", "keep_existing", "manual"):
            raise ValueError("解决方式必须是 accept_new / keep_existing / manual")
        receipt = self.conn.execute(
            "SELECT * FROM receipts WHERE id=?", (conflict_receipt_id,)
        ).fetchone()
        if receipt is None or receipt["status"] != "conflict":
            raise NotFound("没有处于冲突状态的该回执")
        applied_entity = applied_id = None
        if resolution == "accept_new":
            payload = json.loads(receipt["payload"])
            applied_entity, applied_id = self._apply_supervisor_correction(
                payload, conflict_receipt_id, operator_id
            )
            new_status = "processed"
        else:
            new_status = "duplicate" if resolution == "keep_existing" else "conflict"
        self.conn.execute(
            """UPDATE receipts SET status=?, applied_entity=?, applied_id=?
               WHERE id=?""",
            (new_status, applied_entity, applied_id, conflict_receipt_id),
        )
        self.conn.execute(
            """UPDATE receipt_conflicts SET status='resolved', resolution=?,
               resolved_by=?, resolved_at=? WHERE receipt_id=?""",
            (resolution, operator_id, self._now(), conflict_receipt_id),
        )
        self._resolve_alert(f"receipt-conflict:{receipt['code']}")
        self._audit(
            "receipt_conflict_resolved", "receipt", conflict_receipt_id,
            operator_id=operator_id, after={"resolution": resolution},
        )
        return {"status": new_status, "applied_entity": applied_entity,
                "applied_id": applied_id}

    # ----------------------------------------------------------------- 预警

    def _raise_alert(
        self, type_: str, severity: str, subject_type: str, subject_id: str | None,
        message: str, *, detail: dict[str, Any] | None = None,
        dedup_key: str | None = None,
    ) -> str | None:
        key = dedup_key or f"{type_}:{subject_type}:{subject_id}:{message}"
        existing = self.conn.execute(
            "SELECT id FROM alerts WHERE dedup_key=? AND status IN ('open','acked')",
            (key,),
        ).fetchone()
        if existing:
            return None
        aid = _new_id("alt")
        self.conn.execute(
            """INSERT INTO alerts
               (id, type, severity, subject_type, subject_id, message, detail,
                dedup_key, status, created_at)
               VALUES (?,?,?,?,?,?,?,?,'open',?)""",
            (aid, type_, severity, subject_type, subject_id, message,
             dumps(detail or {}), key, self._now()),
        )
        return aid

    def _resolve_alert(self, dedup_key: str) -> None:
        self.conn.execute(
            "UPDATE alerts SET status='resolved', resolved_at=? "
            "WHERE dedup_key=? AND status IN ('open','acked')",
            (self._now(), dedup_key),
        )

    def open_alerts(self, *, severity: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM alerts WHERE status='open'"
        params: list[Any] = []
        if severity:
            sql += " AND severity=?"
            params.append(severity)
        sql += " ORDER BY CASE severity WHEN 'critical' THEN 0 WHEN 'warning' THEN 1 ELSE 2 END, created_at"
        return [dict(r) for r in self.conn.execute(sql, params)]

    def check_capacity(self) -> list[dict[str, Any]]:
        """立即检查一次容量，生成/解除预警，返回当前越限安置点。"""
        occupancy = self.shelter_occupancy()
        findings: list[dict[str, Any]] = []
        for shelter in self.conn.execute("SELECT * FROM shelters WHERE status='active'"):
            n = occupancy.get(shelter["id"], {}).get("total", 0)
            acc = occupancy.get(shelter["id"], {}).get("accessible", 0)
            key = f"capacity:{shelter['id']}"
            if shelter["capacity_total"] == 0:
                # 容量未登记（0）的安置点不参与阈值预警，只统计无障碍床位。
                pass
            elif n >= shelter["capacity_total"]:
                self._raise_alert(
                    "capacity", "critical", "shelter", shelter["id"],
                    f"安置点 {shelter['name']} 已满员（{n}/{shelter['capacity_total']}）",
                    detail={"occupancy": n, "capacity": shelter["capacity_total"],
                            "accessible": acc},
                    dedup_key=key,
                )
                findings.append({"shelter_id": shelter["id"], "severity": "critical",
                                 "occupancy": n, "capacity": shelter["capacity_total"]})
            elif n >= shelter["capacity_total"] * 0.85:
                self._raise_alert(
                    "capacity", "warning", "shelter", shelter["id"],
                    f"安置点 {shelter['name']} 使用率超过 85%（{n}/{shelter['capacity_total']}）",
                    detail={"occupancy": n, "capacity": shelter["capacity_total"]},
                    dedup_key=key,
                )
                findings.append({"shelter_id": shelter["id"], "severity": "warning",
                                 "occupancy": n, "capacity": shelter["capacity_total"]})
            else:
                self._resolve_alert(key)
            if shelter["capacity_accessible"] and acc > shelter["capacity_accessible"]:
                self._raise_alert(
                    "capacity_accessible", "critical", "shelter", shelter["id"],
                    f"安置点 {shelter['name']} 无障碍床位不足（{acc}/{shelter['capacity_accessible']}）",
                    detail={"accessible": acc,
                            "capacity_accessible": shelter["capacity_accessible"]},
                    dedup_key=f"capacity-accessible:{shelter['id']}",
                )
        return findings

    def commit(self) -> None:
        self.conn.commit()
