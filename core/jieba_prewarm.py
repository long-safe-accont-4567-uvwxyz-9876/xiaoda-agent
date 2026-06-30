"""jieba 后台预热 — 在 AgentCore 初始化后于后台线程预加载词典，避免首次对话冻结。

db/fts_utils.py 与 memory/memory_manager.py 在函数内 lazy import jieba，
首次调用时同步加载 ~5MB 词典阻塞 1-2 秒。预热模块在后台线程提前完成加载，
用户首次对话时 jieba 已就绪。
"""
from __future__ import annotations

import asyncio
from loguru import logger

_prewarm_started: bool = False


def _load_jieba_dict() -> None:
    """在后台线程中加载 jieba 默认词典。捕获 ImportError 静默返回。"""
    try:
        import jieba
        jieba.initialize()
        logger.info("jieba.prewarm_done")
    except ImportError:
        # jieba 未安装，FTS 分词降级到 n-gram（db/fts_utils.py 现有行为）
        pass
    except Exception as e:
        logger.warning(f"jieba.prewarm_failed error={e}")


async def prewarm_jieba() -> None:
    """后台协程：用 asyncio.to_thread 在后台线程加载 jieba 词典，不阻塞事件循环。

    幂等：重复调用不会重复加载（_prewarm_started 标志保护）。
    """
    global _prewarm_started
    if _prewarm_started:
        return
    _prewarm_started = True
    await asyncio.to_thread(_load_jieba_dict)


def reset_prewarm_flag_for_test() -> None:
    """测试辅助：重置预热标志，便于单元测试。生产代码不应调用。"""
    global _prewarm_started
    _prewarm_started = False
