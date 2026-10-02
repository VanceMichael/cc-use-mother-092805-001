"""安置与家庭团聚后端的 SQLite 结构。

设计要点：
- 人员、移动、物资台账、回执、审计全部只追加：触发器拒绝 DELETE。
- 人员的原始接收地点/时间/经办队伍/经办人不可 UPDATE，合并只加指针。
- 现状（当前所在安置点、物资结余）一律由台账推导，避免计数漂移。
"""

from __future__ import annotations

SCHEMA_VERSION = 2

# 禁止物理删除的台账表：纠正只能用冲抵、合并、作废等新记录表达。
PROTECTED_TABLES = (
    "operators",
    "communities",
    "shelters",
    "families",
    "persons",
    "family_relationships",
    "identity_verifications",
    "evacuation_batches",
    "batch_assignments",
    "person_movements",
    "sensitive_records",
    "access_log",
    "supply_batches",
    "stock_ledger",
    "medication_regimens",
    "tracing_leads",
    "lead_links",
    "reunion_decisions",
    "reunion_evidence",
    "receipts",
    "receipt_conflicts",
    "arrangements",
    "scheduled_jobs",
    "alerts",
    "audit_log",
    "person_merges",
)

_SCHEMA = """
CREATE TABLE operators (
    id          TEXT PRIMARY KEY,
    code        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    role        TEXT NOT NULL,
    team        TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE communities (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    district    TEXT,
    province    TEXT,
    created_at  TEXT NOT NULL,
    created_by  TEXT REFERENCES operators(id)
);

CREATE TABLE shelters (
    id                  TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    district            TEXT,
    capacity_total      INTEGER NOT NULL CHECK (capacity_total >= 0),
    capacity_accessible INTEGER NOT NULL DEFAULT 0 CHECK (capacity_accessible >= 0),
    status              TEXT NOT NULL DEFAULT 'active',
    created_at          TEXT NOT NULL
);

CREATE TABLE families (
    id           TEXT PRIMARY KEY,
    community_id TEXT REFERENCES communities(id),
    status       TEXT NOT NULL DEFAULT 'active',
    note         TEXT,
    created_at   TEXT NOT NULL,
    created_by   TEXT REFERENCES operators(id)
);

CREATE TABLE persons (
    id                      TEXT PRIMARY KEY,
    family_id               TEXT REFERENCES families(id),
    display_code            TEXT NOT NULL UNIQUE,
    -- unidentified 身份不明临时档案；provisional 自称身份待核；verified 已核验
    status                  TEXT NOT NULL
                            CHECK (status IN ('unidentified','provisional','verified')),
    full_name               TEXT,
    nickname                TEXT,
    gender                  TEXT,
    age_group               TEXT CHECK (age_group IN ('child','adult','elder') OR age_group IS NULL),
    language                TEXT,
    is_child                INTEGER NOT NULL DEFAULT 0,
    is_elder                INTEGER NOT NULL DEFAULT 0,
    -- 合并指针：被合并的临时档案整行保留，所有历史继续挂在它名下。
    merged_into_person_id   TEXT REFERENCES persons(id),
    merged_at               TEXT,
    merge_reason            TEXT,
    -- 以下四项为原始接收事实，建档后不可更改（见触发器）。
    received_at             TEXT NOT NULL,
    received_location       TEXT NOT NULL,
    received_by_team        TEXT NOT NULL,
    received_by_operator    TEXT NOT NULL,
    shelter_at_intake_id    TEXT REFERENCES shelters(id),
    created_at              TEXT NOT NULL
);

CREATE TABLE family_relationships (
    id            TEXT PRIMARY KEY,
    person_id     TEXT NOT NULL REFERENCES persons(id),
    other_person_id TEXT NOT NULL REFERENCES persons(id),
    relation      TEXT NOT NULL,
    confirmed     INTEGER NOT NULL DEFAULT 0,
    source        TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    created_by    TEXT REFERENCES operators(id),
    UNIQUE (person_id, other_person_id, relation)
);

CREATE TABLE identity_verifications (
    id            TEXT PRIMARY KEY,
    person_id     TEXT NOT NULL REFERENCES persons(id),
    method        TEXT NOT NULL,          -- document / witness / biometric / hotline_crosscheck ...
    evidence_ref  TEXT,                   -- 证据编号或转述，禁止存放无关敏感内容
    confidence    INTEGER NOT NULL DEFAULT 0 CHECK (confidence BETWEEN 0 AND 100),
    status        TEXT NOT NULL CHECK (status IN ('pending','confirmed','rejected')),
    verifier_id   TEXT REFERENCES operators(id),
    created_at    TEXT NOT NULL,
    decided_at    TEXT,
    note          TEXT
);

CREATE TABLE evacuation_batches (
    id                TEXT PRIMARY KEY,
    code              TEXT NOT NULL UNIQUE,
    origin_location   TEXT NOT NULL,
    team              TEXT NOT NULL,
    vehicle_ref       TEXT,
    destination_shelter_id TEXT REFERENCES shelters(id),
    status            TEXT NOT NULL DEFAULT 'planned'
                      CHECK (status IN ('planned','in_transit','arrived','cancelled')),
    departed_at       TEXT,
    arrived_at        TEXT,
    created_at        TEXT NOT NULL,
    note              TEXT
);

CREATE TABLE batch_assignments (
    id          TEXT PRIMARY KEY,
    batch_id    TEXT NOT NULL REFERENCES evacuation_batches(id),
    person_id   TEXT NOT NULL REFERENCES persons(id),
    status      TEXT NOT NULL DEFAULT 'assigned'
                CHECK (status IN ('assigned','arrived','no_show')),
    boarded_at  TEXT,
    arrived_at  TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (batch_id, person_id)
);

CREATE TABLE person_movements (
    id              TEXT PRIMARY KEY,
    person_id       TEXT NOT NULL REFERENCES persons(id),
    -- intake 接收入住 / transfer 区内转运 / cross_district 跨区调剂 /
    -- discharge 出院 / return_home 返家
    type            TEXT NOT NULL
                    CHECK (type IN ('intake','transfer','cross_district','discharge','return_home')),
    from_shelter_id TEXT REFERENCES shelters(id),
    to_shelter_id   TEXT REFERENCES shelters(id),
    effective_at    TEXT NOT NULL,
    batch_id        TEXT REFERENCES evacuation_batches(id),
    arrangement_id  TEXT,
    reason          TEXT,
    recorded_by     TEXT REFERENCES operators(id),
    created_at      TEXT NOT NULL
);

-- 敏感资料按类别分岗授权；读取必须在 access_log 留痕。
CREATE TABLE sensitive_records (
    id          TEXT PRIMARY KEY,
    person_id   TEXT NOT NULL REFERENCES persons(id),
    category    TEXT NOT NULL
                CHECK (category IN ('medical','child','elder','accessibility')),
    data        TEXT NOT NULL,            -- JSON 文本
    created_at  TEXT NOT NULL,
    created_by  TEXT REFERENCES operators(id)
);

CREATE TABLE access_log (
    id          TEXT PRIMARY KEY,
    operator_id TEXT REFERENCES operators(id),
    role        TEXT NOT NULL,
    person_id   TEXT REFERENCES persons(id),
    category    TEXT,
    purpose     TEXT,
    granted     INTEGER NOT NULL,
    at          TEXT NOT NULL
);

CREATE TABLE supply_batches (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL UNIQUE,
    shelter_id    TEXT REFERENCES shelters(id),
    item_name     TEXT NOT NULL,
    category      TEXT NOT NULL
                  CHECK (category IN ('medicine','infant','food','general')),
    unit          TEXT NOT NULL,
    quantity_in   INTEGER NOT NULL CHECK (quantity_in > 0),
    source_team   TEXT,
    arrived_at    TEXT NOT NULL,
    created_by    TEXT REFERENCES operators(id),
    note          TEXT,
    created_at    TEXT NOT NULL
);

-- 物资台账：只追加。receive 入库；issue 发放；return 退回；
-- transfer_out/transfer_in 成对表达转交；reissue 补发（挂原发放行）。
CREATE TABLE stock_ledger (
    id            TEXT PRIMARY KEY,
    batch_id      TEXT NOT NULL REFERENCES supply_batches(id),
    entry_type    TEXT NOT NULL
                  CHECK (entry_type IN
                         ('receive','issue','return','transfer_out','transfer_in',
                          'reissue','correction_in','correction_out')),
    person_id     TEXT REFERENCES persons(id),
    quantity      INTEGER NOT NULL CHECK (quantity > 0),
    ref_ledger_id TEXT REFERENCES stock_ledger(id),
    receipt_id    TEXT REFERENCES receipts(id),
    at            TEXT NOT NULL,
    operator_id   TEXT REFERENCES operators(id),
    note          TEXT,
    created_at    TEXT NOT NULL
);

CREATE TABLE medication_regimens (
    id             TEXT PRIMARY KEY,
    person_id      TEXT NOT NULL REFERENCES persons(id),
    medicine       TEXT NOT NULL,
    dosage         TEXT,
    interval_hours INTEGER NOT NULL CHECK (interval_hours > 0),
    supply_hours   INTEGER NOT NULL CHECK (supply_hours > 0),
    last_ledger_id TEXT REFERENCES stock_ledger(id),
    last_issued_at TEXT,
    next_due_at    TEXT NOT NULL,
    status         TEXT NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active','paused','completed')),
    prescriber     TEXT,
    created_at     TEXT NOT NULL,
    created_by     TEXT REFERENCES operators(id)
);

CREATE TABLE tracing_leads (
    id               TEXT PRIMARY KEY,
    code             TEXT NOT NULL UNIQUE,
    seeker_person_id TEXT REFERENCES persons(id),
    sought_name      TEXT,
    sought_detail    TEXT,                 -- JSON：年龄层、社区、特征等
    contact          TEXT,
    source           TEXT NOT NULL,        -- in_person / hotline / team_report / roster
    source_team      TEXT,
    status           TEXT NOT NULL DEFAULT 'new'
                     CHECK (status IN ('new','matched','confirmed','rejected')),
    matched_person_id TEXT REFERENCES persons(id),
    created_at       TEXT NOT NULL,
    created_by       TEXT REFERENCES operators(id),
    decided_at       TEXT,
    decided_by       TEXT REFERENCES operators(id),
    note             TEXT
);

CREATE TABLE lead_links (
    id         TEXT PRIMARY KEY,
    lead_id    TEXT NOT NULL REFERENCES tracing_leads(id),
    person_id  TEXT NOT NULL REFERENCES persons(id),
    score      INTEGER NOT NULL,
    reasons    TEXT NOT NULL,              -- JSON 打分依据
    status     TEXT NOT NULL DEFAULT 'proposed'
               CHECK (status IN ('proposed','accepted','rejected')),
    created_at TEXT NOT NULL,
    UNIQUE (lead_id, person_id)
);

CREATE TABLE reunion_decisions (
    id          TEXT PRIMARY KEY,
    family_id   TEXT REFERENCES families(id),
    person_id   TEXT NOT NULL REFERENCES persons(id),
    decided_by  TEXT REFERENCES operators(id),
    decided_at  TEXT NOT NULL,
    note        TEXT
);

CREATE TABLE reunion_evidence (
    decision_id TEXT NOT NULL REFERENCES reunion_decisions(id),
    lead_id     TEXT NOT NULL REFERENCES tracing_leads(id),
    PRIMARY KEY (decision_id, lead_id)
);

CREATE TABLE receipts (
    id            TEXT PRIMARY KEY,
    code          TEXT NOT NULL,
    source_team   TEXT NOT NULL,
    payload_hash  TEXT NOT NULL,
    payload       TEXT NOT NULL,           -- JSON 原文
    -- received 已收待处理 / processed 已入账 / duplicate 重复回执 / conflict 内容冲突待核
    status        TEXT NOT NULL DEFAULT 'received'
                  CHECK (status IN ('received','processed','duplicate','conflict')),
    applied_entity TEXT,
    applied_id    TEXT,
    received_at   TEXT NOT NULL,
    ingested_by   TEXT REFERENCES operators(id)
);

CREATE INDEX idx_receipts_code ON receipts(code);

CREATE TABLE receipt_conflicts (
    id                 TEXT PRIMARY KEY,
    receipt_id         TEXT NOT NULL REFERENCES receipts(id),
    existing_receipt_id TEXT NOT NULL REFERENCES receipts(id),
    field              TEXT NOT NULL,
    new_value          TEXT,
    old_value          TEXT,
    status             TEXT NOT NULL DEFAULT 'open'
                       CHECK (status IN ('open','resolved')),
    resolution         TEXT,              -- accept_new / keep_existing / manual
    resolved_by        TEXT REFERENCES operators(id),
    resolved_at        TEXT,
    created_at         TEXT NOT NULL
);

CREATE TABLE arrangements (
    id               TEXT PRIMARY KEY,
    person_id        TEXT NOT NULL REFERENCES persons(id),
    -- transfer / cross_district / discharge / return_home / medication_pickup / reunion_visit
    type             TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'planned'
                     CHECK (status IN
                            ('planned','awaiting_confirmation','in_progress',
                             'done','cancelled','superseded')),
    planned_at       TEXT NOT NULL,
    from_shelter_id  TEXT REFERENCES shelters(id),
    to_shelter_id    TEXT REFERENCES shelters(id),
    payload          TEXT,
    event_id         TEXT,                -- 触发调整的移动事件
    follows_arrangement_id TEXT REFERENCES arrangements(id),
    executed_at      TEXT,
    created_at       TEXT NOT NULL,
    created_by       TEXT REFERENCES operators(id),
    note             TEXT
);

CREATE TABLE scheduled_jobs (
    id               TEXT PRIMARY KEY,
    job_type         TEXT NOT NULL
                     CHECK (job_type IN
                            ('capacity_warning','match_missing',
                             'medication_resupply','pending_transfer')),
    payload          TEXT NOT NULL DEFAULT '{}',
    run_at           TEXT NOT NULL,
    interval_seconds INTEGER,             -- 非空则为周期任务，执行后自动续排
    status           TEXT NOT NULL DEFAULT 'scheduled'
                     CHECK (status IN ('scheduled','running','done','failed','cancelled')),
    idempotency_key  TEXT UNIQUE,
    run_count        INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE alerts (
    id          TEXT PRIMARY KEY,
    type        TEXT NOT NULL,
    severity    TEXT NOT NULL CHECK (severity IN ('info','warning','critical')),
    subject_type TEXT NOT NULL,           -- shelter / person / arrangement / system
    subject_id  TEXT,
    message     TEXT NOT NULL,
    detail      TEXT,
    dedup_key   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'
                CHECK (status IN ('open','acked','resolved')),
    created_at  TEXT NOT NULL,
    acked_by    TEXT REFERENCES operators(id),
    acked_at    TEXT,
    resolved_at TEXT
);

-- 同一活跃预警只保留一条；已解除的历史预警保留，且之后允许再次触发。
CREATE UNIQUE INDEX idx_alerts_active_dedup
    ON alerts(dedup_key) WHERE status IN ('open','acked');

CREATE TABLE audit_log (
    id          TEXT PRIMARY KEY,
    at          TEXT NOT NULL,
    operator_id TEXT,
    role        TEXT,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT,
    before_state TEXT,
    after_state  TEXT,
    reason       TEXT
);

CREATE TABLE person_merges (
    id              TEXT PRIMARY KEY,
    kept_person_id  TEXT NOT NULL REFERENCES persons(id),
    merged_person_id TEXT NOT NULL REFERENCES persons(id),
    verification_id TEXT REFERENCES identity_verifications(id),
    reason          TEXT NOT NULL,
    at              TEXT NOT NULL,
    operator_id     TEXT REFERENCES operators(id)
);
"""


