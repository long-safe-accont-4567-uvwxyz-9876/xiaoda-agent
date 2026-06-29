import json
import re
import time
from loguru import logger
from utils.text_utils import has_dsml_tool_calls, parse_dsml_tool_calls as _parse_dsml


class ToolCallRepair:

    STORM_TTL = 120

    def __init__(self, allowed_tool_names: set[str] | None = None, storm_window: int = 3) -> None:
        """初始化工具调用修复器.

        Args:
            allowed_tool_names: 允许的工具名集合, None 表示空集
            storm_window: 风暴检测窗口大小, 默认 3
        """
        self._allowed_tools = allowed_tool_names or set()
        self._storm_window = storm_window
        self._recent_calls: list[tuple[str, str, float]] = []

    def _parse_dsml_tool_calls(self, text: str) -> list[dict]:
        return _parse_dsml(text, self._allowed_tools)

    def scavenge(self, reasoning_content: str | None, tool_calls: list | None) -> list:
        """从推理内容中拾取被遗漏的工具调用.

        Args:
            reasoning_content: 推理/思考内容文本
            tool_calls: 已有的工具调用列表

        Returns:
            解析出的工具调用列表 (优先返回原 tool_calls)
        """
        if not reasoning_content:
            return tool_calls or []

        if tool_calls:
            return tool_calls

        scavenged = []

        if has_dsml_tool_calls(reasoning_content):
            dsml_calls = _parse_dsml(reasoning_content, self._allowed_tools)
            if dsml_calls:
                scavenged.extend(dsml_calls)
                logger.info("tool.repair.scavenge_dsml", count=len(dsml_calls))

        json_pattern = r'\{[^{}]*"name"\s*:\s*"(\w+)"[^{}]*"arguments"\s*:\s*(\{[^}]*\})[^{}]*\}'
        for match in re.finditer(json_pattern, reasoning_content, re.DOTALL):
            tool_name = match.group(1)
            args_str = match.group(2)
            if self._allowed_tools and tool_name not in self._allowed_tools:
                continue
            if any(s["function"]["name"] == tool_name for s in scavenged):
                continue
            try:
                args = json.loads(args_str)
                scavenged.append({
                    "id": f"scavenged_{len(scavenged)}",
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    }
                })
            except json.JSONDecodeError:
                continue

        if scavenged:
            logger.info("tool.repair.scavenged_count", count=len(scavenged))

        return scavenged

    def repair_truncation(self, arguments_str: str) -> str | None:
        """修复被截断的工具调用 JSON 参数.

        Args:
            arguments_str: 工具调用的 JSON 参数字符串

        Returns:
            修复后的合法 JSON 字符串, 无法修复返回 None
        """
        if not arguments_str:
            return None

        try:
            json.loads(arguments_str)
            return arguments_str
        except json.JSONDecodeError:
            pass

        repaired = arguments_str.rstrip()
        repaired = re.sub(r',\s*"[^"]*"\s*:\s*$', '', repaired)

        open_braces = repaired.count('{') - repaired.count('}')
        open_brackets = repaired.count('[') - repaired.count(']')

        if open_braces > 0:
            repaired += '}' * open_braces
        if open_brackets > 0:
            repaired += ']' * open_brackets

        try:
            json.loads(repaired)
            logger.info("tool.repair.truncation_fixed",
                        original_len=len(arguments_str),
                        repaired_len=len(repaired))
            return repaired
        except json.JSONDecodeError:
            logger.warning("tool.repair.truncation_failed",
                           arguments_preview=arguments_str[:100])
            return None

    def detect_storm(self, tool_name: str, arguments: str) -> bool:
        """检测工具调用风暴 (短时间内重复调用相同工具+参数).

        Args:
            tool_name: 工具名
            arguments: JSON 参数字符串

        Returns:
            True 表示检测到风暴
        """
        now = time.time()
        cutoff = now - self.STORM_TTL
        self._recent_calls = [(n, a, t) for n, a, t in self._recent_calls if t > cutoff]

        # 规范化 JSON 参数，避免键顺序不同导致漏检
        normalized_args = self._normalize_json(arguments)
        call_key = (tool_name, normalized_args)
        recent_keys = [(n, a) for n, a, t in self._recent_calls[-self._storm_window:]]
        is_storm = call_key in recent_keys
        self._recent_calls.append((tool_name, normalized_args, now))

        if len(self._recent_calls) > self._storm_window * 2:
            self._recent_calls = self._recent_calls[-self._storm_window * 2:]

        if is_storm:
            logger.warning("tool.repair.storm_detected",
                           tool=tool_name,
                           args_preview=arguments[:80])

        return is_storm

    def clear_storm_window(self) -> None:
        """清空风暴检测的近期调用记录."""
        self._recent_calls.clear()

    @staticmethod
    def _normalize_json(arguments: str) -> str:
        """规范化 JSON 字符串，统一键顺序以便比较"""
        try:
            parsed = json.loads(arguments)
            return json.dumps(parsed, sort_keys=True, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError):
            return arguments
