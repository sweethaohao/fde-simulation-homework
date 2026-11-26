"""审计六要素、敏感字段脱敏、90 天留存与到期归档约束。"""

from __future__ import annotations  # 延迟注解


def test_audit_entries_have_six_elements(client, headers) -> None:
    """每条审计至少包含时间、主体、动作、对象、决定、请求号。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    items = client.get("/api/audit/logs", headers=headers["admin"], params={"limit": 100}).json()["data"]["items"]  # 日志
    assert items  # 非空
    for item in items:  # 逐条
        assert item["created_at"] and item["actor_name"] and item["action"]  # 时间/主体/动作
        assert item["resource"] is not None and item["decision"]  # 对象/决定
        assert item["request_id"]  # 请求号可串联


def test_sensitive_fields_are_redacted(client, headers) -> None:
    """口令、token 等敏感字段不得出现在审计里。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    client.post("/api/auth/login", json={"username": "admin", "password": "Demo@2026"})  # 制造一次成功登录
    client.post("/api/auth/login", json={"username": "admin", "password": "leak-me-please"})  # 制造一次失败登录
    raw = client.get("/api/audit/export", headers=headers["admin"]).text  # 导出 CSV（全量）
    assert "Demo@2026" not in raw  # 口令不得明文
    assert "leak-me-please" not in raw  # 错误口令不得落库
    assert "password" not in raw.lower() or "***" in raw  # 若出现字段名，值必须脱敏


def test_denied_action_is_logged(client, headers) -> None:
    """admin 尝试签核必须留痕为 deny + FORBIDDEN_ACTION。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    client.post("/api/replenishment/orders/ORD-DEMO-0001/sign", headers=headers["admin"])  # 触发越权
    items = client.get("/api/audit/logs", headers=headers["admin"], params={"limit": 200}).json()["data"]["items"]  # 日志
    matched = [i for i in items if i["error_code"] == "FORBIDDEN_ACTION"]  # 越权记录
    assert matched  # 有记录
    assert any(i["decision"] == "deny" for i in matched)  # 决定为拒绝


def test_retention_policy(client, headers) -> None:
    """审计留存 90 天，到期归档需 loss_manager 确认且不自动续期。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    data = client.get("/api/audit/retention", headers=headers["admin"]).json()["data"]  # 留存状态
    assert data["retention_days"] == 90  # 90 天
    assert data["archive_requires"] == "loss_manager"  # 归档确认人
    assert data["auto_renew"] is False  # 不自动续期


def test_export_is_csv(client, headers) -> None:
    """审计导出返回 CSV。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.get("/api/audit/export", headers=headers["admin"])  # 导出
    assert res.status_code == 200  # 成功
    assert "text/csv" in res.headers["content-type"]  # MIME
    assert "created_at" in res.text.splitlines()[0]  # 表头
