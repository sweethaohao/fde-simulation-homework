"""数据库连接、事务与建表。

使用标准库 sqlite3 文件库；启用 WAL、外键、行工厂。
所有表带 `is_synthetic` 默认 1（R3：外部数据只导 CSV，不回写）。
"""

from __future__ import annotations  # 延迟注解

import sqlite3  # 标准库驱动
from contextlib import contextmanager  # 连接上下文
from datetime import datetime, timedelta, timezone  # 时间与固定时区
from pathlib import Path  # 路径类型
from typing import Iterator  # 迭代器类型

from .config import settings  # 配置单例

TZ = timezone(timedelta(hours=8))  # 业务时区固定东八区（Windows 无 tzdata，避免额外依赖）


def now_iso() -> str:
    """当前时间（ISO 8601，带时区）。

    入参：无。
    出参：形如 `2026-11-19T14:05:00+08:00` 的字符串。
    失败时的行为：不抛异常。
    """

    return datetime.now(TZ).replace(microsecond=0).isoformat()  # 去掉微秒，便于比较


def connect(db_path: str | Path | None = None) -> sqlite3.Connection:
    """打开数据库连接。

    入参：db_path 可选路径；默认取 settings.db_path（测试通过 APP_DB_PATH 覆盖）。
    出参：sqlite3.Connection，行工厂为 sqlite3.Row。
    失败时的行为：目录不可写或文件损坏时抛 sqlite3.Error（由上层兜底）。
    """

    path = Path(db_path) if db_path else settings.db_path  # 允许测试指定临时库
    path.parent.mkdir(parents=True, exist_ok=True)  # 首次运行自动建目录
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)  # 每请求独立连接，允许线程内复用
    conn.row_factory = sqlite3.Row  # 行可按列名访问
    conn.execute("PRAGMA journal_mode=WAL")  # 读写并发更稳
    conn.execute("PRAGMA foreign_keys=ON")  # 开启外键约束
    conn.execute("PRAGMA synchronous=NORMAL")  # 演示环境兼顾性能与安全
    return conn  # 返回连接


@contextmanager
def get_conn(db_path: str | Path | None = None) -> Iterator[sqlite3.Connection]:
    """连接上下文：自动提交与关闭。

    入参：db_path 可选路径。
    出参：生成器产出 Connection。
    失败时的行为：异常时回滚并向上抛出（由全局处理器转 INTERNAL）。
    """

    conn = connect(db_path)  # 建立连接
    try:
        yield conn  # 交给调用方执行 SQL
        conn.commit()  # 正常结束提交
    except Exception:
        conn.rollback()  # 异常回滚，避免半写入
        raise  # 保留原始异常
    finally:
        conn.close()  # 必须关闭，避免文件句柄泄漏


