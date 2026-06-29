"""ToolOrchestrator — 工具调用处理编排。

从 AgentCore 中提取的工具调用相关逻辑：
- 工具调用循环
- 媒体提取
- 钩子触发
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger


class ToolOrchestrator:
    """工具调用编排器。

    职责：
    1. 工具调用分发与执行
    2. 工具结果后处理（媒体提取、路径清理）
    3. 钩子触发
    """

    def __init__(self, core: Any) -> None:
        """初始化 ToolOrchestrator。

        Args:
            core: AgentCore 实例（门面引用）
        """
        self._core = core

    async def extract_media_from_tool_results(
        self, tool_results: list, reply: str
    ) -> tuple[list[Path], Path | None, str]:
        """从工具结果中提取图片/视频路径，并清理回复文本中的冗余路径描述。

        当前实现委托回 AgentCore._extract_media_from_tool_results，
        后续逐步将逻辑迁移到本方法中。
        """
        return await self._core._extract_media_from_tool_results(tool_results, reply)

    @staticmethod
    def extract_image_paths_from_data(data_str: str) -> list[str]:
        """从工具结果数据字符串中提取图片路径。"""
        paths = []
        for m in re.finditer(r'图片已保存到:\s*(\S+)', data_str):
            paths.append(m.group(1))
        for m in re.finditer(r'图片URL:\s*(\S+)', data_str):
            paths.append(m.group(1).rstrip('`'))
        return paths

    @staticmethod
    def extract_video_path_from_data(data_str: str) -> str | None:
        """从工具结果数据字符串中提取视频路径。"""
        m = re.search(r'本地路径:\s*(\S+\.mp4)', data_str)
        if m:
            return m.group(1)
        return None
