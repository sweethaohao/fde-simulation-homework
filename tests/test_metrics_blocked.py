"""R10 指标口径：未签发不出数、数据不足不填估算值、两种分母并列。"""

from __future__ import annotations  # 延迟注解


def test_turnover_days_blocked(client, headers) -> None:
    """库存周转天数未签发 → METRIC_BLOCKED 且没有数字。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.get("/api/metrics/turnover_days", headers=headers["admin"])  # 查询
    body = res.json()  # 信封
    assert res.status_code == 200 and body["ok"] is False  # 业务拦截 200
    assert body["error"]["code"] == "METRIC_BLOCKED"  # 固定错误码
    data = body["data"]  # 指标详情
    assert data.get("no_value") is True  # 明确无值
    assert "value" not in data  # 不填估算值
    assert data["signature"]["signed"] is False  # 未签发
    assert data["signature"]["required_signer"] == "顾明岚"  # 签发人


def test_shelf_restock_hours_data_not_ready(client, headers) -> None:
    """上架时效因 shelved 无时间戳 → DATA_NOT_READY。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    res = client.get("/api/metrics/shelf_restock_hours", headers=headers["admin"])  # 查询
    body = res.json()  # 信封
    assert res.status_code == 200 and body["ok"] is False  # 业务拦截
    assert body["error"]["code"] == "DATA_NOT_READY"  # 数据不足
    assert body["data"].get("value") is None  # 无值


def test_stockout_rate_two_denominators(client, headers) -> None:
    """缺货率两种分母并列，差约 9pp，不抹平。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    body = client.get("/api/metrics/stockout_rate", headers=headers["admin"]).json()  # 查询
    assert body["ok"] is True  # 已签发可出数
    data = body["data"]  # 详情
    keys = [v["key"] for v in data["variants"]]  # 两种口径
    assert keys == ["snapshot", "time_weighted"]  # 顺序固定
    snap, weighted = data["variants"]  # 拆开
    assert snap["denominator"] > 0 and weighted["denominator"] > 0  # 分母有效
    assert abs(snap["rate"] - 0.12) < 0.02  # 当日截面约 12%
    assert abs(weighted["rate"] - 0.21) < 0.03  # 时段加权约 21%
    assert abs(data["gap_pp"] - 9.0) <= 1.5  # 差约 9pp


def test_account_diff_split(client, headers) -> None:
    """账实差异约 8%，编码伪差异约占三分之一。

    入参：client；headers。
    出参：无。
    失败时的行为：断言失败。
    """

    data = client.get("/api/metrics/account_diff_rate", headers=headers["admin"]).json()["data"]  # 查询
    assert abs(data["value"] - 0.08) < 0.02  # 约 8%
    sample = data["sample"]  # 样本
    variants = {v["key"]: v for v in data["variants"]}  # 口径
    assert variants["code_pseudo"]["count"] > 0  # 有伪差异
    ratio = variants["code_pseudo"]["count"] / max(1, sample["flagged"])  # 伪差异占比
    assert 0.2 < ratio < 0.5  # 约三分之一
