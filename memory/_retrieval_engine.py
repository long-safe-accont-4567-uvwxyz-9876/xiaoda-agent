"""兼容垫片：RetrievalEngine 实现已按职责拆分至 memory/retrieval/ 包。

历史说明：本模块原为 MemoryManager 检索方法的抽取实现（约 2100 行），
2026-08 按职责拆分为 retrieval 子包（pipeline/channels/fusion/query_transform/scoring），
本文件仅保留 re-export，保证既有消费者
（memory/memory_manager.py、tests/test_retrieval_single_channel.py 等）的
`from memory._retrieval_engine import RetrievalEngine` 路径零破坏。
新代码请直接 `from memory.retrieval import RetrievalEngine`。

拆分映射（原行号为拆分前近似位置）：
- 七路召回编排/空路剔除/单路短路/raw 兜底/顶层入口 → retrieval/pipeline.py
- 通道实现 FTS/Vec/HyDE/扩散/时间/对话日志 + ContextNest selector → retrieval/channels.py
- RRF 融合 + Entity Boost + Reranker 精排 → retrieval/fusion.py
- 查询理解/变换/多查询调度 → retrieval/query_transform.py
- FSRS 评分/去重/topic/touch → retrieval/scoring.py
- 写入侧 _insert_indexed_children → 归还生产调用方模块 memory/memory_manager.py
"""
from memory.retrieval import RecallChannels, RetrievalEngine

__all__ = ["RecallChannels", "RetrievalEngine"]
