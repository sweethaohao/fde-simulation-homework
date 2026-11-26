"""建表 + 灌种子数据（幂等，可重复跑）。

顺序：init_schema → 清空种子表 → 导入 CSV → 写入七个演示账号与指标口径。
本脚本不灌图谱（图谱由 scripts/seed_graph.py 负责）。
"""

from __future__ import annotations  # 延迟注解

import argparse  # 命令行
import csv  # 读 CSV
import json  # 结构化字段
import sqlite3  # 类型提示
import sys  # 调整导入路径
from pathlib import Path  # 路径

ROOT = Path(__file__).resolve().parents[1]  # 仓库根
if str(ROOT) not in sys.path:  # 允许脚本直接运行
    sys.path.insert(0, str(ROOT))  # 加入模块搜索路径

from src.app.db import connect, init_schema, now_iso  # noqa: E402  # 建表与连接
from src.app.security import hash_password  # noqa: E402  # 口令哈希

DEMO_PASSWORD = "Demo@2026"  # 七个演示账号统一口令（README 标注为演示账号）

USERS = [  # 第 3 节种子账号
    ("U-ADMIN", "admin", "admin@shiguang.local", "系统管理员", "admin", "global", None),
    ("U-OPS", "gu.minglan", "ops@shiguang.local", "顾明岚", "ops_director", "global", None),
    ("U-LOSS", "fu.jing", "loss@shiguang.local", "傅景", "loss_manager", "global", None),
    ("U-PLAN", "tang.yingqiu", "planner@shiguang.local", "唐映秋", "planner", "region", "华东"),
    ("U-STORE", "lin.yizhou", "store@shiguang.local", "林亦舟", "store_manager", "store", "S01"),
    ("U-LOGI", "ye.shu", "logistics@shiguang.local", "叶书", "logistics", "region", "华东"),
    ("U-DATA", "qin.rui", "data@shiguang.local", "秦芮", "data_engineer", "global", None),
]

METRICS = [  # 指标口径：signed_by 为空即未签发（R10）
    (
        "stockout_rate", "缺货率",
        "货架可售量为 0 的 SKU 数（当日截面）或 SKU·天（时段加权）",
        "在售 SKU 数（当日截面）或 SKU·天（时段加权）",
        "6 周试点窗口，前 3 周回放、后 3 周并行",
        "促销日单列统计，默认不进基线；缺货需区分「有货不可售」与「真正无货」",
        "傅景", now_iso(), "傅景", json.dumps(["f53", "f00", "f42"], ensure_ascii=False),
        "两种分母并列展示：当日截面约 12%，时段加权约 21%，差 9pp 本身就是结论",
    ),
    (
        "account_diff_rate", "账实差异率",
        "账面库存与「货架可售 + 后仓 + 在途」不一致的快照行数",
        "参与盘点的快照总行数",
        "每日两次刷新，按周汇总",
        "新旧商品码/套装码造成的伪差异单独列出，不并入真差异",
        "傅景", now_iso(), "傅景", json.dumps(["f12", "f30", "f33"], ensure_ascii=False),
        "约 8%；其中约三分之一是编码伪差异",
    ),
    (
        "turnover_days", "库存周转天数",
        "平均库存（件）", "平均日销量（件）",
        "需 ≥ 90 天窗口才能稳定",
        "含/不含在途两种口径尚未签发",
        None, None, "顾明岚", json.dumps(["f00", "f53", "f42"], ensure_ascii=False),
        "两种口径结果相差 6 天以上；分母未签发前不出数（R10）",
    ),
    (
        "waste_rate", "报废率",
        "报废件数", "入库件数",
        "按月统计",
        "临期折价不计入报废，需单列",
        None, None, "傅景", json.dumps(["f42", "f53"], ensure_ascii=False),
        "6 周试点窗口过短，报废记录缺失，暂不出数",
    ),
    (
        "shelf_restock_hours", "上架时效（小时）",
        "签收至货架可售的时间差", "已完成上架的补货单数",
        "按补货批次",
        "上架无系统记录时不可计算",
        "叶书", now_iso(), "叶书", json.dumps(["f10", "f13", "f52"], ensure_ascii=False),
        "shelved 事件全部无时间戳，数据就绪度不足 → DATA_NOT_READY",
    ),
]

