"""共享工具函数"""

import hashlib
import os

# WebUI 默认监听端口。所有模块（agent.py / doctor / watchdog / cli_client /
# system 路由）统一引用此常量，避免 "8082" 魔法数字散落多处导致端口不一致。
DEFAULT_WEBUI_PORT = 8082

# 旧版随包分发的 .env 默认值 → 当前默认值。first-run 会把 .env.example 复制进
# 用户目录（frozen: ~/.ai-agent/.env），之后升级安装不覆盖用户数据——旧默认值
# 从此固化并毒化端口解析（agent.py --port 默认读 WEBUI_PORT env）。字段机
# 0.5.80 实测：.env 遗留 WEBUI_PORT=8080，看门狗以 --port 8080 拉起主进程。
_LEGACY_ENV_DEFAULTS = {
    "WEBUI_PORT": ("8080", str(DEFAULT_WEBUI_PORT)),
}

# 条件迁移：仅当 guard 环境变量为空时才改写。老 .env.example 固化的
# WEBUI_HOST=0.0.0.0 + 无密码 = VULN-11 fail-closed 下谁也登不进来（连本机
# 127.0.0.1 都 403，前端却强制要求输密码——死结），改绑回环零损失；
# 已设密码的用户可能刻意开放局域网访问，不得动他们的 WEBUI_HOST。
_CONDITIONAL_ENV_DEFAULTS = {
    "WEBUI_HOST": ("0.0.0.0", "127.0.0.1", "WEBUI_PASSWORD"),
}


def migrate_legacy_env_defaults(env_path) -> list[str]:
    """把用户 .env 中与旧 shipped 默认相等的键原位迁移为当前默认值。

    只动"值恰好等于旧默认"的行——用户显式自定义过的值（如 9090）不受影响，
    注释行不碰。条件迁移（_CONDITIONAL_ENV_DEFAULTS）额外要求 guard 环境变量
    为空才生效。返回被改写的键列表；文件缺失/不可写时返回空列表。
    """
    try:
        with open(env_path, "r", encoding="utf-8") as fp:
            lines = fp.readlines()
    except OSError:
        return []
    rules: dict[str, tuple[str, str]] = {}
    for key, (legacy, current) in _LEGACY_ENV_DEFAULTS.items():
        rules[key] = (legacy, current)
    for key, (legacy, current, guard) in _CONDITIONAL_ENV_DEFAULTS.items():
        if not os.environ.get(guard, "").strip():
            rules[key] = (legacy, current)
    changed: list[str] = []
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, sep, value = stripped.partition("=")
        if not sep:
            continue
        rule = rules.get(key.strip())
        if rule is not None and value.strip() == rule[0]:
            lines[i] = f"{key.strip()}={rule[1]}\n"
            if key.strip() not in changed:
                changed.append(key.strip())
    if not changed:
        return []
    try:
        with open(env_path, "w", encoding="utf-8") as fp:
            fp.writelines(lines)
    except OSError:
        return []
    return changed

# LLM 响应 max_tokens 的默认值（生成 token 上限）。transport 层与 model_router /
# message_processor 统一引用，避免 "4096" 魔法数字散落多处。
DEFAULT_MAX_TOKENS = 4096


def safe_int(val, default):
    """安全解析整数值，非法值回退到 default."""
    if val is None:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def safe_float(val, default=0.0):
    """安全解析浮点数，None / 非法值回退到 default."""
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def mask_api_key(key: str) -> str:
    """返回 API key 的 sha256 前 8 位，用于日志标识而不泄漏真实 key 片段。

    同一 key 哈希稳定、不同 key 哈希不同、无法逆推原始 key。
    """
    if not key or len(key) < 8:
        return "***"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
