"""f44 五类兜底：全部命中即停，不返回建议量，接手人为商品与损耗经理。"""

from __future__ import annotations  # 延迟注解

import pytest  # 参数化

CASES = [  # (说明, 门店, 商品, 期望规则)
    ("G1 临时促销", "S01", "K001", "G1_TEMP_PROMO"),
    ("G2 缺货截断", "S02", "K002", "G2_STOCKOUT_TRUNCATION"),
    ("G3 可售与账面不一致", "S03", "K003", "G3_BOOK_SHELF_MISMATCH"),
    ("G4 依据缺失（整箱规格）", "S04", "K049", "G4_MISSING_EVIDENCE"),
    ("G5 关键系统不可用", "S07", "K006", "G5_SYSTEM_UNAVAILABLE"),
]


@pytest.mark.parametrize("label,store_id,sku_id,rule_code", CASES)  # 五条
def test_guardrail_stops(client, headers, latest_biz_date, label, store_id, sku_id, rule_code) -> None:
    """五类兜底都必须 200 + ok=false + GUARDRAIL_STOPPED。

    入参：client；headers；latest_biz_date；样例参数。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.post("/api/replenishment/suggest", headers=headers["planner"],
                      json={"store_id": store_id, "sku_id": sku_id, "biz_date": latest_biz_date})  # 生成建议
    body = res.json()  # 信封
    assert res.status_code == 200  # 业务阻塞仍 200
    assert body["ok"] is False  # 业务级失败
    assert body["error"]["code"] == "GUARDRAIL_STOPPED"  # 固定错误码
    data = body["data"]  # 停止详情（统一信封把附加数据放在顶层 data）
    assert data["suggestion"] is None  # 不返回建议量
    assert data["status"] == "STOPPED"  # 单据状态
    assert data["hits"], label  # 至少一条命中
    assert rule_code in [hit["rule_code"] for hit in data["hits"]]  # 命中期望规则
    assert data["handoff"]["name"] == "傅景"  # 接手人
    assert data["handoff"]["manual_confirm_at"]  # 人工确认时限


def test_stopped_order_is_rebuildable(client, headers) -> None:
    """兜底单据可以重建，且保留 f44 依据。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    orders = client.get("/api/replenishment/orders", headers=headers["planner"],
                        params={"status": "STOPPED"}).json()["data"]["items"]  # 兜底单
    assert orders, "至少有一张 STOPPED 单"  # 存在
    order_id = orders[0]["order_id"]  # 取一张
    rebuild = client.get(f"/api/replenishment/orders/{order_id}/rebuild", headers=headers["planner"])  # 重建
    assert rebuild.status_code == 200 and rebuild.json()["ok"]  # 成功
    data = rebuild.json()["data"]  # 数据
    assert data["guardrail"] is not None  # 兜底详情
    assert data["suggestion"] is None  # 没有建议量
    assert data["order"]["status"] == "STOPPED"  # 状态保留