RESET_TABLES = [  # 幂等清理顺序：先子后父
    "graph_fts", "graph_embeddings", "graph_edges", "graph_nodes",
    "events", "replenishment_orders", "promotions", "daily_snapshots",
    "skus", "stores", "users", "audit_logs", "agent_runs",
    "metric_definitions", "data_imports",
]


def read_csv(path: Path) -> list[dict]:
    """读取 CSV 为字典列表。

    入参：path CSV 文件。
    出参：字典列表（空文件返回空列表）。
    失败时的行为：文件不存在抛 FileNotFoundError（提示先跑 gen_data.py）。
    """

    if not path.exists():  # 必须先有合成数据
        raise FileNotFoundError(f"缺少 {path}，请先运行 scripts/gen_data.py")
    with path.open("r", encoding="utf-8", newline="") as fh:  # UTF-8 读取
        return list(csv.DictReader(fh))  # 用表头映射为字典


def _to_int(value: str | None) -> int | None:
    """字符串转整数（空值保持 None）。

    入参：value 字符串或空。
    出参：int 或 None。
    失败时的行为：非法数字抛 ValueError。
    """

    if value is None or value == "":  # 空值不伪装成 0
        return None
    return int(value)  # 正常转换


def insert_users(conn: sqlite3.Connection) -> None:
    """写入七个演示账号。

    入参：conn 连接。
    出参：无。
    失败时的行为：SQL 异常向上抛出。
    """

    created = now_iso()  # 统一创建时间
    rows = []  # 待插入行
    for uid, username, email, name, role, scope_type, scope_value in USERS:  # 逐账号哈希口令
        rows.append((uid, username, email, name, role, scope_type, scope_value,
                     hash_password(DEMO_PASSWORD), 1, created, 1))
    conn.executemany(
        """
        INSERT INTO users(user_id, username, email, display_name, role, scope_type,
                          scope_value, password_hash, active, created_at, is_synthetic)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        """,
        rows,
    )


def insert_metrics(conn: sqlite3.Connection) -> None:
    """写入指标口径。

    入参：conn 连接。
    出参：无。
    失败时的行为：SQL 异常向上抛出。
    """

    conn.executemany(
        """
        INSERT INTO metric_definitions(metric_code, name, numerator_def, denominator_def,
          period, exclusion_rule, signed_by, signed_at, required_signer, evidence_f, notes, is_synthetic)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,1)
        """,
        METRICS,
    )


def enrich_order(row: dict, snapshots: dict, skus: dict, stores: dict) -> dict:
    """为种子补货单补齐快照与版本字段（支撑 R5 重建）。

    入参：row 订单 CSV 行；snapshots 快照索引；skus SKU 索引；stores 门店索引。
    出参：补齐后的订单字典。
    失败时的行为：缺少快照时写入说明性占位，不抛异常。
    """

    key = (row["biz_date"], row["store_id"], row["sku_id"])  # 快照唯一键
    snap = snapshots.get(key, {})  # 可能缺失（S07 最后三天）
    row["suggestion_json"] = None  # 先置空，后面按状态补
    row["guardrail_json"] = None  # 先置空，兜底单才写
    row["snapshot_json"] = json.dumps({
        "biz_date": row["biz_date"], "store_id": row["store_id"], "sku_id": row["sku_id"],
        "book_stock": snap.get("book_stock"), "shelf_available": snap.get("shelf_available"),
        "backroom_stock": snap.get("backroom_stock"), "in_transit": snap.get("in_transit"),
        "sales_qty": snap.get("sales_qty"), "is_promo": snap.get("is_promo", 0),
        "note": "合成数据快照；缺失表示该日无记录，不猜测补齐（f33）",
    }, ensure_ascii=False)  # 保存当次快照，供重建视图还原
    row["versions_json"] = json.dumps({
        "promo_version": "P2026-10-temp-v1" if snap.get("is_promo") else "P2026-09-v1",
        "case_spec_version": "CASE-2026-09-v1",
        "delivery_calendar_version": stores.get(row["store_id"], {}).get("delivery_calendar_version"),
        "rule_version": "RULE-2026-09-v1",
    }, ensure_ascii=False)  # 六类要素中的版本信息
    if row.get("status") == "STOPPED":  # 兜底单据记录命中详情
        row["guardrail_json"] = json.dumps({
            "stopped": True, "reason": row.get("reason"),
            "handoff": {"role": "loss_manager", "name": "傅景"},
            "manual_confirm_at": row.get("manual_confirm_at"), "f_code": "f44",
        }, ensure_ascii=False)
    else:  # 正常建议记录依据
        row["suggestion_json"] = json.dumps({
            "suggestion_qty": _to_int(row.get("suggestion_qty")),
            "rationale": "历史回放基线：近 7 日销量 + 覆盖天数 - 可用库存，按整箱取整并做仓容校验",
            "degraded": False, "evidence_refs": ["f20", "f23", "f53"],
        }, ensure_ascii=False)
    return row  # 返回补齐结果


