"""SQLite 持久层：工作表 + 只追加的事件日志。

事件日志（events）是审计依据：接收地点/时间/经办人、家庭拆分原因、
团聚所依据的线索等都以事件形式保留，应用重启后仍可回放与查询。
工作表保存当前状态，与事件在同一事务内写入。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  type TEXT NOT NULL,
  actor TEXT NOT NULL,
  role TEXT NOT NULL,
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_type, subject_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type);

CREATE TABLE IF NOT EXISTS communities (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  province TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS shelters (
  id TEXT PRIMARY KEY,
  community_id TEXT NOT NULL REFERENCES communities(id),
  name TEXT NOT NULL,
  capacity INTEGER NOT NULL CHECK(capacity >= 0),
  occupancy INTEGER NOT NULL DEFAULT 0 CHECK(occupancy >= 0),
  status TEXT NOT NULL DEFAULT 'open',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS evacuation_batches (
  id TEXT PRIMARY KEY,
  team TEXT NOT NULL,
  source_community_id TEXT NOT NULL REFERENCES communities(id),
  dest_shelter_id TEXT NOT NULL REFERENCES shelters(id),
  status TEXT NOT NULL DEFAULT 'planned',
  planned_departure TEXT,
  arrived_at TEXT,
  arrived_headcount INTEGER,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS persons (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  name_normalized TEXT NOT NULL,
  sex TEXT,
  age_band TEXT NOT NULL,
  sensitivity TEXT NOT NULL DEFAULT 'general',
  identity_status TEXT NOT NULL,          -- temp_unverified | verified | merged
  is_temp INTEGER NOT NULL,
  id_document TEXT,
  state TEXT NOT NULL DEFAULT 'sheltered', -- sheltered | discharged | returned_home
  current_shelter_id TEXT REFERENCES shelters(id),
  registered_shelter_id TEXT NOT NULL,     -- 原接收地点（合并后仍保留）
  registered_at TEXT NOT NULL,             -- 原接收时间
  registered_by TEXT NOT NULL,             -- 原经办人
  batch_id TEXT,
  merged_into TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_persons_name ON persons(name_normalized, age_band);
CREATE INDEX IF NOT EXISTS idx_persons_shelter ON persons(current_shelter_id);

CREATE TABLE IF NOT EXISTS families (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'intact',   -- intact | separated | reunited
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS family_members (
  family_id TEXT NOT NULL REFERENCES families(id),
  person_id TEXT NOT NULL REFERENCES persons(id),
  relation TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  joined_at TEXT NOT NULL,
  left_at TEXT,
  PRIMARY KEY (family_id, person_id)
);

CREATE TABLE IF NOT EXISTS identity_verifications (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  step TEXT NOT NULL,                      -- document_check | interview | community_witness | clue_match
  outcome TEXT NOT NULL,                   -- passed | failed | inconclusive
  detail TEXT,
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS needs (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  category TEXT NOT NULL,                  -- health | mobility | infant_care | child_welfare | elder_care | other
  sensitivity TEXT NOT NULL,               -- general | child | elderly | medical
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',     -- open | met | cancelled
  recorded_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tracing_clues (
  id TEXT PRIMARY KEY,
  clue_type TEXT NOT NULL,                 -- seeking_person | found_person | possible_duplicate | location_report
  subject_person_id TEXT,
  target_name TEXT,
  target_age_band TEXT,
  detail TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',     -- open | confirmed | rejected | used
  submitted_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  confirmed_by TEXT,
  confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS match_candidates (
  id TEXT PRIMARY KEY,
  clue_id TEXT NOT NULL,
  person_id TEXT NOT NULL,
  score REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | accepted | rejected
  UNIQUE(clue_id, person_id)
);

CREATE TABLE IF NOT EXISTS transfers (
  id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  from_shelter_id TEXT NOT NULL,
  to_shelter_id TEXT NOT NULL,
  kind TEXT NOT NULL DEFAULT 'transfer',   -- transfer | reallocation
  reason TEXT NOT NULL,
  reason_category TEXT NOT NULL DEFAULT 'other',
  status TEXT NOT NULL DEFAULT 'planned',  -- planned | confirmed | cancelled
  planned_by TEXT NOT NULL,
  planned_at TEXT NOT NULL,
  confirmed_by TEXT,
  confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS supply_batches (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                      -- medicine | infant | food | hygiene | other
  item TEXT NOT NULL,
  quantity INTEGER NOT NULL CHECK(quantity >= 0),
  unit TEXT NOT NULL,
  shelter_id TEXT NOT NULL,                -- 入库安置点
  sensitivity TEXT NOT NULL DEFAULT 'general',
  resupply_level INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS supply_movements (
  id TEXT PRIMARY KEY,
  batch_id TEXT NOT NULL REFERENCES supply_batches(id),
  movement_type TEXT NOT NULL,             -- distribute | return | transfer | handover | reissue | adjust
  quantity INTEGER NOT NULL CHECK(quantity > 0),
  person_id TEXT,
  from_shelter_id TEXT,
  to_shelter_id TEXT,
  to_person_id TEXT,
  reason TEXT,
  linked_movement_id TEXT,
  status TEXT NOT NULL DEFAULT 'completed', -- planned | completed | cancelled
  actor TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_movements_batch ON supply_movements(batch_id);
CREATE INDEX IF NOT EXISTS idx_movements_person ON supply_movements(person_id);

CREATE TABLE IF NOT EXISTS receipts (
  source_team TEXT NOT NULL,
  external_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  payload TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  result TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (source_team, external_id)
);

CREATE TABLE IF NOT EXISTS conflicts (
  id TEXT PRIMARY KEY,
  source_team TEXT NOT NULL,
  external_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  existing_payload TEXT NOT NULL,
  incoming_payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',     -- open | resolved
  resolution TEXT,
  resolved_by TEXT,
  resolved_at TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pending_tasks (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,                      -- capacity_alert | missing_person_match | medicine_resupply | transfer_confirmation
  subject_type TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  payload TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending | done | cancelled
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(kind, subject_type, subject_id)
);
"""


class Store:
    """SQLite 存储；单连接，事务由服务层显式控制。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        # check_same_thread=False：HTTP 层用锁串行化访问，连接可跨线程使用
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """显式事务：状态变更与事件追加要么同时成功，要么同时回滚。"""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    def append_event(
        self,
        event_type: str,
        *,
        actor: str,
        role: str,
        subject_type: str,
        subject_id: str,
        payload: dict[str, Any],
        created_at: str,
    ) -> int:
        """追加事件；只在事务内调用，事件永不修改或删除。"""
        cursor = self.conn.execute(
            "INSERT INTO events(type, actor, role, subject_type, subject_id, payload, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                actor,
                role,
                subject_type,
                subject_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                created_at,
            ),
        )
        return int(cursor.lastrowid)

    def events_for(self, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM events WHERE subject_type = ? AND subject_id = ? ORDER BY id",
            (subject_type, subject_id),
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    def events_of_types(self, types: list[str]) -> list[dict[str, Any]]:
        marks = ",".join("?" for _ in types)
        rows = self.conn.execute(
            f"SELECT * FROM events WHERE type IN ({marks}) ORDER BY id", types
        ).fetchall()
        return [self._event_dict(row) for row in rows]

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        value["payload"] = json.loads(value["payload"])
        return value

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        return [dict(row) for row in self.conn.execute(sql, params).fetchall()]
