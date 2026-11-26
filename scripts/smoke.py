"""一条命令跑完全链路冒烟：健康、七角色登录、admin 越权、R10 拦截、五类兜底、建议→签核→重建、三跳、审计脱敏。

入参：命令行可选 `--base http://127.0.0.1:8000`；缺省自动探测，必要时用当前解释器拉起 uvicorn。
出参：退出码 0 表示全部通过；1 表示存在失败项；2 表示服务无法启动。
失败时的行为：打印失败原因，不吞异常；不改动数据库结构（只新增演示单据与审计）。
"""

from __future__ import annotations  # 延迟注解

import argparse  # 命令行参数
import json  # 审计脱敏检查
import os  # 环境变量
import subprocess  # 自动拉起服务
import sys  # 解释器
import time  # 等待启动
from pathlib import Path  # 路径

import httpx  # 与 requirements 同一依赖

ROOT = Path(__file__).resolve().parents[1]  # 仓库根
PASSWORD = "Demo@2026"  # 演示账号统一口令
ACCOUNTS = [  # 七个角色
    ("admin", "admin"),
    ("gu.minglan", "ops_director"),
    ("fu.jing", "loss_manager"),
    ("tang.yingqiu", "planner"),
    ("lin.yizhou", "store_manager"),
    ("ye.shu", "logistics"),
    ("qin.rui", "data_engineer"),
]
RESULTS: list[tuple[str, bool, str]] = []  # (名称, 是否通过, 说明)


def check(name: str, ok: bool, detail: str = "") -> None:
    """记录一条检查结果。

    入参：name 名称；ok 是否通过；detail 说明。
    出参：无。
    失败时的行为：不抛异常。
    """

    RESULTS.append((name, bool(ok), detail))  # 收集
    print(("PASS " if ok else "FAIL ") + name + (" · " + detail if detail else ""))  # 即时输出


def login(client: httpx.Client, username: str) -> dict | None:
    """登录并返回 token。

    入参：client；username。
    出参：{token, user, scope} 或 None。
    失败时的行为：失败返回 None，不抛异常。
    """

    res = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})  # 登录
    body = res.json()  # 统一信封
    if res.status_code == 200 and body.get("ok"):  # 成功
        return body["data"]  # 返回 data
    return None  # 失败


def auth(token: str) -> dict:
    """构造鉴权头。

    入参：token。
    出参：请求头字典。
    失败时的行为：不抛异常。
    """

    return {"Authorization": "Bearer " + token}  # Bearer 方案


def wait_ready(base: str, timeout: int = 60) -> bool:
    """等待 /api/health 可用。

    入参：base 服务地址；timeout 秒。
    出参：是否就绪。
    失败时的行为：超时返回 False。
    """

    deadline = time.time() + timeout  # 截止时间
    while time.time() < deadline:  # 轮询
        try:
            res = httpx.get(base + "/api/health", timeout=2)  # 探活
            if res.status_code == 200:  # 进程已起
                return True  # 就绪
        except httpx.HTTPError:  # 尚未监听
            pass  # 继续等待
        time.sleep(1)  # 间隔
    return False  # 超时