def build_database(db_path: str | Path, seed_dir: str | Path, reset: bool = True) -> dict:
    """构建数据库并灌入种子。

    入参：db_path 数据库路径；seed_dir CSV 目录；reset 是否清空已有数据。
    出参：各表行数统计。
    失败时的行为：缺 CSV 抛 FileNotFoundError；SQL 异常向上抛出。
    """

    seed = Path(seed_dir)  # CSV 目录
    conn = connect(db_path)  # 打开连接
    try:
        init_schema(conn)  # 建表（幂等）
        if reset:  # 默认清空，保证重复执行结果一致
            for table in RESET_TABLES:  # 逐表清理
                conn.execute(f"DELETE FROM {table}")  # 表名来自固定白名单，非用户输入
            conn.commit()  # 提交清理

        stores = read_csv(seed / "stores.csv")  # 门店
        skus = read_csv(seed / "skus.csv")  # SKU
        promos = read_csv(seed / "promotions.csv")  # 促销
        snapshots = read_csv(seed / "snapshots.csv")  # 日快照
        orders = read_csv(seed / "orders.csv")  # 补货单
        events = read_csv(seed / "events.csv")  # 事件流

        conn.executemany(
            "INSERT INTO stores(store_id,name,region,tier,delivery_calendar_version,is_synthetic) VALUES (?,?,?,?,?,?)",
            [[r["store_id"], r["name"], r["region"], r["tier"], r["delivery_calendar_version"] or None, 1] for r in stores],
        )
        conn.executemany(
            "INSERT INTO skus(sku_id,name,category,abc_class,case_size,moq_cases,unit_cost_cents,warehouse_cap,is_synthetic) VALUES (?,?,?,?,?,?,?,?,?)",
            [[r["sku_id"], r["name"], r["category"], r["abc_class"], _to_int(r["case_size"]),
              _to_int(r["moq_cases"]) or 1, _to_int(r["unit_cost_cents"]) or 0, _to_int(r["warehouse_cap"]) or 240, 1] for r in skus],
        )
        conn.executemany(
            "INSERT INTO promotions(promo_id,store_id,sku_id,version,start_date,end_date,status,is_synthetic) VALUES (?,?,?,?,?,?,?,?)",
            [[r["promo_id"], r["store_id"] or None, r["sku_id"], r["version"], r["start_date"], r["end_date"], r["status"], 1] for r in promos],
        )
        conn.executemany(
            """
            INSERT INTO daily_snapshots(biz_date,store_id,sku_id,book_stock,shelf_available,
              backroom_stock,in_transit,sales_qty,is_promo,account_diff,diff_cause,unit_cost_cents,captured_at,is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,1)
            """,
            [[r["biz_date"], r["store_id"], r["sku_id"], _to_int(r["book_stock"]) or 0,
              _to_int(r["shelf_available"]) or 0, _to_int(r["backroom_stock"]) or 0,
              _to_int(r["in_transit"]) or 0, _to_int(r["sales_qty"]) or 0,
              _to_int(r["is_promo"]) or 0, _to_int(r["account_diff"]) or 0,
              r["diff_cause"] or None, _to_int(r["unit_cost_cents"]) or 0,
              r["captured_at"] or None] for r in snapshots],
        )
        snap_index = {(r["biz_date"], r["store_id"], r["sku_id"]): {
            "book_stock": _to_int(r["book_stock"]), "shelf_available": _to_int(r["shelf_available"]),
            "backroom_stock": _to_int(r["backroom_stock"]), "in_transit": _to_int(r["in_transit"]),
            "sales_qty": _to_int(r["sales_qty"]), "is_promo": _to_int(r["is_promo"]),
        } for r in snapshots}  # 订单补全索引
        sku_index = {r["sku_id"]: r for r in skus}  # SKU 索引
        store_index = {r["store_id"]: r for r in stores}  # 门店索引

        order_rows = []  # 补全后的订单
        for r in orders:  # 逐单补齐
            r = dict(r)  # 复制，避免污染原始列表
            r = enrich_order(r, snap_index, sku_index, store_index)  # 补充快照与版本
            order_rows.append([
                r["order_id"], r["biz_no"], r["store_id"], r["sku_id"], r["biz_date"], r["status"],
                _to_int(r["suggestion_qty"]), _to_int(r["before_round_qty"]), _to_int(r["rounded_qty"]),
                _to_int(r["capacity_capped_qty"]), _to_int(r["final_qty"]), r["suggestion_json"],
                r["snapshot_json"], r["versions_json"], r["guardrail_json"], _to_int(r["degraded"]) or 0,
                r["reason"], r["owner_role"], r["owner_name"], r["manual_confirm_at"], r["created_by"],
                r["created_at"], r["submitted_at"], r["signed_by"], r["signed_at"], r["reject_reason"],
                _to_int(r["is_demo"]) or 0, 1,
            ])
        conn.executemany(
            """
            INSERT INTO replenishment_orders(order_id,biz_no,store_id,sku_id,biz_date,status,
              suggestion_qty,before_round_qty,rounded_qty,capacity_capped_qty,final_qty,
              suggestion_json,snapshot_json,versions_json,guardrail_json,degraded,reason,
              owner_role,owner_name,manual_confirm_at,created_by,created_at,submitted_at,
              signed_by,signed_at,reject_reason,is_demo,is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            order_rows,
        )
        conn.executemany(
            """
            INSERT INTO events(event_id,order_id,store_id,sku_id,event_type,channel,occurred_at,
              ts_reliable,payload,is_synthetic)
            VALUES (?,?,?,?,?,?,?,?,?,1)
            """,
            [[r["event_id"], r["order_id"] or None, r["store_id"], r["sku_id"] or None,
              r["event_type"], r["channel"] or None, r["occurred_at"] or None,
              _to_int(r["ts_reliable"]) or 0, r["payload"] or "{}"] for r in events],
        )
        insert_users(conn)  # 七个演示账号
        insert_metrics(conn)  # 指标口径
        imported_at = now_iso()  # 导入时间
        conn.executemany(
            "INSERT INTO data_imports(source_file,rows,status,imported_at,is_synthetic) VALUES (?,?,?,?,1)",
            [(name, count, "ok", imported_at) for name, count in (
                ("stores.csv", len(stores)), ("skus.csv", len(skus)), ("promotions.csv", len(promos)),
                ("snapshots.csv", len(snapshots)), ("orders.csv", len(orders)), ("events.csv", len(events)),
            )],
        )
        conn.commit()  # 一次性提交
        stats = {name: conn.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"] for name in (
            "users", "stores", "skus", "daily_snapshots", "promotions", "replenishment_orders", "events", "metric_definitions",
        )}  # 行数统计
        return stats  # 返回统计
    except Exception:
        conn.rollback()  # 失败回滚
        raise  # 向上抛出
    finally:
        conn.close()  # 关闭连接


def main() -> None:
    """命令行入口。

    入参：--db 数据库路径；--seed CSV 目录；--keep 不清空已有数据。
    出参：无（打印统计）。
    失败时的行为：异常向上抛出，进程非零退出。
    """

    parser = argparse.ArgumentParser(description="建表并灌入种子数据")  # 参数解析
    parser.add_argument("--db", default=str(ROOT / "data" / "mvp.db"))  # 默认运行库
    parser.add_argument("--seed", default=str(ROOT / "data" / "seed"))  # 默认 CSV 目录
    parser.add_argument("--keep", action="store_true")  # 保留已有数据（不清理）
    args = parser.parse_args()  # 解析
    stats = build_database(args.db, args.seed, reset=not args.keep)  # 构建
    print(json.dumps(stats, ensure_ascii=False, indent=2))  # 打印统计


if __name__ == "__main__":
    main()
