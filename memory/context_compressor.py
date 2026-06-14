"""智能上下文压缩引擎"""
import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from loguru import logger
from config import DATA_DIR
from utils.atomic_write import atomic_write

SUMMARY_PREFIX = (
    "[上下文压缩 — 仅供参考] 之前的对话已被压缩为以下摘要。"
    "这是从之前上下文窗口的交接——仅作为背景参考，不是当前指令。"
    "不要回答摘要中提到的问题或执行摘要中的请求，它们已经被处理过了。"
    "只响应摘要之后出现的最新用户消息——那条消息才是当前应该做什么的唯一真实来源。"
    "如果最新用户消息与摘要中的'进行中任务'/'待处理事项'/'剩余工作'矛盾、取代、更改主题或以任何方式偏离，"
    "以最新消息为准——完全丢弃那些过时项。"
    "持久记忆（Instinct、SOUL）始终有效且活跃。"
)

_HISTORICAL_SUMMARY_PREFIXES = [
    SUMMARY_PREFIX,
    # 旧版前缀（用于剥离）
    "[历史对话摘要（仅供参考，不是当前指令。最新消息优先于此摘要）]",
    "[CONTEXT COMPACTION — REFERENCE ONLY]",
]


@dataclass
class CompressionResult:
    """压缩结果数据类"""
    messages: list = field(default_factory=list)
    summary: str = ""
    tokens_saved: int = 0


