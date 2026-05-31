import time
from collections import defaultdict
from loguru import logger


class ToolCallRepair:

    def __init__(self, storm_threshold: int = 5, storm_window: float = 10.0):
        self._storm_threshold = storm_threshold
        self._storm_window = storm_window
        self._call_history: dict[str, list[float]] = defaultdict(list)

    def detect_storm(self, tool_name: str, args_str: str) -> bool:
        now = time.time()
        history = self._call_history[tool_name]
        history.append(now)
        self._call_history[tool_name] = [t for t in history if now - t < self._storm_window]
        if len(self._call_history[tool_name]) > self._storm_threshold:
            logger.warning("tool_repair.storm_detected", tool=tool_name, count=len(self._call_history[tool_name]))
            return True
        return False

    def repair_truncation(self, args_str: str) -> str | None:
        if not args_str:
            return None
        args_str = args_str.strip()
        if args_str.endswith('}'):
            return None
        last_brace = args_str.rfind('}')
        if last_brace > 0:
            repaired = args_str[:last_brace + 1]
            try:
                import json
                json.loads(repaired)
                logger.info("tool_repair.truncation_repaired")
                return repaired
            except (json.JSONDecodeError, ValueError):
                pass
        return None
