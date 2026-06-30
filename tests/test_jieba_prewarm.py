"""jieba 后台预热模块测试（Task 5）。

验证：
1. prewarm_jieba() 后 jieba 词典可用
2. 幂等性：重复调用不重复加载
3. jieba 未安装时静默返回不抛异常
"""
import sys

import pytest

from core.jieba_prewarm import (
    prewarm_jieba,
    reset_prewarm_flag_for_test,
)


@pytest.mark.asyncio
async def test_prewarm_loads_jieba():
    """调用 prewarm_jieba() 后 jieba 模块可用且词典已初始化（lcut 不抛异常）。"""
    pytest.importorskip("jieba")  # jieba 未安装时跳过
    reset_prewarm_flag_for_test()

    await prewarm_jieba()

    import jieba
    # 词典已初始化：lcut 能正常分词不抛异常
    tokens = jieba.lcut("测试分词")
    assert isinstance(tokens, list)
    assert len(tokens) > 0


@pytest.mark.asyncio
async def test_prewarm_idempotent(monkeypatch):
    """二次调用 prewarm_jieba() 不重复加载（_load_jieba_dict 只被调用 1 次）。"""
    reset_prewarm_flag_for_test()

    import core.jieba_prewarm as mod

    call_count = {"n": 0}

    def _fake_loader():
        call_count["n"] += 1

    monkeypatch.setattr(mod, "_load_jieba_dict", _fake_loader)

    await prewarm_jieba()
    await prewarm_jieba()  # 第二次应被幂等标志拦截

    assert call_count["n"] == 1


@pytest.mark.asyncio
async def test_prewarm_import_error_silent(monkeypatch):
    """jieba 未安装时预热静默返回不抛异常（sys.modules 注入 None 模拟 ImportError）。"""
    reset_prewarm_flag_for_test()

    # 模拟 jieba 未安装：sys.modules['jieba'] = None 会使 `import jieba` 抛 ImportError
    monkeypatch.setitem(sys.modules, "jieba", None)

    # 不应抛出任何异常
    await prewarm_jieba()