def start_server(base: str) -> subprocess.Popen | None:
    """按需拉起本地服务。

    入参：base 期望地址。
    出参：子进程或 None（已有服务时）。
    失败时的行为：无法启动返回 None。
    """

    try:
        httpx.get(base + "/api/health", timeout=1)  # 已有服务
        return None  # 复用
    except httpx.HTTPError:  # 没有服务
        pass  # 继续启动
    port = base.rsplit(":", 1)[-1]  # 端口
    env = dict(os.environ)  # 继承环境变量（含 .env 由应用自行读取）
    env.setdefault("APP_LLM_TIMEOUT", "20")  # 冒烟时缩短模型超时
    proc = subprocess.Popen(  # 启动 uvicorn
        [sys.executable, "-m", "uvicorn", "src.app.main:app", "--host", "127.0.0.1", "--port", port],
        cwd=str(ROOT), env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # 后台进程
    return proc  # 返回句柄


def run(base: str) -> int:
    """执行全部检查。

    入参：base 服务地址。
    出参：退出码。
    失败时的行为：单条失败不中断，最后汇总。
    """

    with httpx.Client(base_url=base, timeout=150) as client:  # 复用连接
        health = client.get("/api/health")  # 存活
        check("健康检查 /api/health", health.status_code == 200 and health.json().get("ok"), str(health.status_code))  # 记录
        ready = client.get("/api/ready")  # 就绪
        ready_body = ready.json()  # 信封
        check("就绪检查 /api/ready 真实探测", ready.status_code in (200, 503) and "checks" in (ready_body.get("data") or {}),
              "llm=" + str(((ready_body.get("data") or {}).get("checks") or {}).get("llm", {}).get("ok")))  # 允许 Ollama 未启动
        index = client.get("/")  # 单页
        check("单页可访问且含合成数据横幅", index.status_code == 200 and "合成数据" in index.text, str(index.status_code))  # 记录

        tokens: dict[str, str] = {}  # 账号 → token
        all_login = True  # 登录总标志
        for username, role in ACCOUNTS:  # 逐个登录
            data = login(client, username)  # 登录
            if not data:  # 失败
                all_login = False  # 标记
                continue  # 下一个
            tokens[role] = data["token"]  # 保存 token
            me = client.get("/api/auth/me", headers=auth(data["token"]))  # 校验身份
            if me.status_code != 200 or me.json()["data"]["user"]["role"] != role:  # 角色不符
                all_login = False  # 标记
        check("七个角色可登录且角色正确", all_login and len(tokens) == 7, f"{len(tokens)}/7")  # 记录

        admin = tokens.get("admin", "")  # 管理员
        planner = tokens.get("planner", "")  # 计划员
        ops = tokens.get("ops_director", "")  # 运营总监

        demo_sign = client.post("/api/replenishment/orders/ORD-DEMO-0001/sign", headers=auth(admin))  # admin 试图签核
        demo_sign_body = demo_sign.json()  # 信封
        check("admin 签核被拒 FORBIDDEN_ACTION",
              demo_sign.status_code == 403 and (demo_sign_body.get("error") or {}).get("code") == "FORBIDDEN_ACTION",
              str(demo_sign.status_code))  # 记录

        blocked = client.get("/api/metrics/turnover_days", headers=auth(admin))  # 未签发指标
        blocked_body = blocked.json()  # 信封
        check("未签发指标不出数 METRIC_BLOCKED",
              blocked.status_code == 200 and blocked_body.get("ok") is False
              and (blocked_body.get("error") or {}).get("code") == "METRIC_BLOCKED"
              and not (blocked_body.get("data") or {}).get("value"),
              "code=" + str((blocked_body.get("error") or {}).get("code")))  # 记录

        dash = client.get("/api/dashboard/summary", headers=auth(admin))  # 看板
        dash_data = (dash.json().get("data") or {})  # 数据
        latest = dash_data.get("as_of")  # 最新业务日
        snap = (dash_data.get("stockout") or {}).get("snapshot") or {}  # 当日截面
        tw = (dash_data.get("stockout") or {}).get("time_weighted") or {}  # 时段加权
        check("缺货率两种分母并列且差 9pp 左右",
              abs(float(snap.get("rate") or 0) - 0.12) < 0.02 and abs(float(tw.get("rate") or 0) - 0.21) < 0.03,
              f"{snap.get('rate')} vs {tw.get('rate')}")  # 记录

        cases = [  # 五类兜底样例（显式给业务日，避免 S07 自动取到旧日期）
            ("G1 临时促销", "S01", "K001", "G1_TEMP_PROMO"),
            ("G2 缺货截断", "S02", "K002", "G2_STOCKOUT_TRUNCATION"),
            ("G3 账实不一致", "S03", "K003", "G3_BOOK_SHELF_MISMATCH"),
            ("G4 依据缺失（箱规）", "S04", "K049", "G4_MISSING_EVIDENCE"),
            ("G5 关键系统不可用", "S07", "K006", "G5_SYSTEM_UNAVAILABLE"),
        ]
        for label, store_id, sku_id, rule_code in cases:  # 逐个触发
            res = client.post("/api/replenishment/suggest", headers=auth(planner),
                              json={"store_id": store_id, "sku_id": sku_id, "biz_date": latest})  # 生成
            body = res.json()  # 信封
            data = body.get("data") or {}  # 停止详情
            hit_codes = [h.get("rule_code") for h in (data.get("hits") or [])]  # 命中规则
            ok = (res.status_code == 200 and body.get("ok") is False
                  and (body.get("error") or {}).get("code") == "GUARDRAIL_STOPPED"
                  and data.get("suggestion") is None and rule_code in hit_codes)  # 判定
            check("兜底拦截：" + label, ok, ",".join(x or "" for x in hit_codes))  # 记录

        order_id = None  # 正常链路单据
        for sku_id in [f"K{n:03d}" for n in range(10, 21)]:  # 找一个不命中兜底的商品
            res = client.post("/api/replenishment/suggest", headers=auth(planner),
                              json={"store_id": "S01", "sku_id": sku_id, "biz_date": latest})  # 生成建议
            body = res.json()  # 信封
            if body.get("ok") and (body.get("data") or {}).get("order_id"):  # 成功
                order_id = body["data"]["order_id"]  # 记录
                break  # 结束寻找
        check("补货计划员可生成建议（非兜底）", bool(order_id), str(order_id))  # 记录

        if order_id:  # 全链路
            submit = client.post(f"/api/replenishment/orders/{order_id}/submit", headers=auth(planner))  # 提交
            check("提交待签核", submit.status_code == 200 and submit.json()["data"]["order"]["status"] == "PENDING_SIGN",
                  str(submit.status_code))  # 记录
            sign = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=auth(ops))  # 签核
            check("运营总监签核成功", sign.status_code == 200 and sign.json()["data"]["order"]["status"] == "SIGNED",
                  str(sign.status_code))  # 记录
            again = client.post(f"/api/replenishment/orders/{order_id}/sign", headers=auth(ops))  # 重复签核
            check("重复签核被拒 CONFLICT_ALREADY_SIGNED",
                  again.status_code == 409 and (again.json().get("error") or {}).get("code") == "CONFLICT_ALREADY_SIGNED",
                  str(again.status_code))  # 记录
            rebuild = client.get(f"/api/replenishment/orders/{order_id}/rebuild", headers=auth(ops))  # 重建
            rb = rebuild.json().get("data") or {}  # 数据
            check("重建视图含快照/版本/审计/时间线",
                  rebuild.status_code == 200 and rb.get("snapshot") is not None and bool(rb.get("audit_trail"))
                  and bool(rb.get("timeline")), str(len(rb.get("audit_trail") or [])))  # 记录
            path = client.get("/api/graph/path", headers=auth(admin),
                              params={"src": f"suggestion:{order_id}", "depth": 3})  # 三跳
            hops = (path.json().get("data") or {}).get("hops") or []  # 边
            check("建议三跳溯源可用", path.status_code == 200 and len(hops) > 0, f"{len(hops)} 条边")  # 记录

        client.post("/api/auth/login", json={"username": "admin", "password": "wrong-password"})  # 制造一次失败登录
        logs = client.get("/api/audit/logs", headers=auth(admin), params={"limit": 300})  # 审计
        raw = json.dumps(logs.json(), ensure_ascii=False)  # 序列化检查
        check("审计不含演示口令明文", PASSWORD not in raw and "wrong-password" not in raw, "扫描 300 条")  # 记录
        check("越权尝试已留痕", any((item.get("error_code") == "FORBIDDEN_ACTION")
                                    for item in (logs.json().get("data") or {}).get("items", [])), "")  # 记录

        readiness = client.get("/api/admin/import", headers=auth(tokens.get("data_engineer", "")))  # 就绪度
        items = ((readiness.json().get("data") or {}).get("readiness") or {}).get("items") or []  # 明细
        check("数据就绪度按指标拆项", readiness.status_code == 200 and len(items) >= 5, f"{len(items)} 项")  # 记录

    failed = [r for r in RESULTS if not r[1]]  # 失败项
    print("\n===== 冒烟汇总：%d 通过 / %d 失败 =====" % (len(RESULTS) - len(failed), len(failed)))  # 汇总
    return 0 if not failed else 1  # 退出码


def main() -> int:
    """入口。

    入参：命令行参数。
    出参：退出码。
    失败时的行为：服务无法启动返回 2。
    """

    parser = argparse.ArgumentParser(description="MVP 全链路冒烟")  # 解析器
    parser.add_argument("--base", default=os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000"))  # 基地址
    args = parser.parse_args()  # 解析
    base = args.base.rstrip("/")  # 去尾斜杠
    proc = start_server(base)  # 按需拉起
    if not wait_ready(base):  # 等待
        print("[ERROR] 服务未在 60 秒内就绪，请手动运行 scripts/run_dev.bat 查看日志")  # 提示
        if proc:  # 清理
            proc.terminate()  # 结束
        return 2  # 无法启动
    try:
        return run(base)  # 执行检查
    finally:
        if proc:  # 只清理本次拉起的进程
            proc.terminate()  # 结束
            try:
                proc.wait(timeout=10)  # 等待退出
            except subprocess.TimeoutExpired:  # 超时
                proc.kill()  # 强杀


if __name__ == "__main__":  # 直接运行
    sys.exit(main())  # 返回退出码