SCHEMA_SQL = """
-- 用户：七个角色全部落库，scope 决定数据域
CREATE TABLE IF NOT EXISTS users (
  user_id        TEXT PRIMARY KEY,
  username       TEXT NOT NULL UNIQUE,
  email          TEXT NOT NULL,
  display_name   TEXT NOT NULL,
  role           TEXT NOT NULL,
  scope_type     TEXT NOT NULL DEFAULT 'store',
  scope_value    TEXT,
  password_hash  TEXT NOT NULL,
  active         INTEGER NOT NULL DEFAULT 1,
  created_at     TEXT NOT NULL,
  is_synthetic   INTEGER NOT NULL DEFAULT 1
);

-- 门店：8 家，均在华东
CREATE TABLE IF NOT EXISTS stores (
  store_id                  TEXT PRIMARY KEY,
  name                      TEXT NOT NULL,
  region                    TEXT NOT NULL,
  tier                      TEXT NOT NULL,
  delivery_calendar_version TEXT,
  is_synthetic              INTEGER NOT NULL DEFAULT 1
);

-- SKU：50 个常温品；case_size 允许为空以制造「依据缺失」兜底
CREATE TABLE IF NOT EXISTS skus (
  sku_id          TEXT PRIMARY KEY,
  name            TEXT NOT NULL,
  category        TEXT NOT NULL,
  abc_class       TEXT NOT NULL,
  case_size       INTEGER,
  moq_cases       INTEGER NOT NULL DEFAULT 1,
  unit_cost_cents INTEGER NOT NULL,
  warehouse_cap   INTEGER NOT NULL DEFAULT 240,
  is_synthetic    INTEGER NOT NULL DEFAULT 1
);

-- 日快照：账面 / 货架可售 / 后仓 / 在途 / 销量
CREATE TABLE IF NOT EXISTS daily_snapshots (
  snapshot_id      INTEGER PRIMARY KEY AUTOINCREMENT,
  biz_date         TEXT NOT NULL,
  store_id         TEXT NOT NULL,
  sku_id           TEXT NOT NULL,
  book_stock       INTEGER NOT NULL,
  shelf_available  INTEGER NOT NULL,
  backroom_stock   INTEGER NOT NULL,
  in_transit       INTEGER NOT NULL,
  sales_qty        INTEGER NOT NULL,
  is_promo         INTEGER NOT NULL DEFAULT 0,
  account_diff     INTEGER NOT NULL DEFAULT 0,
  diff_cause       TEXT,
  unit_cost_cents  INTEGER NOT NULL,
  captured_at      TEXT,
  is_synthetic     INTEGER NOT NULL DEFAULT 1,
  UNIQUE (biz_date, store_id, sku_id)
);

-- 促销版本：临时促销是 f44 第一类兜底条件
CREATE TABLE IF NOT EXISTS promotions (
  promo_id     TEXT PRIMARY KEY,
  store_id     TEXT,
  sku_id       TEXT NOT NULL,
  version      TEXT NOT NULL,
  start_date   TEXT NOT NULL,
  end_date     TEXT NOT NULL,
  status       TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 1
);

-- 补货单：只到「待签核」，不存在下单路径（R1）
CREATE TABLE IF NOT EXISTS replenishment_orders (
  order_id              TEXT PRIMARY KEY,
  biz_no                TEXT NOT NULL UNIQUE,
  store_id              TEXT NOT NULL,
  sku_id                TEXT NOT NULL,
  biz_date              TEXT NOT NULL,
  status                TEXT NOT NULL,
  suggestion_qty        INTEGER,
  before_round_qty      INTEGER,
  rounded_qty           INTEGER,
  capacity_capped_qty   INTEGER,
  final_qty             INTEGER,
  suggestion_json       TEXT,
  snapshot_json         TEXT,
  versions_json         TEXT,
  guardrail_json        TEXT,
  degraded              INTEGER NOT NULL DEFAULT 0,
  reason                TEXT,
  owner_role            TEXT,
  owner_name            TEXT,
  manual_confirm_at     TEXT,
  created_by            TEXT,
  created_at            TEXT NOT NULL,
  submitted_at          TEXT,
  signed_by             TEXT,
  signed_at             TEXT,
  reject_reason         TEXT,
  is_demo               INTEGER NOT NULL DEFAULT 0,
  is_synthetic          INTEGER NOT NULL DEFAULT 1
);

-- 事件流：时间戳允许为空（留空正是 data_readiness 的材料）
CREATE TABLE IF NOT EXISTS events (
  event_id     TEXT PRIMARY KEY,
  order_id     TEXT,
  store_id     TEXT NOT NULL,
  sku_id       TEXT,
  event_type   TEXT NOT NULL,
  channel      TEXT,
  occurred_at  TEXT,
  ts_reliable  INTEGER NOT NULL DEFAULT 1,
  payload      TEXT NOT NULL DEFAULT '{}',
  is_synthetic INTEGER NOT NULL DEFAULT 1
);

-- 审计日志：写入前过字段白名单（R4）
CREATE TABLE IF NOT EXISTS audit_logs (
  audit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id TEXT,
  actor_id   TEXT,
  actor_name TEXT,
  role       TEXT,
  action     TEXT NOT NULL,
  resource   TEXT,
  resource_id TEXT,
  decision   TEXT NOT NULL,
  error_code TEXT,
  scope      TEXT,
  detail     TEXT NOT NULL DEFAULT '{}',
  ip         TEXT,
  archived   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 1
);

-- Agent/工具调用留痕（Harness 落点之二）
CREATE TABLE IF NOT EXISTS agent_runs (
  run_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  request_id     TEXT,
  kind           TEXT NOT NULL,
  agent          TEXT,
  tool           TEXT,
  input_summary  TEXT,
  output_summary TEXT,
  duration_ms    INTEGER,
  tokens         INTEGER,
  status         TEXT NOT NULL,
  detail         TEXT NOT NULL DEFAULT '{}',
  created_at     TEXT NOT NULL,
  is_synthetic   INTEGER NOT NULL DEFAULT 1
);

-- 指标口径：signed_by 为空即未签发（R10）
CREATE TABLE IF NOT EXISTS metric_definitions (
  metric_code    TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  numerator_def  TEXT NOT NULL,
  denominator_def TEXT NOT NULL,
  period         TEXT NOT NULL,
  exclusion_rule TEXT NOT NULL,
  signed_by      TEXT,
  signed_at      TEXT,
  required_signer TEXT NOT NULL,
  evidence_f     TEXT NOT NULL DEFAULT '[]',
  notes          TEXT,
  is_synthetic   INTEGER NOT NULL DEFAULT 1
);

-- 知识图谱：节点与边（SQLite + 递归 CTE，不装 Neo4j）
CREATE TABLE IF NOT EXISTS graph_nodes (
  id         TEXT PRIMARY KEY,
  kind       TEXT NOT NULL,
  name       TEXT NOT NULL,
  props      TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS graph_edges (
  src   TEXT NOT NULL,
  dst   TEXT NOT NULL,
  rel   TEXT NOT NULL,
  props TEXT NOT NULL DEFAULT '{}',
  PRIMARY KEY (src, dst, rel)
);

-- 向量检索：nomic-embed-text 768 维，JSON 存储；不可用时降级 FTS5
CREATE TABLE IF NOT EXISTS graph_embeddings (
  node_id    TEXT PRIMARY KEY,
  vector     TEXT NOT NULL,
  model      TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- FTS5：中文按二元组切分后入索引，支持关键词降级检索
CREATE VIRTUAL TABLE IF NOT EXISTS graph_fts USING fts5(node_id UNINDEXED, text);

-- 导入批次：证明数据只经 CSV 进入本地库（R3）
CREATE TABLE IF NOT EXISTS data_imports (
  import_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  source_file  TEXT NOT NULL,
  rows         INTEGER NOT NULL,
  status       TEXT NOT NULL,
  imported_at  TEXT NOT NULL,
  is_synthetic INTEGER NOT NULL DEFAULT 1
);
"""


def init_schema(conn: sqlite3.Connection) -> None:
    """建表（幂等）。

    入参：conn 数据库连接。
    出参：无。
    失败时的行为：SQL 语法/权限问题抛 sqlite3.Error。
    """

    conn.executescript(SCHEMA_SQL)  # 一条脚本建全部表与虚拟表


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    """行对象转字典列表。

    入参：rows 查询结果。
    出参：字典列表。
    失败时的行为：不抛异常。
    """

    return [dict(r) for r in rows]  # sqlite3.Row 支持 dict() 转换


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    """单行转字典。

    入参：row 可能为空。
    出参：字典或 None。
    失败时的行为：不抛异常。
    """

    return dict(row) if row is not None else None  # 空行保持 None