def _trigger_sql() -> str:
    parts: list[str] = []
    for table in PROTECTED_TABLES:
        parts.append(
            f"CREATE TRIGGER trg_{table}_no_delete BEFORE DELETE ON {table}\n"
            f"BEGIN\n"
            f"  SELECT RAISE(ABORT, '{table} 为台账记录，禁止删除；请用冲抵或作废记录纠正');"
            f"\nEND;"
        )
    # 只追加表禁止任何 UPDATE。
    for table in ("stock_ledger", "audit_log", "access_log", "reunion_evidence"):
        parts.append(
            f"CREATE TRIGGER trg_{table}_no_update BEFORE UPDATE ON {table}\n"
            f"BEGIN\n"
            f"  SELECT RAISE(ABORT, '{table} 为只追加台账，禁止修改');"
            f"\nEND;"
        )
    parts.append(
        """
CREATE TRIGGER trg_persons_reception_immutable
BEFORE UPDATE OF received_at, received_location, received_by_team, received_by_operator ON persons
WHEN OLD.received_at IS NOT NEW.received_at
  OR OLD.received_location IS NOT NEW.received_location
  OR OLD.received_by_team IS NOT NEW.received_by_team
  OR OLD.received_by_operator IS NOT NEW.received_by_operator
BEGIN
  SELECT RAISE(ABORT, '原始接收地点、时间、经办队伍与经办人不得修改；合并档案也必须保留可查');
END;
"""
    )
    return "\n".join(parts)


def full_schema() -> str:
    return _SCHEMA + "\n" + _trigger_sql()
