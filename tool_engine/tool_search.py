"""Tool Search — BM25 按需加载工具定义

灵感: Anthropic Claude Tool Search (85% token 节省)
原理: 只向 LLM 暴露一个 search_tools 元工具,
LLM 按需搜索并加载完整工具定义, 避免上下文膨胀。

实现:
1. 工具注册时标记 defer_loading
2. BM25 检索匹配工具
3. 只返回 top-k 工具的完整定义
"""
import math, re
from collections import Counter
from dataclasses import dataclass, field
from loguru import logger
from typing import Optional


@dataclass
class ToolDef:
    """工具定义"""
    name: str
    description: str
    parameters: dict
    handler_path: str = ""        # 模块路径, 懒加载
    defer_loading: bool = True    # 默认延迟加载
    keywords: list[str] = field(default_factory=list)
    use_count: int = 0            # 热度统计
    last_used: float = 0          # 最近使用时间

    @property
    def token_estimate(self) -> int:
        """估算工具定义的 token 数"""
        import json
        text = json.dumps({"name": self.name, "description": self.description, "parameters": self.parameters})
        return len(text) // 4  # 粗略估算


class BM25Index:
    """BM25 倒排索引"""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._docs: list[list[str]] = []
        self._df: Counter = Counter()
        self._avg_len: float = 0
        self._tool_defs: list[ToolDef] = []

    def add_tool(self, tool: ToolDef) -> None:
        """添加工具到索引"""
        doc = self._tokenize(f"{tool.name} {tool.description} {' '.join(tool.keywords)}")
        self._docs.append(doc)
        self._tool_defs.append(tool)
        for term in set(doc):
            self._df[term] += 1
        self._avg_len = sum(len(d) for d in self._docs) / max(1, len(self._docs))

    def search(self, query: str, top_k: int = 5) -> list[ToolDef]:
        """BM25 搜索, 返回 top-k 工具"""
        if not self._docs:
            return []
        q_terms = self._tokenize(query)
        scores = []
        for i, doc in enumerate(self._docs):
            score = self._bm25_score(q_terms, doc, i)
            scores.append((score, i))
        scores.sort(reverse=True)
        return [self._tool_defs[idx] for _, idx in scores[:top_k]]

    def _bm25_score(self, q_terms: list[str], doc: list[str], doc_idx: int) -> float:
        """计算 BM25 分数"""
        doc_len = len(doc)
        doc_counter = Counter(doc)
        score = 0.0
        N = len(self._docs)
        for term in q_terms:
            if term not in doc_counter:
                continue
            tf = doc_counter[term]
            df = self._df.get(term, 0)
            idf = math.log((N - df + 0.5) / (df + 0.5) + 1)
            norm = tf * (self._k1 + 1) / (tf + self._k1 * (1 - self._b + self._b * doc_len / max(1, self._avg_len)))
            score += idf * norm
        return score

    @staticmethod
    def _tokenize(text: str) -> list[str]:
        """简单分词"""
        return re.findall(r'[a-zA-Z_]\w*', text.lower())


class ToolSearchEngine:
    """Tool Search 引擎 — 按需加载工具定义"""

    def __init__(self) -> None:
        self._index = BM25Index()
        self._always_loaded: list[ToolDef] = []  # 常驻工具 (不延迟加载)
        self._loaded_tools: dict[str, ToolDef] = {}  # 已加载的工具
        self._search_count = 0
        self._total_token_saved = 0

    def register(self, tool: ToolDef, always_loaded: bool = False) -> None:
        """注册工具"""
        if always_loaded:
            tool.defer_loading = False
            self._always_loaded.append(tool)
            self._loaded_tools[tool.name] = tool
        else:
            self._index.add_tool(tool)
        logger.debug(f"ToolSearch: 注册工具 {tool.name} (defer={tool.defer_loading})")

    def get_tools_for_llm(self, query: str = "", top_k: int = 5) -> list[dict]:
        """获取要发送给 LLM 的工具定义"""
        tools = list(self._always_loaded)
        if query:
            searched = self._index.search(query, top_k=top_k)
            tools.extend(searched)
            self._search_count += 1
            saved = sum(t.token_estimate for t in self._index._tool_defs) - sum(t.token_estimate for t in searched)
            self._total_token_saved += saved
            logger.info(f"ToolSearch: 搜索'{query}' → {len(searched)}个工具, 节省~{saved} tokens")
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                }
            }
            for t in tools
        ]

    def get_search_tool_def(self) -> dict:
        """返回 search_tools 元工具定义 (暴露给 LLM)"""
        return {
            "type": "function",
            "function": {
                "name": "search_tools",
                "description": "Search for available tools by keyword. Use this when you need a tool that isn't already loaded.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "Keywords describing the tool you need"},
                        "top_k": {"type": "integer", "description": "Max tools to return", "default": 5}
                    },
                    "required": ["query"]
                }
            }
        }

    def get_stats(self) -> dict:
        """获取统计信息"""
        total_tools = len(self._always_loaded) + len(self._index._tool_defs)
        return {
            "total_tools": total_tools,
            "always_loaded": len(self._always_loaded),
            "deferred": len(self._index._tool_defs),
            "search_count": self._search_count,
            "total_token_saved": self._total_token_saved,
            "avg_token_saved": self._total_token_saved / max(1, self._search_count),
        }


# 全局单例
_engine = ToolSearchEngine()


def get_tool_search_engine() -> ToolSearchEngine:
    return _engine


def register_tool(name: str, description: str, parameters: dict,
                  handler_path: str = "", keywords: list[str] | None = None,
                  always_loaded: bool = False) -> None:
    """便捷注册工具"""
    tool = ToolDef(
        name=name,
        description=description,
        parameters=parameters,
        handler_path=handler_path,
        keywords=keywords or [],
    )
    _engine.register(tool, always_loaded=always_loaded)