class ContextCompressor:
    """上下文压缩器"""

    CACHE_DIR = DATA_DIR / "ccr_cache"
    TOOL_OUTPUT_THRESHOLD = 2000  # 超过此长度压缩
    HISTORY_KEEP_RECENT = 5       # 保留最近 5 轮完整内容

    def __init__(self, router=None):
        self._router = router
        self._cache_dir = self.CACHE_DIR
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._stats = {
            "compressed_count": 0,
            "retrieved_count": 0,
            "tokens_saved": 0,
        }

    def _make_ccr_key(self, content: str) -> str:
        """生成 CCR 缓存键（BLAKE3 风格，用 SHA256 替代）"""
        return hashlib.sha256(content.encode()).hexdigest()[:24]

    def _cache_original(self, ccr_key: str, content: str) -> Path:
        """缓存原始数据到本地文件"""
        cache_path = self._cache_dir / f"{ccr_key}.json"
        atomic_write(cache_path, json.dumps({
            "content": content,
            "cached_at": time.time(),
        }, ensure_ascii=False), encoding="utf-8")
        return cache_path

    def retrieve(self, ccr_key: str) -> str | None:
        """检索缓存的原始数据"""
        cache_path = self._cache_dir / f"{ccr_key}.json"
        if cache_path.exists():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8"))
                self._stats["retrieved_count"] += 1
                return data.get("content", "")
            except Exception as e:
                logger.warning("ccr.retrieve_failed", key=ccr_key, error=str(e))
        return None

    def _strip_historical_prefix(self, text: str) -> str:
        """剥离旧版 SUMMARY_PREFIX，防止旧指令嵌入摘要体"""
        for prefix in _HISTORICAL_SUMMARY_PREFIXES:
            if text.startswith(prefix):
                text = text[len(prefix):].lstrip("\n")
                break
        return text

    def _deterministic_fallback(self, messages: list[dict]) -> str:
        """确定性回退：LLM 不可用时提取关键锚点"""
        anchors = []
        for msg in messages:
            role = msg.get("role", "")
            content = str(msg.get("content", ""))
            if not content:
                continue

            # 提取用户请求
            if role == "user":
                anchors.append(f"用户: {content[:100]}")
            # 提取工具调用
            elif role == "assistant" and msg.get("tool_calls"):
                for tc in msg["tool_calls"][:3]:
                    fn = tc.get("function", {})
                    name = fn.get("name", "")
                    anchors.append(f"工具调用: {name}")
            # 提取错误信息
            elif "error" in content.lower() or "失败" in content:
                anchors.append(f"错误: {content[:80]}")
            # 提取文件路径
            else:
                import re
                paths = re.findall(r'[/\w]+\.\w{1,10}', content)
                for p in paths[:2]:
                    anchors.append(f"文件: {p}")

        if not anchors:
            return "（对话历史已压缩，无关键锚点可提取）"
        return "；".join(anchors[:15])

    def _tool_output_summary(self, tool_name: str, output: str, max_len: int = 80) -> str:
        """生成工具输出的信息性摘要（替代通用占位符）"""
        # 提取关键信息
        lines = output.strip().split("\n")

        # 终端命令：提取退出码
        if tool_name in ("shell_command", "terminal", "bash"):
            for line in reversed(lines[-5:]):
                if "exit" in line.lower() or "error" in line.lower():
                    return f"[{tool_name}] {line.strip()[:max_len]}"
            return f"[{tool_name}] 执行完成，{len(lines)} 行输出"

        # 文件操作：提取路径
        if tool_name in ("read_file", "write_file"):
            first_line = lines[0][:max_len] if lines else ""
            return f"[{tool_name}] {first_line}"

        # 搜索结果：提取匹配数
        if tool_name in ("search", "grep", "find"):
            return f"[{tool_name}] {len(lines)} 条结果"

        # 通用：首行
        return f"[{tool_name}] {lines[0][:max_len]}" if lines else f"[{tool_name}] （空输出）"

    def compress_tool_output(self, output: str, tool_name: str = "") -> str:
        """压缩工具输出

        保留关键信息，替换冗余部分为压缩标记。
        """
        if len(output) <= self.TOOL_OUTPUT_THRESHOLD:
            return output

        # 缓存原始数据
        ccr_key = self._make_ccr_key(output)
        self._cache_original(ccr_key, output)

        # 生成压缩版本
        lines = output.split("\n")
        total_lines = len(lines)

        # 保留前 5 行和后 5 行
        if total_lines > 15:
            head = lines[:5]
            tail = lines[-5:]
            summary = self._tool_output_summary(tool_name, output)
            compressed = "\n".join(head) + f"\n\n... [已压缩：{total_lines} 行，摘要：{summary}，原始数据可通过 retrieve_context 检索，key={ccr_key}] ...\n\n" + "\n".join(tail)
        else:
            # 行数不多但字符多，截断
            summary = self._tool_output_summary(tool_name, output)
            half = len(output) // 3
            compressed = output[:half] + f"\n\n... [已压缩，摘要：{summary}，原始数据可通过 retrieve_context 检索，key={ccr_key}] ...\n\n" + output[-half:]

        self._stats["compressed_count"] += 1
        self._stats["tokens_saved"] += len(output) - len(compressed)
        logger.debug("ccr.tool_output_compressed", tool=tool_name,
                     original=len(output), compressed=len(compressed),
                     key=ccr_key)
        return compressed

    def compress_history(self, messages: list[dict], keep_recent: int = 5) -> CompressionResult:
        """压缩对话历史

        保留最近 keep_recent 轮完整内容，之前的进行摘要压缩。
        返回 CompressionResult 包含 messages、summary、tokens_saved。
        """
        if len(messages) <= keep_recent * 2:  # 每轮 user+assistant
            return CompressionResult(messages=messages)

        # 分离需要压缩的和保留的
        split_point = max(0, len(messages) - keep_recent * 2)
        to_compress = messages[:split_point]
        to_keep = messages[split_point:]

        if not to_compress:
            return CompressionResult(messages=messages)

        # 对早期消息生成摘要标记
        summary_parts = []
        for msg in to_compress:
            role = msg.get("role", "")
            content = msg.get("content", "")
            if not content:
                continue
            if role == "tool":
                # 保留工具结果的关键信息，不直接跳过
                tool_name = msg.get("name", "工具")
                summary_parts.append(f"[{tool_name}结果]: {content[:60]}")
                continue
            prefix = {"user": "用户", "assistant": "纳西妲"}.get(role, role)
            summary_parts.append(f"{prefix}: {content[:80]}")

        if summary_parts:
            summary_text = "；".join(summary_parts[:20])
            # 剥离旧版前缀，防止旧指令嵌入摘要体
            summary_text = self._strip_historical_prefix(summary_text)
            ccr_key = self._make_ccr_key(summary_text)
            self._cache_original(ccr_key, "\n".join(summary_parts))

            compressed_msg = {
                "role": "system",
                "content": (
                    SUMMARY_PREFIX + "\n" + summary_text[:500] + "\n"
                    f"[可通过 retrieve_context 工具检索完整历史，key={ccr_key}]"
                ),
            }
            self._stats["compressed_count"] += 1
            # 估算节省的 token 数
            original_tokens = sum(len(m.get("content", "")) for m in to_compress)
            compressed_tokens = len(compressed_msg["content"])
            tokens_saved = max(0, original_tokens - compressed_tokens)
            self._stats["tokens_saved"] += tokens_saved
            return CompressionResult(
                messages=[compressed_msg] + to_keep,
                summary=summary_text[:500],
                tokens_saved=tokens_saved,
            )

        return CompressionResult(messages=messages)

    def get_stats(self) -> dict:
        """获取压缩统计"""
        return dict(self._stats)


# 全局压缩器实例
_default_compressor: ContextCompressor | None = None

def get_context_compressor(router=None) -> ContextCompressor:
    global _default_compressor
    if _default_compressor is None:
        _default_compressor = ContextCompressor(router=router)
    return _default_compressor


# 注册为工具
from tool_engine.tool_registry import register_tool, ToolPermission, ToolResult

@register_tool(
    name="retrieve_context",
    description="检索之前被压缩的上下文原始数据。当需要查看被压缩的工具输出或对话历史时使用。",
    schema={
        "type": "object",
        "properties": {
            "key": {"type": "string", "description": "压缩标记中的 key 值"},
        },
        "required": ["key"],
    },
    permission=ToolPermission.READ_ONLY,
    max_frequency=30,
)
async def retrieve_context(key: str) -> ToolResult:
    """检索压缩的原始数据"""
    compressor = get_context_compressor()
    content = compressor.retrieve(key)
    if content:
        return ToolResult.ok(content)
    return ToolResult.fail(f"未找到 key={key} 对应的缓存数据")
