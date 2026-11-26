"""pytest 共享夹具：临时数据库、七个角色 token、TestClient。

关键顺序：先设置 APP_DB_PATH / 密钥等环境变量，再 import 应用，
否则 config.settings 单例会绑定到默认的 data/mvp.db，污染真实演示库。
"""

from __future__ import annotations  # 延迟注解

import os  # 环境变量
import sys  # 模块路径
import tempfile  # 临时目录
from pathlib import Path  # 路径

import pytest  # 测试框架

ROOT = Path(__file__).resolve().parents[1]  # 仓库根
if str(ROOT) not in sys.path:  # 允许导入 src 与 scripts
    sys.path.insert(0, str(ROOT))  # 加入搜索路径

_TMP = Path(tempfile.mkdtemp(prefix="fde-mvp-test-"))  # 会话级临时目录
os.environ["APP_DB_PATH"] = str(_TMP / "test.db")  # 测试独立库
os.environ["APP_JWT_SECRET"] = "test-only-secret-0123456789abcdef"  # 测试密钥（非真实）
os.environ["OLLAMA_BASE_URL"] = "http://127.0.0.1:9"  # 指向不可达端口，强制走规则降级
os.environ["APP_LLM_TIMEOUT"] = "1"  # 超时压到 1 秒，避免测试卡住
os.environ["APP_LLM_NUM_PREDICT"] = "32"  # 降低生成长度

from scripts.init_db import build_database  # noqa: E402  # 复用建库脚本
from scripts.seed_graph import seed as seed_graph  # noqa: E402  # 复用图谱种子（节点/边/FTS/向量降级）
from fastapi.testclient import TestClient  # noqa: E402  # 进程内测试客户端
from src.app.db import get_conn  # noqa: E402  # 读取最新业务日
from src.app.main import app  # noqa: E402  # 应用实例（此时已绑定测试库）

PASSWORD = "Demo@2026"  # 演示口令
ACCOUNTS = {  # 角色 → 账号
    "admin": "admin",
    "ops_director": "gu.minglan",
    "loss_manager": "fu.jing",
    "planner": "tang.yingqiu",
    "store_manager": "lin.yizhou",
    "logistics": "ye.shu",
    "data_engineer": "qin.rui",
}


@pytest.fixture(scope="session", autouse=True)
def prepared_db() -> None:
    """准备测试数据库。

    入参：无。
    出参：无。
    失败时的行为：缺 CSV 抛 FileNotFoundError，测试直接报错。
    """

    build_database(os.environ["APP_DB_PATH"], ROOT / "data" / "seed", reset=True)  # 幂等重建
    seed_graph(os.environ["APP_DB_PATH"])  # 图谱节点/边/FTS；向量在不可达 Ollama 下自动跳过
    yield  # 交由测试使用


@pytest.fixture(scope="session")
def client(prepared_db) -> TestClient:
    """创建 TestClient。

    入参：prepared_db 已建库。
    出参：TestClient。
    失败时的行为：应用启动异常直接抛出。
    """

    return TestClient(app)  # 进程内调用，不占端口


@pytest.fixture(scope="session")
def tokens(client: TestClient) -> dict[str, str]:
    """登录全部角色并返回 token。

    入参：client。
    出参：{role: token}。
    失败时的行为：登录失败断言失败。
    """

    result: dict[str, str] = {}  # 结果
    for role, username in ACCOUNTS.items():  # 逐个登录
        res = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})  # 登录
        assert res.status_code == 200 and res.json()["ok"], username  # 必须成功
        result[role] = res.json()["data"]["token"]  # 保存
    return result  # 返回


@pytest.fixture(scope="session")
def headers(tokens: dict[str, str]) -> dict[str, dict]:
    """把 token 转成请求头字典。

    入参：tokens。
    出参：{role: {"Authorization": ...}}。
    失败时的行为：不抛异常。
    """

    return {role: {"Authorization": "Bearer " + token} for role, token in tokens.items()}  # Bearer


@pytest.fixture(scope="session")
def latest_biz_date() -> str:
    """读取数据域内最新业务日。

    入参：无。
    出参：日期字符串。
    失败时的行为：无数据时断言失败。
    """

    with get_conn() as conn:  # 独立连接
        row = conn.execute("SELECT MAX(biz_date) AS d FROM daily_snapshots").fetchone()  # 最大日期
    assert row and row["d"]  # 必须有数据
    return row["d"]  # 返回
