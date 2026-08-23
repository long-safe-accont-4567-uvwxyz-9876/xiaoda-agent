"""memory.retrieval：检索引擎子包（自 memory/_retrieval_engine.py 按职责拆分）。

模块划分（纯移动，行为零变化）：
- pipeline        顶层入口 + 七路召回编排/空路剔除/单路短路/raw 兜底 + RetrievalEngine 组合
- channels        通道实现 FTS/Vec/HyDE/扩散/时间/对话日志 + ContextNest selector
- fusion          RRF 融合 + Entity Boost + Reranker 精排
- query_transform 查询理解/变换/多查询调度
- scoring         FSRS 评分/去重/topic/touch

兼容入口：`from memory._retrieval_engine import RetrievalEngine` 垫片继续可用。
"""
from memory.retrieval.pipeline import RecallChannels, RetrievalEngine

__all__ = ["RecallChannels", "RetrievalEngine"]
