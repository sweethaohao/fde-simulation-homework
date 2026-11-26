"""诊断 Agent：把缺货拆成「后仓有货未上架 / 真正无货 / 已签收未上架」。

统计全部由 SQL 确定性算出；LLM 只可能做摘要，本实现不调用模型，避免数字漂移。
"""

from __future__ import annotations  # 延迟注解

from datetime import date, timedelta  # 日期

from . import registry  # 留痕
from ..services import metrics  # 复用最新业务日


def run(conn, store_id: str, days: int = 7) -> dict:
    """执行门店缺货诊断。

    入参：conn；store_id；days 观察窗口天数。
    出参：分类计数、摘要与依据。
    失败时的行为：SQL 异常向上抛出；无数据时返回 0 计数并说明。
    """

    latest = metrics.latest_biz_date(conn, [store_id])  # 最新有数据的日期
    if not latest:  # 门店无快照
        return {"store_id": store_id, "window": None, "stockout_skus": 0, "not_on_shelf": 0,
                "no_stock": 0, "replenished_not_shelved": 0, "summary": "无快照数据，无法诊断", "source": "rules"}
    start = (date.fromisoformat(latest) - timedelta(days=days - 1)).isoformat()  # 窗口起点
    rows = conn.execute(
        """
        SELECT sku_id, MAX(book_stock) AS book, MAX(backroom_stock) AS backroom,
               MAX(CASE WHEN shelf_available = 0 THEN 1 ELSE 0 END) AS out_flag
        FROM daily_snapshots WHERE store_id = ? AND biz_date BETWEEN ? AND ?
        GROUP BY sku_id
        """,
        (store_id, start, latest),
    ).fetchall()  # 按 SKU 聚合
    stockout_skus = {r["sku_id"] for r in rows if r["out_flag"] == 1}  # 窗口内出现过缺货的 SKU
    not_on_shelf = {r["sku_id"] for r in rows if r["out_flag"] == 1 and int(r["backroom"] or 0) > 0}  # 有货不可售
    no_stock = {r["sku_id"] for r in rows if r["out_flag"] == 1 and int(r["book"] or 0) == 0}  # 真正无货
    signed = conn.execute(
        """
        SELECT DISTINCT o.sku_id FROM replenishment_orders o
        WHERE o.store_id = ? AND o.status = 'SIGNED' AND o.biz_date BETWEEN ? AND ?
        """,
        (store_id, start, latest),
    ).fetchall()  # 窗口内已签收的 SKU
    signed_skus = {r["sku_id"] for r in signed}  # 集合
    replenished_not_shelved = {r["sku_id"] for r in rows if r["out_flag"] == 1 and r["sku_id"] in signed_skus}  # 签收后仍缺货
    summary = (
        f"近 {days} 天 {store_id} 有 {len(stockout_skus)} 个 SKU 出现货架为 0："
        f"后仓有货未上架 {len(not_on_shelf)} 个、真正无货 {len(no_stock)} 个、"
        f"已签收仍未上架 {len(replenished_not_shelved)} 个。"
    )
    registry.record_run(
        "tool", agent="diagnose", tool="metrics.query",
        input_summary=f"{store_id} 近 {days} 天", output_summary=summary,
        status="ok", detail={"stockout_skus": len(stockout_skus)},
    )  # Harness 留痕
    return {
        "store_id": store_id, "window": {"start": start, "end": latest, "days": days},
        "stockout_skus": len(stockout_skus), "not_on_shelf": len(not_on_shelf),
        "no_stock": len(no_stock), "replenished_not_shelved": len(replenished_not_shelved),
        "summary": summary, "source": "rules", "evidence_f": ["f10", "f13", "f31"],
    }
