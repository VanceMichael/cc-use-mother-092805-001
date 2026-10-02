"""SQLite 连接与仓库初始化。"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .schema import SCHEMA_VERSION, full_schema


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    """打开数据库并启用外键约束。

    应用重启时对同一文件再次调用即可：结构已存在不会重建，待办任务原样保留。
    """
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    _bootstrap(conn)
    return conn


def _bootstrap(conn: sqlite3.Connection) -> None:
    already = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='operators'"
    ).fetchone()
    if already is None:
        conn.executescript(full_schema())
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
    )
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
    elif int(row["value"]) > SCHEMA_VERSION:
        raise RuntimeError(f"数据库结构版本 {row['value']} 高于当前代码 {SCHEMA_VERSION}")
    conn.commit()


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
