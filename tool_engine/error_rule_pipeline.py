"""P5 失败经验→规则闭环。

工具调用失败后，自动调用硅基流动免费模型提取一条可复用预防规则，
写入 tool_error_rules 表。下次同类工具调用前可查询规则并按
ERROR_RULE_STRICT_MODE 拒绝或仅警告。

向后兼容：ErrorRulePipeline 为可选组件，未配置 db/router 时不影响主流程。
失败安全：所有异常都被捕获并记录日志，绝不抛出到主流程。
"""

from __future__ import annotations
from typing import Any, ClassVar

import json
import os
import re
import time

import httpx
from loguru import logger


# 提取 prompt — 让 LLM 从一次失败中提取一条最关键的预防规则
EXTRACT_PROMPT = """你是错误分析助手。分析以下工具调用失败，提取一条可复用的预防规则。

工具名：{tool_name}
参数：{args}
错误：{error}

输出格式（一行）：
规则文本 | 匹配特征

示例：
URL 必须以 http 或 https 开头 | url_not_http
文件路径不能包含空格 | path_has_space
参数 query 不能为空 | query_empty

只输出一条最关键的规则，如果没有可提取的规则则输出空行："""


# 用于拆分 pattern 为关键词 token 的分隔符
_PATTERN_SPLIT_RE = re.compile(r"[_\-\s:;,.]+")

# 停用词：这些 token 出现在几乎所有 pattern 中，无区分度，过滤掉以避免误匹配
_PATTERN_STOPWORDS = frozenset({
    "not", "has", "have", "the", "and", "or", "for", "with", "empty",
    "is", "are", "was", "were", "be", "to", "of", "in", "on", "at",
})


def _pattern_to_tokens(pattern: str) -> list[str]:
    """把 pattern（如 url_not_http）拆分为关键词 token（如 ['url', 'http']）。

    过滤停用词（not/has/the/and/or/for/with/empty 等）和长度 < 3 的 token，
    只保留有区分度的关键词。过滤后为空则返回空列表（不匹配）。
    """
    if not pattern:
        return []
    tokens = _PATTERN_SPLIT_RE.split(pattern.lower())
    return [t for t in tokens if len(t) >= 3 and t not in _PATTERN_STOPWORDS]


