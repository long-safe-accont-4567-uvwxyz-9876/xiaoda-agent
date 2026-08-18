from typing import Any
import json
import os
import re
import httpx
from loguru import logger

from agent_context import estimate_tokens
from utils.http_pool import get_shared_client


def _strip_markdown(text: str) -> str:
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text)
    text = re.sub(r'\*([^*\n]+)\*', r'\1', text)
    text = re.sub(r'^\s*[*\-•]\s+', '', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*#{1,6}\s*', '', text, flags=re.MULTILINE)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()

RESULT_CAP_TOKENS = 800

FAILURE_TEMPLATES = {
    "timeout": "那边有点慢呢……等会儿再试试好不好？",
    "not_found": "人家找了一圈，好像没有找到呢……",
    "default": "出了一点小问题……等会儿再试试好不好？",
}


class ResultWrapper:
    """结果包装器，将工具输出转写为用户友好的回复。"""

    def __init__(self, router: Any | None=None) -> None:
        self.router = router
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = "https://api.siliconflow.cn/v1"
        self._free_model = "THUDM/GLM-4-9B-0414"  # 非思考模型，避免 Z1 思考碎片

    async def _call_free_model(self, messages: list, temperature: float = 0.3,
                                max_tokens: int = 500) -> str | None:
        """调用硅基流动免费模型"""
        if not self._free_api_key:
            return None
        try:
            # G4: 共享 httpx.AsyncClient（连接池复用 + HTTP/2），单次请求级别覆盖 timeout
            client = get_shared_client()
            response = await client.post(
                f"{self._free_base_url}/chat/completions",
                json={
                    "model": self._free_model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                },
                headers={
                    "Authorization": f"Bearer {self._free_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=httpx.Timeout(15.0),
            )
            response.raise_for_status()
            data = response.json()
            return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("result_wrapper.free_model_failed", error=str(e))
            return None

    async def wrap(self, tool_name: str, result: Any, user_context: str = "") -> str:
        from tool_engine.tool_registry import ToolResult

        if isinstance(result, ToolResult):
            if not result.success:
                return self._failure_text(result.error)
            data = result.data
        else:
            data = result

        if isinstance(data, str):
            if len(data) > 400:
                data = data[:400] + "\n…"
            data = _strip_markdown(data)
            return f"人家帮你查了一下～\n\n{data}\n\n——就是这样 ~♪"

        data_str = json.dumps(data, ensure_ascii=False, indent=2) if not isinstance(data, str) else data
        if len(data_str) > 400:
            data_str = data_str[:400] + "\n…"
        data_str = _strip_markdown(data_str)

        return f"人家帮你查了一下～\n\n{data_str}\n\n——就是这样 ~♪"

    async def compact_result(self, tool_name: str, result_text: str,
                              user_context: str = "") -> str:
        original_tokens = estimate_tokens(result_text)
        if original_tokens <= RESULT_CAP_TOKENS:
            return result_text

        if not self.router:
            return result_text[:int(len(result_text) * RESULT_CAP_TOKENS / original_tokens)]

        try:
            prompt = f"""请将以下工具结果压缩为简洁摘要，保留最关键的信息点。
原始结果来自工具「{tool_name}」，用户当时的问题是：{user_context[:200]}

要求：
- 保留所有关键数据和事实
- 去除冗余和格式化内容
- 压缩到原来的 1/3 长度以内
- 用纯文本格式

原始结果：
{result_text[:3000]}

压缩摘要："""

            # 优先使用免费模型，降级到主路由
            compacted = await self._call_free_model(
                [{"role": "user", "content": prompt}],
                temperature=0.3, max_tokens=1024,
            )
            if compacted is None and self.router:
                compacted = await self.router.route(
                    "tool_result_wrap",
                    [{"role": "user", "content": prompt}],
                    temperature=0.3,
                    max_tokens=1024,
                )

            if compacted and len(compacted) > 20:
                return compacted

            return result_text[:int(len(result_text) * RESULT_CAP_TOKENS / original_tokens)]
        except Exception as e:
            logger.warning("tool.compaction_failed", tool=tool_name, error=str(e)[:120])
            return result_text[:int(len(result_text) * RESULT_CAP_TOKENS / original_tokens)]

    def _failure_text(self, error: str) -> str:
        if "timeout" in error.lower():
            return FAILURE_TEMPLATES["timeout"]
        if "not found" in error.lower() or "没找到" in error:
            return FAILURE_TEMPLATES["not_found"]
        if any(kw in error for kw in ("人家", "呢", "好不好", "……", "♪")):
            return error
        return FAILURE_TEMPLATES["default"]
