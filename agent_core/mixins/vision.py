"""VisionMixin —— Phase 6 拆分自 message_processor.py。

包含图片描述逻辑：_describe_images（Vision API 识别图片）与
_xiaoda_synthesis_chat（小妲结果合成汇报），及 VISION_FAILURE_PATTERNS
失败模式常量（P0 Task 1.7）。

叶子模块依赖约定：本 mixin 只允许依赖 agent_core._shared 及 config/core 叶子模块，
不得 import agent_core.message_processor（避免循环导入）。
"""
from __future__ import annotations

import openai as _openai_mod  # P0 Task 1.8：用于捕获 BadRequestError/APIError
from loguru import logger


class VisionMixin:
    """图片描述与结果合成相关方法的 Mixin，由 MessageProcessorMixin 组合使用。"""

    # P0 修复（Task 1.7）：MiMo Vision API 已知失败模式
    # 当模型返回这些字符串时，说明图片识别失败，不应作为合法 description 透传
    VISION_FAILURE_PATTERNS = (
        "cannot read image", "unable to read", "i cannot read",
        "image not readable", "can't read", "无法识别",
        "图片无法识别", "图片读取失败", "无法读取图片",
    )

    async def _describe_images(self, image_data: list[dict]) -> str:
        """使用 Vision API 识别图片内容。

        P0 修复（用户要求"主chatLLM是谁图片发给谁，不要硬编mimo"）：
        - 移除硬编码 `provider="mimo"` 和 `model=MIMO_MODEL`
        - 改用 `router.get_vision_provider_and_model()` 动态选择：
          优先用当前主 chat LLM（若 supports_vision），否则从 provider_metadata.json
          找 vision-capable provider，最后兜底环境变量。
        - 保留 Task 1.6（安全客户端路径）、1.7（失败模式校验）、1.8（BadRequestError 捕获）
        """
        try:
            # Task 1.6：走安全客户端路径（含锁 + 懒注册 + LLMError）
            if not self.router:
                logger.warning("agent.vision_no_router")
                return ""
            # P0 修复：动态选择 vision provider + model（不再硬编码 mimo）
            _vision_provider, _vision_model = self.router.get_vision_provider_and_model()
            if not _vision_provider or not _vision_model:
                logger.warning("agent.vision_no_capable_provider",
                               hint="主 chat LLM 不支持 vision 且元数据无 vision-capable provider")
                return ""
            try:
                client = await self.router._select_client_for_provider(_vision_provider)
            except Exception as ce:
                logger.warning("agent.vision_client_unavailable",
                               provider=_vision_provider,
                               error=f"{type(ce).__name__}: {ce}"[:200])
                return ""
            logger.info("agent.vision_client_acquired",
                        provider=_vision_provider, model=_vision_model)

            vision_parts = [{"type": "text", "text": "请详细描述这张图片的内容。如果有文字，请完整转录。如果是题目，请给出题目内容。"}]
            for i, img in enumerate(image_data):
                b64_data = img.get('data', '')
                mime = img.get('mimeType', 'image/jpeg')
                logger.info("agent.vision_image", index=i, mime=mime, b64_len=len(b64_data))
                if not b64_data:
                    logger.warning("agent.vision_empty_data", index=i)
                    continue
                vision_parts.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{mime};base64,{b64_data}"
                    }
                })

            if len(vision_parts) <= 1:
                logger.warning("agent.vision_no_valid_images")
                return ""

            # Task 1.8：优先捕获 BadRequestError，记录具体错误码
            try:
                response = await client.chat.completions.create(
                    model=_vision_model,
                    messages=[{"role": "user", "content": vision_parts}],
                    max_tokens=1024,
                )
            except _openai_mod.BadRequestError as be:
                # vision API 的 BadRequestError 通常意味着图片格式/大小问题
                _status = getattr(be, "response", None)
                _status_code = _status.status_code if _status is not None else None
                logger.warning("agent.vision_bad_request",
                               provider=_vision_provider, model=_vision_model,
                               status_code=_status_code,
                               body=str(getattr(be, "body", ""))[:200],
                               error=f"{type(be).__name__}: {be}"[:200])
                return ""
            except _openai_mod.APIError as ae:
                logger.warning("agent.vision_api_error",
                               provider=_vision_provider, model=_vision_model,
                               error=f"{type(ae).__name__}: {ae}"[:200])
                return ""

            description = (response.choices[0].message.content or "").strip()
            logger.info("agent.image_described", length=len(description),
                        provider=_vision_provider, model=_vision_model,
                        preview=description[:80])

            # Task 1.7：校验响应内容，识别已知失败模式
            # 根因：Vision API 可能把 "cannot read image" 作为 content 返回，
            #       原实现不校验直接透传到 system message，导致主聊天 LLM 据此回答"看不清图片"
            if not description or len(description) < 10:
                logger.warning("agent.vision_suspicious_response",
                               reason="too_short", content_preview=description[:100])
                return ""
            _desc_lower = description.lower()
            for pattern in self.VISION_FAILURE_PATTERNS:
                if pattern in _desc_lower:
                    logger.warning("agent.vision_suspicious_response",
                                   reason="failure_pattern_matched",
                                   pattern=pattern,
                                   content_preview=description[:100])
                    return ""  # 走兜底分支

            return description
        except Exception as e:
            logger.warning("agent.image_describe_failed",
                           error=str(e), error_type=type(e).__name__)
            return ""

    async def _xiaoda_synthesis_chat(self, prompt: str) -> str:
        try:
            result = await self.router.route(
                "chat",
                [
                    {"role": "system", "content": """你是小妲，团队的核心助手。你的任务是整理团队成员的工作结果，向用户汇报。

重要规则：
1. 必须输出具体的事实信息和关键要点，不要只说空洞的比喻或感想
2. 如果搜索到了新闻/资料，必须列出具体的标题、摘要和关键数据
3. 如果是代码/技术结果，列出核心代码和结论
4. 用简洁清晰的语言组织，可以带一点你的风格但内容必须充实
5. 不要编造信息，只基于提供的内容整理
6. 格式：先一句话总结，然后分点列出具体信息"""},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=3072,
                temperature=0.5,
            )
            if isinstance(result, str):
                return result.strip()
            return result.choices[0].message.content.strip()
        except Exception as e:
            logger.warning("agent.xiaoda_synthesis_failed", error=str(e))
            return prompt
