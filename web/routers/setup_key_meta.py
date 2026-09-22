"""向导 Key 元数据的降级定义（setup_wizard 导入失败时的兜底清单）。

拆分动机（巨型文件止血液轮）：这两份列表原内联在 web/routers/setup.py，
纯静态数据、无逻辑，抽到独立模块后 setup.py 不再因新增 Key 而增长。

与 setup_wizard.REQUIRED_KEYS / OPTIONAL_KEYS 保持同构（key/label/desc/
url/url_desc）；setup.py 优先用 setup_wizard 的真身，仅在导入失败时回退本模块。
新增 Key 时两处都要同步（探针见 setup_key_probes）。
"""
from __future__ import annotations

from typing import Any

_FALLBACK_REQUIRED_KEYS: tuple[str, ...] = (
    "MIMO_API_KEY",
    "QQBOT_APP_ID",
    "QQBOT_APP_SECRET",
    "SILICONFLOW_API_KEY",
)

_FALLBACK_REQUIRED_KEYS_META: list[dict[str, Any]] = [
    {"key": "MIMO_API_KEY", "label": "MiMo API 密钥", "desc": "小米 MiMo 大模型 API 密钥", "url": "https://platform.xiaomimimo.com?ref=SU5WDZ", "url_desc": "注册 → 控制台 → API Keys"},
    {"key": "QQBOT_APP_ID", "label": "QQ Bot App ID", "desc": "QQ 机器人应用 ID", "url": "https://q.qq.com", "url_desc": "创建机器人应用 → 获取 AppID"},
    {"key": "QQBOT_APP_SECRET", "label": "QQ Bot App Secret", "desc": "QQ 机器人应用密钥", "url": "https://q.qq.com", "url_desc": "同一页面的 AppSecret"},
    {"key": "SILICONFLOW_API_KEY", "label": "SiliconFlow API 密钥", "desc": "硅基流动 API 密钥", "url": "https://cloud.siliconflow.cn/i/iM5RmeWc", "url_desc": "注册 → API Keys"},
]

_FALLBACK_OPTIONAL_KEYS_META: list[dict[str, Any]] = [
    {"key": "WEBUI_PASSWORD", "label": "Web UI 密码", "desc": "留空则无需密码登录", "url": "", "url_desc": ""},
    {"key": "TAVILY_API_KEY", "label": "Tavily 搜索 API 密钥", "desc": "AI 搜索引擎", "url": "https://tavily.com", "url_desc": "注册 → API Keys"},
    {"key": "ANYSEARCH_API_KEY", "label": "AnySearch 统一搜索密钥", "desc": "统一搜索基础设施（选填，搜索首选引擎，失败自动回退）", "url": "https://www.coze.cn/s/qBK5eb8QVoE/", "url_desc": "使用手册（含 Key 获取方式）"},
    {"key": "DEEPSEEK_API_KEY", "label": "DeepSeek API 密钥", "desc": "DeepSeek 大模型 API 密钥", "url": "https://platform.deepseek.com", "url_desc": "注册 → API Keys"},
    {"key": "OPENROUTER_API_KEY", "label": "OpenRouter API 密钥", "desc": "OpenRouter API 密钥", "url": "https://openrouter.ai", "url_desc": "注册 → API Keys"},
    {"key": "WOLFRAMALPHA_API_KEY", "label": "WolframAlpha 知识计算密钥", "desc": "知识计算引擎", "url": "https://products.wolframalpha.com/api/", "url_desc": "注册 → Get AppID"},
    {"key": "AGNES_API_KEY", "label": "Agnes AI 图像/视频密钥", "desc": "图片生成和视频生成的核心依赖", "url": "https://agnes-ai.cn", "url_desc": "注册 → API Keys"},
    {"key": "JEV_API_KEY", "label": "Jev 决策模型密钥", "desc": "TypeSafe System One 决策模型（结构化判断，不生成文字）；选填，配置后在功能节点开启", "url": "https://console.typesafe.ai/settings/keys", "url_desc": "控制台 → Settings → Keys（形如 jev_…）"},
    {"key": "GITHUB_PERSONAL_ACCESS_TOKEN", "label": "GitHub 个人访问令牌", "desc": "GitHub MCP Server 所需", "url": "https://github.com/settings/tokens", "url_desc": "Generate new token"},
    {"key": "MODELSCOPE_ACCESS_TOKEN", "label": "魔搭 Access Token", "desc": "魔搭 ModelScope 免费模型发现", "url": "https://modelscope.cn", "url_desc": "注册 → 个人中心 → 访问令牌"},
]

__all__ = [
    "_FALLBACK_OPTIONAL_KEYS_META",
    "_FALLBACK_REQUIRED_KEYS",
    "_FALLBACK_REQUIRED_KEYS_META",
]
