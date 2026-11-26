"""f44 兜底五类：确定性硬拦截，禁止模型判定。

命中任一类即停止自动建议，返回原因、接手人（商品与损耗经理）与人工确认时间。
"""

from __future__ import annotations  # 延迟注解

from datetime import date, datetime, timedelta  # 日期计算

from ..config import settings  # 人工确认时限
from ..db import TZ, row_to_dict  # 业务时区

# 规则清单：前端「兜底规则」页与 API 共用，保持单一事实来源
GUARDRAIL_RULES = [
    {
        "rule_code": "G1_TEMP_PROMO", "category": "temp_promo", "f_code": "f44",
        "title": "临时促销生效中",
        "condition": "查询日落在 status=temporary 的促销窗口内（含全国与门店级）",
        "owner_role": "loss_manager", "owner_name": "傅景",
        "action": "停止自动建议，由商品与损耗经理确认促销版本与改价回传",
    },
    {
        "rule_code": "G2_STOCKOUT_TRUNCATION", "category": "stockout_truncation", "f_code": "f44",
        "title": "缺货截断，销量不可信",
        "condition": "近 7 日销量合计为 0 且存在货架为 0 的日期",
        "owner_role": "loss_manager", "owner_name": "傅景",
        "action": "停止自动建议，先补录缺货时点再由计划员人工核对",
    },
    {
        "rule_code": "G3_BOOK_SHELF_MISMATCH", "category": "book_vs_shelf_mismatch", "f_code": "f44",
        "title": "可售与账面不一致",
        "condition": "快照 account_diff=1 且账面与「货架+后仓」偏差 ≥ 10%",
        "owner_role": "loss_manager", "owner_name": "傅景",
        "action": "停止自动建议，转门店盘点/编码核对",
    },
    {
        "rule_code": "G4_MISSING_EVIDENCE", "category": "missing_evidence", "f_code": "f44",
        "title": "依据缺失",
        "condition": "整箱规格为空，或门店配送日历版本为空",
        "owner_role": "loss_manager", "owner_name": "傅景",
        "action": "停止自动建议，进人工修复队列，不猜测补齐（f33）",
    },
    {
        "rule_code": "G5_SYSTEM_UNAVAILABLE", "category": "system_unavailable", "f_code": "f44",
        "title": "关键系统不可用",
        "condition": "查询日在门店快照中无记录（数据源中断）",
        "owner_role": "loss_manager", "owner_name": "傅景",
        "action": "停止自动建议，继续人工流程并标注待刷新（f35）",
    },
]


def rules_payload() -> dict:
    """返回兜底规则清单。

    入参：无。
    出参：{rules, manual_confirm_minutes, stop_actions_on_hit}。
    失败时的行为：不抛异常。
    """

    return {
        "rules": GUARDRAIL_RULES,
        "manual_confirm_minutes": settings.guardrail.manual_confirm_minutes,
        "stop_actions_on_hit": settings.guardrail.stop_actions_on_hit,
    }


def _hit(rule_code: str, reason: str, detail: dict) -> dict:
    """构造一条命中记录。

    入参：rule_code 规则编码；reason 原因；detail 细节。
    出参：命中学典。
    失败时的行为：不抛异常。
    """

    rule = next(r for r in GUARDRAIL_RULES if r["rule_code"] == rule_code)  # 取规则元数据
    return {
        "rule_code": rule["rule_code"], "category": rule["category"], "f_code": rule["f_code"],
        "title": rule["title"], "reason": reason, "detail": detail,
        "owner_role": rule["owner_role"], "owner_name": rule["owner_name"],
        "manual_confirm_minutes": settings.guardrail.manual_confirm_minutes,
    }