class ErrorRulePipeline:
    """失败经验 → 规则 提取与查询管线。

    依赖 DatabaseManager + ModelRouter，二者均可为 None（降级为 no-op）。
    """

    # 内存级时间窗节流：记录每个 tool_name 的上次提取时间戳（类变量，跨实例共享）
    _last_extract_time: ClassVar[dict[str, float]] = {}
    _EXTRACT_THROTTLE_SECONDS = 60  # 同一 tool_name 在 60 秒内只允许一次提取
    _MAX_RULES_PER_TOOL_24H = 5     # 24h 内同 tool_name 规则数上限，超过则跳过 LLM 提取

    def __init__(self, db: Any, router: Any) -> None:
        self.db = db
        self.router = router
        self._available = db is not None
        self._free_api_key = os.getenv("SILICONFLOW_API_KEY", "") or os.getenv("EMBED_API_KEY", "")
        self._free_base_url = "https://api.siliconflow.cn/v1"
        self._free_model = "THUDM/GLM-Z1-9B-0414"

    def set_free_model_client(self, api_key: str, base_url: str, model: str) -> None:
        """配置硅基流动免费模型客户端（与 InstinctManager 接口一致）"""
        self._free_api_key = api_key
        self._free_base_url = base_url
        self._free_model = model

    async def _call_free_model(self, messages: list, temperature: float = 0.3,
                                max_tokens: int = 200) -> str | None:
        """调用硅基流动免费模型。失败返回 None。"""
        if not self._free_api_key:
            return None
        try:
            async with httpx.AsyncClient(timeout=15) as client:
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
                )
                response.raise_for_status()
                data = response.json()
                return data.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning("error_rule.free_model_failed", error=str(e))
            return None

    async def extract_rule(self, tool_name: str, args: dict, error: str) -> dict | None:
        """从一次工具失败中提取一条规则并写入数据库。

        返回规则 dict 或 None（未提取到 / 去重命中 / 节流命中 / 失败）。
        永不抛异常。
        """
        if not self._available:
            return None
        if not error or len(error.strip()) < 2:
            return None
        try:
            now = time.time()

            # 节流 + 粗粒度去重检查
            cutoff = await self._check_throttle_and_dedup(tool_name, now)
            if cutoff is None:
                return None

            # 调用 LLM 提取规则
            result = await self._call_extract_llm(tool_name, args, error, now)
            if not result or not isinstance(result, str):
                return None

            # 解析「规则文本 | 匹配特征」格式
            line = self._parse_rule_line(result)
            if not line:
                return None
            rule_text, pattern = line
            if not rule_text or not pattern:
                return None

            # 24 小时内同 tool_name + pattern 精确去重
            cursor = await self.db._conn.execute(
                "SELECT id FROM tool_error_rules WHERE tool_name=? AND pattern=? AND created_at >= ?",
                (tool_name, pattern, cutoff),
            )
            existing = await cursor.fetchone()
            if existing:
                logger.debug("error_rule.dedup_skipped",
                             tool_name=tool_name, pattern=pattern)
                return None

            # 写入数据库
            return await self._save_rule(tool_name, pattern, rule_text, now)
        except Exception as e:
            logger.warning("error_rule.extract_failed", error=str(e))
            return None

    async def _check_throttle_and_dedup(self, tool_name: str, now: float) -> Any:
        """节流 + 粗粒度去重检查。返回 cutoff 时间戳或 None（应跳过）。"""
        # 内存级时间窗节流：同一 tool_name 在 60 秒内只允许一次提取
        last_time = self._last_extract_time.get(tool_name, 0.0)
        elapsed = now - last_time
        if elapsed < self._EXTRACT_THROTTLE_SECONDS:
            logger.debug("error_rule.throttle_skipped",
                         tool_name=tool_name, elapsed=round(elapsed, 1))
            return None

        # 粗粒度去重：最近 24h 内同 tool_name 规则数 >= 5 则跳过
        cutoff = now - 86400
        cursor = await self.db._conn.execute(
            "SELECT COUNT(*) FROM tool_error_rules WHERE tool_name=? AND created_at >= ?",
            (tool_name, cutoff),
        )
        row = await cursor.fetchone()
        rule_count = row[0] if row else 0
        if rule_count >= self._MAX_RULES_PER_TOOL_24H:
            logger.debug("error_rule.too_many_rules_skipped",
                         tool_name=tool_name, count=rule_count)
            return None
        return cutoff

    async def _call_extract_llm(self, tool_name: str, args: dict, error: str, now: float) -> Any:
        """调用 LLM 提取规则文本。返回结果字符串或 None。"""
        args_str = json.dumps(args, ensure_ascii=False)[:500]
        prompt = EXTRACT_PROMPT.format(tool_name=tool_name, args=args_str, error=error[:500])
        messages = [{"role": "user", "content": prompt}]

        # 即将调用 LLM，记录节流时间点（即使后续失败也节流，防止重试风暴）
        self._last_extract_time[tool_name] = now

        # 优先硅基流动免费模型，失败降级到 router
        result = await self._call_free_model(messages, temperature=0.3, max_tokens=200)
        if result is None and self.router is not None:
            try:
                result = await self.router.route(
                    task_type="chat_mini", messages=messages,
                    temperature=0.3, max_tokens=200,
                )
            except Exception as e:
                logger.warning("error_rule.extract_llm_failed", error=str(e))
                return None
        return result

    async def _save_rule(self, tool_name: str, pattern: str, rule_text: str, now: float) -> dict:
        """将提取到的规则写入数据库。返回规则 dict。"""
        cursor = await self.db._conn.execute(
            """INSERT INTO tool_error_rules
               (tool_name, pattern, rule_text, created_at, hit_count)
               VALUES (?, ?, ?, ?, 0)""",
            (tool_name, pattern, rule_text, now),
        )
        await self.db._conn.commit()
        rule_id = cursor.lastrowid
        logger.info("error_rule.extracted",
                    tool_name=tool_name, pattern=pattern,
                    rule_text=rule_text[:60], rule_id=rule_id)
        return {
            "id": rule_id,
            "tool_name": tool_name,
            "pattern": pattern,
            "rule_text": rule_text,
            "created_at": now,
            "hit_count": 0,
        }

    @staticmethod
    def _parse_rule_line(result: str) -> tuple[str, str] | None:
        """从 LLM 输出中解析「规则文本 | 匹配特征」一行。"""
        text = result.strip()
        if not text:
            return None
        # 取首个非空、含 | 的行
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith(("<think>", "</think>")):
                continue
            if "|" not in line:
                continue
            parts = line.rsplit("|", 1)
            if len(parts) != 2:
                continue
            rule_text = parts[0].strip().lstrip("-").strip()
            pattern = parts[1].strip()
            if rule_text and pattern:
                return rule_text, pattern
        return None

    async def check_rules(self, tool_name: str, args: dict) -> list[dict]:
        """查询该工具匹配的失败规则。

        匹配策略：
        1. 按 tool_name 查询所有规则（按 hit_count 降序、created_at 降序）
        2. 对每条规则，把 pattern 拆为 token（过滤停用词、长度 < 3 的 token），
           要求所有 token 都出现在参数 JSON 中（AND 逻辑）
        3. pattern 完全为空、过滤后无 token 或匹配失败时不返回（避免误伤）

        返回匹配的规则列表。永不抛异常。
        """
        if not self._available:
            return []
        try:
            cursor = await self.db._conn.execute(
                """SELECT id, tool_name, pattern, rule_text, hit_count, created_at
                   FROM tool_error_rules
                   WHERE tool_name=?
                   ORDER BY hit_count DESC, created_at DESC
                   LIMIT 10""",
                (tool_name,),
            )
            rows = await cursor.fetchall()
            if not rows:
                return []

            args_str = json.dumps(args, ensure_ascii=False).lower()
            matched: list[dict] = []
            for row in rows:
                rule = dict(row)
                pattern = rule.get("pattern", "") or ""
                tokens = _pattern_to_tokens(pattern)
                # 没有 token 的规则无法匹配，跳过
                if not tokens:
                    continue
                # 所有 token 都出现在参数 JSON 中才视为匹配（AND 逻辑，避免误伤）
                if all(tok in args_str for tok in tokens):
                    matched.append(rule)
            return matched
        except Exception as e:
            logger.warning("error_rule.query_failed", error=str(e))
            return []

    async def increment_hit_count(self, rule_id: int) -> None:
        """累计命中次数。永不抛异常。"""
        if not self._available:
            return
        try:
            await self.db._conn.execute(
                "UPDATE tool_error_rules SET hit_count = hit_count + 1 WHERE id = ?",
                (rule_id,),
            )
            await self.db._conn.commit()
        except Exception as e:
            logger.warning("error_rule.increment_failed",
                           rule_id=rule_id, error=str(e))