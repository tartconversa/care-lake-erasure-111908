"""运行状态库：清除进度、墓碑、保留例外、交付与运行记录。

状态库是受限的运行日志，不属于湖内文件；通用查询接口不读取它。
湖内文件（凭据、控制数）不含可回连已清除身份的明文关联。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copies(
  copy_id TEXT PRIMARY KEY,
  path TEXT NOT NULL,
  kind TEXT NOT NULL,               -- detail/derived/export/aggregate/restricted
  availability TEXT NOT NULL,       -- available/unavailable（演练状态）
  identifying_fields TEXT NOT NULL, -- JSON 数组，声明的身份字段
  derivation TEXT,                  -- 派生方式标记（如 person_day）
  registered_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS lineage(
  child TEXT NOT NULL,
  parent TEXT NOT NULL,
  PRIMARY KEY(child, parent)
);
CREATE TABLE IF NOT EXISTS authorities(
  authority_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  partner TEXT NOT NULL,
  fields TEXT NOT NULL,             -- JSON 数组
  from_date TEXT NOT NULL,
  until_date TEXT,                  -- 空值为尚无结束记录
  kind TEXT NOT NULL,               -- service/settlement
  granted_at TEXT NOT NULL          -- 依据建立时间（重授权判定用）
);
CREATE TABLE IF NOT EXISTS requests(
  request_id TEXT PRIMARY KEY,
  person_id TEXT NOT NULL,
  partner TEXT,                     -- 空值表示全部合作方
  fields TEXT NOT NULL,             -- JSON 数组，* 为全部可识别内容
  received_at TEXT NOT NULL,
  state TEXT NOT NULL               -- received/in_progress/cleared/settled
);
CREATE TABLE IF NOT EXISTS tasks(
  request_id TEXT NOT NULL,
  copy_id TEXT NOT NULL,
  status TEXT NOT NULL,             -- pending/done/blocked/no_match
  note TEXT,
  updated_at TEXT,
  PRIMARY KEY(request_id, copy_id)
);
CREATE TABLE IF NOT EXISTS tombstones(
  value_hash TEXT PRIMARY KEY,      -- 键控散列，不存原文
  kind TEXT NOT NULL,               -- person/service/field
  person_token TEXT,
  partner TEXT,
  field TEXT,
  request_id TEXT,
  purged_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exceptions(
  exception_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  basis_label TEXT NOT NULL,        -- 具体依据编号（说明用）
  fields TEXT NOT NULL,             -- JSON 数组，覆盖字段
  expires_at TEXT NOT NULL,         -- 到期日，到期后继续执行清除
  status TEXT NOT NULL,             -- active/fulfilled
  person_token TEXT NOT NULL,
  fulfilled_at TEXT
);
CREATE TABLE IF NOT EXISTS deliveries(
  delivery_id TEXT PRIMARY KEY,
  partner TEXT NOT NULL,
  created_at TEXT NOT NULL,
  status TEXT NOT NULL,             -- delivered/stopped
  path TEXT,
  row_count INTEGER,
  content_hash TEXT,
  note TEXT
);
CREATE TABLE IF NOT EXISTS runs(
  run_id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,               -- purge
  request_id TEXT,
  started_at TEXT NOT NULL,
  finished_at TEXT                  -- 空值表示中断或进行中
);
CREATE TABLE IF NOT EXISTS ingest_log(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  delivery_id TEXT NOT NULL,
  at TEXT NOT NULL,
  decision TEXT NOT NULL,           -- landed/rejected/skipped
  reason TEXT NOT NULL,
  person_token TEXT,
  service_token TEXT,
  detail TEXT                       -- JSON：被剔除字段名等，不含个人值
);
"""


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.conn = sqlite3.connect(str(self.path), isolation_level=None)
        self.conn.row_factory = sqlite3.Row

    @classmethod
    def create(cls, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        store = cls(path)
        store.conn.executescript(SCHEMA)
        return store

    @classmethod
    def open(cls, path):
        path = Path(path)
        if not path.exists():
            raise SystemExit(f'运行目录未初始化（缺少 {path}），请先执行 init')
        return cls(path)

    @contextmanager
    def tx(self):
        """写事务；中断即回滚，配合幂等文件改写实现可恢复进度。"""
        self.conn.execute('BEGIN IMMEDIATE')
        try:
            yield self
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def q(self, sql, args=()):
        return [dict(row) for row in self.conn.execute(sql, args)]

    def one(self, sql, args=()):
        rows = self.q(sql, args)
        return rows[0] if rows else None

    def exec(self, sql, args=()):
        self.conn.execute(sql, args)

    def close(self):
        self.conn.close()

    def __del__(self):
        try:
            self.conn.close()
        except Exception:
            pass