def evaluate(conn, store_id: str, sku_id: str, biz_date: str) -> dict:
    """执行五类兜底判定。

    入参：conn；store_id；sku_id；biz_date 查询业务日。
    出参：{hits, primary, snapshot, sku, store}。
    失败时的行为：SQL 异常向上抛出；数据缺失按命中处理，不猜测。
    """

    snapshot_row = conn.execute(
        "SELECT * FROM daily_snapshots WHERE store_id = ? AND sku_id = ? AND biz_date = ?",
        (store_id, sku_id, biz_date),
    ).fetchone()  # 当日快照
    sku = row_to_dict(conn.execute("SELECT * FROM skus WHERE sku_id = ?", (sku_id,)).fetchone())  # 商品
    store = row_to_dict(conn.execute("SELECT * FROM stores WHERE store_id = ?", (store_id,)).fetchone())  # 门店
    snapshot = row_to_dict(snapshot_row)  # 快照字典
    hits: list[dict] = []  # 命中列表

    if snapshot is None:  # 第一优先：数据源缺当日记录
        hits.append(_hit("G5_SYSTEM_UNAVAILABLE", f"{biz_date} 门店 {store_id} 无库存快照，数据源中断", {
            "biz_date": biz_date, "store_id": store_id,
            "note": "历史快照不得伪装当前（f34）；继续人工流程（f35）",
        }))
    else:  # 有快照才判断其余四类
        promo = conn.execute(
            """
            SELECT * FROM promotions
            WHERE sku_id = ? AND (store_id IS NULL OR store_id = ?)
              AND start_date <= ? AND end_date >= ? AND status = 'temporary'
            ORDER BY start_date DESC LIMIT 1
            """,
            (sku_id, store_id, biz_date, biz_date),
        ).fetchone()  # 临时促销
        if promo:  # G1
            hits.append(_hit("G1_TEMP_PROMO", f"临时促销生效中：{promo['version']}（{promo['start_date']}–{promo['end_date']}）", {
                "promo_version": promo["version"], "status": promo["status"],
                "note": "门店改价不一定回传（f21），预测会失效",
            }))
        start = (date.fromisoformat(biz_date) - timedelta(days=6)).isoformat()  # 近 7 日窗口
        recent = conn.execute(
            """
            SELECT COUNT(*) AS days, SUM(sales_qty) AS sales,
                   SUM(CASE WHEN shelf_available = 0 THEN 1 ELSE 0 END) AS out_days
            FROM daily_snapshots
            WHERE store_id = ? AND sku_id = ? AND biz_date BETWEEN ? AND ?
            """,
            (store_id, sku_id, start, biz_date),
        ).fetchone()  # 近期销量与缺货天数
        out_days = int(recent["out_days"] or 0) if recent else 0  # 缺货天数
        sales = int(recent["sales"] or 0) if recent else 0  # 销量合计
        if sales == 0 and out_days > 0:  # G2
            hits.append(_hit("G2_STOCKOUT_TRUNCATION", f"近 7 日销量为 0，但其中 {out_days} 天货架为 0，销量被缺货截断", {
                "window": f"{start}~{biz_date}", "sales": sales, "stockout_days": out_days,
            }))
        book = int(snapshot["book_stock"] or 0)  # 账面
        on_hand = int(snapshot["shelf_available"] or 0) + int(snapshot["backroom_stock"] or 0)  # 可售+后仓
        gap_ratio = abs(book - on_hand) / max(1, book)  # 偏差比例
        if snapshot["account_diff"] == 1 and gap_ratio >= 0.10:  # G3
            hits.append(_hit("G3_BOOK_SHELF_MISMATCH", f"账面 {book} 与货架+后仓 {on_hand} 偏差 {gap_ratio:.1%}", {
                "book_stock": book, "shelf_plus_backroom": on_hand, "gap_ratio": round(gap_ratio, 4),
                "diff_cause": snapshot.get("diff_cause"),
            }))
        missing: list[str] = []  # 缺失依据
        if sku is None or sku.get("case_size") in (None, 0):  # 整箱规格缺失
            missing.append("整箱规格 case_size")
        if store is None or not store.get("delivery_calendar_version"):  # 配送日历版本缺失
            missing.append("配送日历版本 delivery_calendar_version")
        if not snapshot.get("captured_at"):  # 快照采集时间缺失
            missing.append("快照采集时间 captured_at")
        if missing:  # G4
            hits.append(_hit("G4_MISSING_EVIDENCE", "关键依据缺失：" + "、".join(missing), {
                "missing_fields": missing, "note": "缺字段进人工修复队列，不猜测补齐（f33）",
            }))

    primary = hits[0] if hits else None  # 第一命中作为主原因
    confirm_at = (datetime.now(TZ) + timedelta(minutes=settings.guardrail.manual_confirm_minutes)).replace(microsecond=0).isoformat()  # 人工确认时限
    return {
        "hits": hits,
        "primary": primary,
        "stopped": bool(hits) and settings.guardrail.stop_actions_on_hit,
        "handoff": {
            "role": primary["owner_role"] if primary else None,
            "name": primary["owner_name"] if primary else None,
            "manual_confirm_at": confirm_at if primary else None,
        } if primary else None,
        "snapshot": snapshot, "sku": sku, "store": store,
    }
