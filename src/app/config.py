"""配置读取：config.yaml + .env + 环境变量覆盖。

硬约束：密钥、模型地址、端口、库路径只出现在配置里；代码中只读 settings.xxx。
"""

# 标准库
from __future__ import annotations

import os  # 读环境变量（优先级最高）
from dataclasses import dataclass, field  # 轻量配置对象
from pathlib import Path  # 路径解析
from typing import Any  # YAML 原始结构

import yaml  # 解析 config.yaml

# 仓库根 = src/app/config.py 向上三级
BASE_DIR = Path(__file__).resolve().parents[2]  # 所有相对路径都相对仓库根解析


@dataclass
class AppConfig:
    """应用级配置。"""

    name: str = "门店补货与库存协同 · MVP"  # 应用名
    host: str = "0.0.0.0"  # 监听地址
    port: int = 8000  # 监听端口
    env: str = "prod"  # 运行环境
    data_scope_default: str = "store"  # 缺省最小数据域


@dataclass
class DbConfig:
    """数据库配置。"""

    path: str = "data/mvp.db"  # SQLite 文件路径（相对仓库根）


@dataclass
class LlmConfig:
    """本地 Ollama 配置。"""

    base_url: str = "http://127.0.0.1:11434"  # Ollama 地址
    chat_model: str = "qwen3.5:9b"  # 生成模型
    embed_model: str = "nomic-embed-text"  # 嵌入模型
    think: bool = False  # 必须 false，否则 content 为空
    temperature: float = 0.2  # 采样温度
    num_ctx: int = 8192  # 上下文窗口
    num_predict: int = 420  # 单次生成上限
    timeout_seconds: int = 120  # 请求超时
    max_context_chars: int = 6000  # 送入 LLM 的检索片段总字符上限


@dataclass
class RetrievalConfig:
    """检索配置。"""

    top_k: int = 6  # 返回片段数
    vector_dim: int = 768  # 向量维度
    fallback: str = "fts5"  # 降级检索方式


@dataclass
class AuthConfig:
    """认证配置。"""

    jwt_secret_env: str = "APP_JWT_SECRET"  # 密钥所在环境变量名
    token_ttl_minutes: int = 480  # token 有效期
    password_hash: str = "scrypt"  # 哈希算法


@dataclass
class GuardrailConfig:
    """兜底配置。"""

    enabled: bool = True  # 总开关
    stop_actions_on_hit: bool = True  # 命中即停
    manual_confirm_minutes: int = 120  # 人工确认时限


@dataclass
class AuditConfig:
    """审计配置。"""

    retention_days: int = 90  # R6 / f43
    redact_fields: list[str] = field(default_factory=lambda: ["password", "token", "secret", "phone", "id_card"])  # 白名单外的敏感键


@dataclass
class MetricsConfig:
    """指标配置。"""

    signed_required: bool = True  # 未签发不出数
    stockout_window_days: int = 42  # 时长加权窗口
    stockout_recent_days: int = 7  # 近期窗口


@dataclass
class ReplenishmentConfig:
    """补货计算参数。"""

    lead_time_days: int = 2  # 前置期
    cover_days: int = 7  # 目标覆盖天数
    safety_ratio: float = 0.30  # 安全库存比例
    warehouse_cap_units: int = 240  # 仓容上限


@dataclass
class DemoConfig:
    """演示配置。"""

    synthetic_banner: bool = True  # 合成数据横幅


@dataclass
class Settings:
    """聚合配置对象。"""

    app: AppConfig = field(default_factory=AppConfig)  # 应用
    db: DbConfig = field(default_factory=DbConfig)  # 数据库
    llm: LlmConfig = field(default_factory=LlmConfig)  # 模型
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)  # 检索
    auth: AuthConfig = field(default_factory=AuthConfig)  # 认证
    guardrail: GuardrailConfig = field(default_factory=GuardrailConfig)  # 兜底
    audit: AuditConfig = field(default_factory=AuditConfig)  # 审计
    metrics: MetricsConfig = field(default_factory=MetricsConfig)  # 指标
    replenishment: ReplenishmentConfig = field(default_factory=ReplenishmentConfig)  # 补货
    demo: DemoConfig = field(default_factory=DemoConfig)  # 演示
    base_dir: Path = BASE_DIR  # 仓库根

    @property
    def db_path(self) -> Path:
        """数据库绝对路径。

        入参：无。
        出参：Path 绝对路径。
        失败时的行为：不抛异常（父目录由 db 层创建）。
        """

        p = Path(self.db.path)  # 可能是相对仓库根的路径
        return p if p.is_absolute() else (self.base_dir / p)  # 统一返回绝对路径

    @property
    def web_dir(self) -> Path:
        """前端静态目录。

        入参：无。
        出参：src/web 的绝对路径。
        失败时的行为：不抛异常。
        """

        return self.base_dir / "src" / "web"  # FastAPI 静态托管的根

    @property
    def seed_dir(self) -> Path:
        """合成数据 CSV 目录。

        入参：无。
        出参：data/seed 的绝对路径。
        失败时的行为：不抛异常。
        """

        return self.base_dir / "data" / "seed"  # 可提交的合成数据产物


