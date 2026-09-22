"""SQLite 连接与 schema 初始化。

容器启动时 ``init_db()`` 会在 ``DB_PATH``（默认 /data/regreview.db）
创建全部表结构；若文件已存在则为幂等空操作。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

DB_PATH = Path("/data/regreview.db")
if not DB_PATH.parent.exists():  # 本地裸跑（无 /data）时回退到仓库内 data/
    DB_PATH = Path(__file__).resolve().parent.parent / "data" / "regreview.db"
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    title        TEXT    NOT NULL,
    old_text     TEXT    NOT NULL DEFAULT '',
    new_text     TEXT    NOT NULL DEFAULT '',
    created_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    aligned_at   TEXT
);

CREATE TABLE IF NOT EXISTS paragraphs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    side        TEXT    NOT NULL CHECK (side IN ('old', 'new')),
    idx         INTEGER NOT NULL,
    content     TEXT    NOT NULL,
    UNIQUE (document_id, side, idx)
);

CREATE TABLE IF NOT EXISTS alignments (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id    INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    old_indices    TEXT    NOT NULL DEFAULT '',   -- 逗号分隔的旧版段落序号（0 基），空表示无
    new_indices    TEXT    NOT NULL DEFAULT '',
    change_type    TEXT    NOT NULL CHECK (change_type IN
                       ('unchanged', 'rewrite', 'added', 'deleted',
                        'split', 'merged', 'multi_rewrite')),
    confidence     REAL    NOT NULL DEFAULT 0.0,  -- 0~1
    explanation    TEXT    NOT NULL DEFAULT '',
    similarity     REAL,
    old_group_text TEXT    NOT NULL DEFAULT '',
    new_group_text TEXT    NOT NULL DEFAULT '',
    diff_ops       TEXT    NOT NULL DEFAULT '[]', -- JSON: 字符级差异
    is_manual      INTEGER NOT NULL DEFAULT 0,
    decision       TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (decision IN ('pending', 'confirmed', 'disputed', 'ignored')),
    decision_note  TEXT    NOT NULL DEFAULT '',
    decided_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_paragraphs_doc ON paragraphs(document_id, side, idx);
CREATE INDEX IF NOT EXISTS idx_alignments_doc ON alignments(document_id);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """创建数据目录与全部表（幂等），容器入口调用。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)


def indices_to_str(indices: list[int]) -> str:
    return ",".join(str(i) for i in indices)


def str_to_indices(value: str) -> list[int]:
    return [int(x) for x in value.split(",") if x != ""]


def row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["old_indices"] = str_to_indices(d.pop("old_indices") or "")
    d["new_indices"] = str_to_indices(d.pop("new_indices") or "")
    d["is_manual"] = bool(d["is_manual"])
    d["similarity"] = d["similarity"]
    return d
