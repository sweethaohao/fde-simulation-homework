"""密码哈希与 JWT 签发校验。

密码使用标准库 hashlib.scrypt（不引入 bcrypt / passlib）；JWT 使用 PyJWT HS256。
密钥只从环境变量读取，未设置时启动直接失败（不允许硬编码兜底密钥）。
"""

from __future__ import annotations  # 延迟注解

import hashlib  # scrypt 哈希
import hmac  # 常量时间比较，避免时序侧信道
import os  # 读取密钥环境变量
import secrets  # 生成随机盐与 jti
from datetime import datetime, timedelta, timezone  # token 时间

import jwt  # PyJWT

from .config import settings  # 配置单例
from .errors import AppError, ErrorCode  # 统一错误

# scrypt 参数固定为提示词要求的值，改动会使已入库口令全部失效
SCRYPT_N = 2 ** 14  # CPU/内存成本
SCRYPT_R = 8  # 块大小
SCRYPT_P = 1  # 并行度
SCRYPT_DKLEN = 32  # 输出长度
SALT_BYTES = 16  # 每用户独立盐长度


def get_jwt_secret() -> str:
    """读取 JWT 密钥。

    入参：无（读 settings.auth.jwt_secret_env 指定的环境变量）。
    出参：密钥字符串。
    失败时的行为：未设置或过短时抛 RuntimeError，使进程启动失败（第 3 节硬约束）。
    """

    env_name = settings.auth.jwt_secret_env  # 环境变量名来自配置，不写死在代码
    secret = os.environ.get(env_name, "")  # 只从环境读取，不做硬编码兜底
    if len(secret) < 16:  # 过短密钥等同未设置
        raise RuntimeError(f"未设置 {env_name}（至少 16 字符），请从 .env.example 复制并填写")  # 启动即失败
    return secret  # 返回可用密钥


def hash_password(password: str) -> str:
    """生成口令哈希。

    入参：password 明文口令。
    出参：`salt_hex$hash_hex` 字符串。
    失败时的行为：入参为空时抛 ValueError（种子阶段即暴露）。
    """

    if not password:  # 空口令不允许入库
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(SALT_BYTES)  # 每用户独立随机盐
    digest = hashlib.scrypt(  # 标准库 scrypt，参数按提示词固定
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return f"{salt.hex()}${digest.hex()}"  # salt$hash 便于单列存储


def verify_password(password: str, stored: str) -> bool:
    """校验口令。

    入参：password 明文；stored 库中 `salt$hash`。
    出参：True/False。
    失败时的行为：格式非法时返回 False（与口令错误同表现，不泄漏内部结构）。
    """

    try:
        salt_hex, hash_hex = stored.split("$", 1)  # 拆分盐与哈希
        salt = bytes.fromhex(salt_hex)  # 还原盐
        expected = bytes.fromhex(hash_hex)  # 还原期望哈希
    except (ValueError, AttributeError):  # 缺分隔符或非字符串
        return False  # 统一按失败处理
    actual = hashlib.scrypt(  # 用相同参数重算
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_DKLEN
    )
    return hmac.compare_digest(actual, expected)  # 常量时间比较


def create_token(user: dict) -> str:
    """签发 JWT。

    入参：user 用户行（含 user_id/username/role/scope_type/scope_value）。
    出参：token 字符串。
    失败时的行为：密钥缺失时抛 RuntimeError（启动阶段已经拦截）。
    """

    now = datetime.now(timezone.utc)  # 统一使用 UTC 生成时间戳
    payload = {
        "sub": user["user_id"],  # 主体为用户 ID
        "username": user["username"],  # 便于日志阅读
        "role": user["role"],  # 角色冗余，权限判定仍以库为准
        "scope_type": user["scope_type"],  # 数据域类型
        "scope_value": user["scope_value"],  # 数据域取值
        "iat": int(now.timestamp()),  # 签发时间
        "exp": int((now + timedelta(minutes=settings.auth.token_ttl_minutes)).timestamp()),  # 过期时间
        "jti": secrets.token_hex(8),  # 唯一标识，便于追踪
    }
    return jwt.encode(payload, get_jwt_secret(), algorithm="HS256")  # HS256 对称签名


def decode_token(token: str) -> dict:
    """校验并解析 JWT。

    入参：token 字符串。
    出参：claims 字典。
    失败时的行为：过期/签名错误/格式错误统一抛 AppError(AUTH_INVALID, 401)。
    """

    try:
        return jwt.decode(token, get_jwt_secret(), algorithms=["HS256"])  # 只接受 HS256
    except jwt.ExpiredSignatureError as exc:  # 过期单独说明
        raise AppError(ErrorCode.AUTH_INVALID, "登录已过期，请重新登录", status=401) from exc
    except jwt.PyJWTError as exc:  # 其余非法 token
        raise AppError(ErrorCode.AUTH_INVALID, "token 无效", status=401) from exc