def _load_dotenv(path: Path) -> None:
    """极简 .env 载入（不引入额外依赖）。

    入参：path .env 文件路径。
    出参：无（写入 os.environ，已有环境变量优先）。
    失败时的行为：文件不存在时静默跳过；解析失败的行忽略。
    """

    if not path.exists():  # 没有 .env 也允许运行（生产用真实环境变量）
        return
    for raw in path.read_text(encoding="utf-8").splitlines():  # 逐行解析 KEY=VALUE
        line = raw.strip()  # 去首尾空白
        if not line or line.startswith("#") or "=" not in line:  # 跳过空行与注释
            continue
        key, value = line.split("=", 1)  # 仅按第一个等号切分
        key, value = key.strip(), value.strip().strip('"').strip("'")  # 去空白与引号
        if key and key not in os.environ:  # 环境变量优先级最高
            os.environ[key] = value  # 写回进程环境


def _apply_env_overrides(cfg: Settings) -> Settings:
    """环境变量覆盖配置文件。

    入参：cfg 已从 YAML 读出的配置。
    出参：同一个 cfg（就地修改）。
    失败时的行为：数值转换失败时抛 ValueError（启动即失败，避免带病运行）。
    """

    mapping = {
        "APP_ENV": ("app", "env", str),  # 运行环境
        "APP_HOST": ("app", "host", str),  # 监听地址
        "APP_PORT": ("app", "port", int),  # 监听端口
        "APP_DB_PATH": ("db", "path", str),  # 数据库路径（测试用）
        "OLLAMA_BASE_URL": ("llm", "base_url", str),  # 模型地址
        "APP_LLM_CHAT_MODEL": ("llm", "chat_model", str),  # 生成模型
        "APP_LLM_TIMEOUT": ("llm", "timeout_seconds", int),  # 超时
        "APP_LLM_NUM_PREDICT": ("llm", "num_predict", int),  # 生成上限
    }
    for env_key, (section, attr, cast) in mapping.items():  # 逐个检查覆盖项
        if env_key in os.environ:  # 只覆盖显式设置的环境变量
            target = getattr(cfg, section)  # 取得对应配置段
            setattr(target, attr, cast(os.environ[env_key]))  # 类型化后写回
    return cfg  # 返回覆盖后的配置


def load_settings(config_path: Path | None = None) -> Settings:
    """读取配置。

    入参：config_path 可选的 YAML 路径，默认仓库根 config.yaml。
    出参：Settings 聚合配置。
    失败时的行为：YAML 解析失败或类型不符时抛异常（启动失败优于静默用错配置）。
    """

    _load_dotenv(BASE_DIR / ".env")  # 先装载 .env（本地演示方便）
    path = config_path or (BASE_DIR / "config.yaml")  # 默认配置文件位置
    raw: dict[str, Any] = {}  # YAML 原始结构
    if path.exists():  # 配置缺失时使用 dataclass 默认值
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}  # 允许空文件
    cfg = Settings()  # 以默认值兜底
    for section_name, section_obj in (  # 逐段把 YAML 值写到 dataclass
        ("app", cfg.app),
        ("db", cfg.db),
        ("llm", cfg.llm),
        ("retrieval", cfg.retrieval),
        ("auth", cfg.auth),
        ("guardrail", cfg.guardrail),
        ("audit", cfg.audit),
        ("metrics", cfg.metrics),
        ("replenishment", cfg.replenishment),
        ("demo", cfg.demo),
    ):
        for key, value in (raw.get(section_name) or {}).items():  # 只覆盖声明的字段
            if hasattr(section_obj, key):  # 忽略未知字段，避免配置笔误导致崩溃
                setattr(section_obj, key, value)  # 写入配置值
    return _apply_env_overrides(cfg)  # 环境变量优先级最高


settings = load_settings()  # 进程级配置单例（测试在 import 前设好环境变量）
