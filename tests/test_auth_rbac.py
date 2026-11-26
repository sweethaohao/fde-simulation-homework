"""认证与三层权限：七角色登录、admin 只能看、店长越域被拒。"""

from __future__ import annotations  # 延迟注解

import pytest  # 测试框架


def test_seven_roles_login(client, tokens, headers) -> None:
    """七个角色可登录且 /auth/me 返回正确角色。

    入参：client；tokens；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    assert len(tokens) == 7  # 七个 token
    for role in tokens:  # 逐个校验
        me = client.get("/api/auth/me", headers=headers[role])  # 当前身份
        assert me.status_code == 200 and me.json()["ok"]  # 成功
        assert me.json()["data"]["user"]["role"] == role  # 角色一致
        assert "actions" in me.json()["data"]  # 返回动作集合


def test_missing_token_is_401(client) -> None:
    """未带 token 访问受保护接口返回 401 AUTH_REQUIRED。

    入参：client。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.get("/api/auth/me")  # 未认证
    assert res.status_code == 401  # HTTP 401
    assert res.json()["error"]["code"] == "AUTH_REQUIRED"  # 固定错误码


def test_wrong_password_is_401_and_logged(client, headers) -> None:
    """错误口令返回 401，且审计里有 deny 记录。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.post("/api/auth/login", json={"username": "admin", "password": "bad-password"})  # 错误登录
    assert res.status_code == 401 and res.json()["error"]["code"] == "AUTH_INVALID"  # 固定错误码
    logs = client.get("/api/audit/logs", headers=headers["admin"], params={"limit": 100}).json()  # 审计
    assert any(item["action"] == "auth.login" and item["decision"] == "deny"
               for item in logs["data"]["items"])  # 存在失败留痕


def test_admin_can_see_all_but_cannot_sign(client, headers) -> None:
    """admin 数据域为全局，但签核必须 FORBIDDEN_ACTION。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    me = client.get("/api/auth/me", headers=headers["admin"]).json()  # 身份
    assert me["data"]["scope"]["type"] == "global"  # 全局域
    assert len(me["data"]["scope"]["stores"]) == 8  # 八家门店可见
    sign = client.post("/api/replenishment/orders/ORD-DEMO-0001/sign", headers=headers["admin"])  # 尝试签核
    assert sign.status_code == 403  # 拒绝
    assert sign.json()["error"]["code"] == "FORBIDDEN_ACTION"  # 动作层拒绝
    assert sign.json()["data"]["hint"]  # 明示已留痕


def test_store_manager_scope_is_limited(client, headers) -> None:
    """店长只看本店，查询其他门店必须 403 FORBIDDEN_SCOPE。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    me = client.get("/api/auth/me", headers=headers["store_manager"]).json()  # 身份
    assert me["data"]["scope"]["type"] == "store"  # 门店域
    assert [s["store_id"] for s in me["data"]["scope"]["stores"]] == ["S01"]  # 仅 S01
    own = client.get("/api/metrics/stockout_rate", headers=headers["store_manager"], params={"store_id": "S01"})  # 本店
    assert own.status_code == 200  # 允许
    other = client.get("/api/metrics/stockout_rate", headers=headers["store_manager"], params={"store_id": "S02"})  # 越域
    assert other.status_code == 403 and other.json()["error"]["code"] == "FORBIDDEN_SCOPE"  # 拒绝


def test_planner_cannot_sign(client, headers) -> None:
    """计划员没有签核动作权限。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.post("/api/replenishment/orders/ORD-DEMO-0001/sign", headers=headers["planner"])  # 计划员尝试签核
    assert res.status_code == 403 and res.json()["error"]["code"] == "FORBIDDEN_ACTION"  # 拒绝


@pytest.mark.parametrize("role,action", [("data_engineer", "order.view"), ("store_manager", "suggestion.generate")])  # 角色动作边界
def test_role_action_boundaries(client, headers, role, action) -> None:
    """未授予的动作必须拒绝（fail closed）。

    入参：client；headers；role；action。
    出参：无。
    失败时的行为：断言失败。
    """

    if action == "order.view":  # 数据工程师无单据查看
        res = client.get("/api/replenishment/orders", headers=headers[role])  # 请求
    else:  # 店长不能生成建议
        res = client.post("/api/replenishment/suggest", headers=headers[role],
                          json={"store_id": "S01", "sku_id": "K010"})  # 请求
    assert res.status_code == 403 and res.json()["error"]["code"] == "FORBIDDEN_ACTION"  # 必须拒绝
