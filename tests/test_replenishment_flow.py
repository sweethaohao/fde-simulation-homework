"""建议 → 提交 → 签核 → 重建：硬约束是不存在下单接口，只到待签核与人工签核。"""

from __future__ import annotations  # 延迟注解


def _pick_order(client, headers, latest_biz_date) -> str:
    """生成一张非兜底建议单并返回单号。

    入参：client；headers；latest_biz_date。
    出参：order_id。
    失败时的行为：全部命中兜底时断言失败。
    """

    for index in range(10, 21):  # 在常规商品里找一个不触发兜底的
        res = client.post("/api/replenishment/suggest", headers=headers["planner"],
                          json={"store_id": "S01", "sku_id": f"K{index:03d}", "biz_date": latest_biz_date})  # 生成
        body = res.json()  # 信封
        if body.get("ok"):  # 成功
            return body["data"]["order_id"]  # 返回单号
    raise AssertionError("没有可用的非兜底样例")  # 失败


def test_full_flow_and_rebuild(client, headers, latest_biz_date) -> None:
    """计划员生成与提交，运营总监签核，随后可完整重建。

    入参：client；headers；latest_biz_date。
    出参：无。
    失败时的行为：断言失败。
    """

    order_id = _pick_order(client, headers, latest_biz_date)  # 生成
    detail = client.get(f"/api/replenishment/orders/{order_id}", headers=headers["planner"]).json()  # 详情
    assert detail["data"]["order"]["status"] == "DRAFT"  # 初始草稿
    assert detail["data"]["order"]["suggestion_qty"] is not None  # 建议量
    submit = client.post(f"/api/replenishment/orders/{order_id}/submit", headers=headers["planner"])  # 提交
    assert submit.status_code == 200 and submit.json()["data"]["order"]["status"] == "PENDING_SIGN"  # 待签核
    sign = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=headers["ops_director"])  # 签核
    assert sign.status_code == 200 and sign.json()["data"]["order"]["status"] == "SIGNED"  # 已签
    assert sign.json()["data"]["order"]["signed_by"]  # 签核人留痕
    rebuild = client.get(f"/api/replenishment/orders/{order_id}/rebuild", headers=headers["ops_director"]).json()  # 重建
    data = rebuild["data"]  # 数据
    assert data["snapshot"]["book_stock"] is not None  # 快照
    assert data["versions"]  # 四类版本
    assert data["suggestion"]["suggestion_qty"] == data["order"]["suggestion_qty"]  # 建议可回溯
    assert data["timeline"] and data["audit_trail"]  # 时间线与审计


def test_double_sign_is_409(client, headers, latest_biz_date) -> None:
    """重复签核返回 CONFLICT_ALREADY_SIGNED。

    入参：client；headers；latest_biz_date。
    出参：无。
    失败时的行为：断言失败。
    """

    order_id = _pick_order(client, headers, latest_biz_date)  # 生成
    client.post(f"/api/replenishment/orders/{order_id}/submit", headers=headers["planner"])  # 提交
    first = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=headers["ops_director"])  # 首次
    assert first.status_code == 200  # 成功
    second = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=headers["ops_director"])  # 再次
    assert second.status_code == 409 and second.json()["error"]["code"] == "CONFLICT_ALREADY_SIGNED"  # 冲突


def test_cannot_update_after_submit(client, headers, latest_biz_date) -> None:
    """已提交的单据不允许再改量（只允许草稿）。

    入参：client；headers；latest_biz_date。
    出参：无。
    失败时的行为：断言失败。
    """

    order_id = _pick_order(client, headers, latest_biz_date)  # 生成
    client.post(f"/api/replenishment/orders/{order_id}/submit", headers=headers["planner"])  # 提交
    res = client.patch(f"/api/replenishment/orders/{order_id}", headers=headers["planner"],
                       json={"final_qty": 1})  # 改量
    assert res.status_code == 422 and res.json()["error"]["code"] == "VALIDATION_ERROR"  # 非草稿拒绝修改


def test_reject_requires_reason_and_is_terminal(client, headers, latest_biz_date) -> None:
    """驳回需要原因，驳回后不能再签核。

    入参：client；headers；latest_biz_date。
    出参：无。
    失败时的行为：断言失败。
    """

    order_id = _pick_order(client, headers, latest_biz_date)  # 生成
    client.post(f"/api/replenishment/orders/{order_id}/submit", headers=headers["planner"])  # 提交
    bad = client.post(f"/api/replenishment/orders/{order_id}/reject", headers=headers["loss_manager"],
                      json={"reason": ""})  # 空原因
    assert bad.status_code == 422  # 校验失败
    good = client.post(f"/api/replenishment/orders/{order_id}/reject", headers=headers["loss_manager"],
                       json={"reason": "临时促销期不建议补货"})  # 正常驳回
    assert good.status_code == 200 and good.json()["data"]["order"]["status"] == "REJECTED"  # 已驳回
    sign = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=headers["ops_director"])  # 再签
    assert sign.status_code in (409, 422)  # 不允许


def test_graph_path_three_hops(client, headers, latest_biz_date) -> None:
    """建议节点可三跳回溯到规则与责任人，超过三跳被拒。

    入参：client；headers；latest_biz_date。
    出参：无。
    失败时的行为：断言失败。
    """

    order_id = _pick_order(client, headers, latest_biz_date)  # 生成
    res = client.get("/api/graph/path", headers=headers["admin"],
                     params={"src": f"suggestion:{order_id}", "depth": 3})  # 三跳
    assert res.status_code == 200 and res.json()["data"]["hops"]  # 有边
    too_deep = client.get("/api/graph/path", headers=headers["admin"],
                          params={"src": f"suggestion:{order_id}", "depth": 4})  # 四跳
    assert too_deep.status_code == 422  # 超限
