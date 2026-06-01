import json
import re
from loguru import logger
from text_utils import has_dsml_tool_calls, parse_dsml_tool_calls as _parse_dsml


class ToolCallRepair:

    def __init__(self, allowed_tool_names: set[str] | None = None, storm_window: int = 3):
        self._allowed_tools = allowed_tool_names or set()
        self._storm_window = storm_window
        self._recent_calls: list[tuple[str, str]] = []

    def _parse_dsml_tool_calls(self, text: str) -> list[dict]:
        return _parse_dsml(text, self._allowed_tools)

    def scavenge(self, reasoning_content: str | None, tool_calls: list | None) -> list:
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
        call_key = (tool_name, arguments)
        is_storm = call_key in self._recent_calls[-self._storm_window:]
        self._recent_calls.append(call_key)

        if len(self._recent_calls) > self._storm_window * 2:
            self._recent_calls = self._recent_calls[-self._storm_window * 2:]

        if is_storm:
            logger.warning("tool.repair.storm_detected",
                           tool=tool_name,
                           args_preview=arguments[:80])

        return is_storm

    def clear_storm_window(self):
        self._recent_calls.clear()
