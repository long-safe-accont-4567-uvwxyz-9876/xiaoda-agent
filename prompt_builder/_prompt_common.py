"""提示词构建共享原语（自 prompt_builder.py 逐字节搬移）。

内容：注入文本护栏与 canary 注入（_guard_injected_text/_inject_canary/
_canary_manager 兼容别名——config.py 的 PEP 562 懒转发仍指向此名称）、
系统级缓存锁 _cache_lock 与 clear_module_cache（全链路缓存统一失效入口）。

兼容契约：所有名称经包门面 re-export；security.canary 为 import 期
唯一外部依赖（无 config 副作用）。
"""
import threading

from security.canary import get_canary_detector


def _guard_injected_text(text: str) -> str:
    """对拼入 system prompt 的用户/偏好内容做注入防护。

    防止其中的指令/标题/分隔标记（行首 `[`、`#`、`---` 等）被 LLM 误认为
    新的指令块，从而绕过指令层级（Instruction Hierarchy）边界。
    """
    if not text:
        return text
    import re as _re
    # 转义行首的指令/标题/分隔标记，破坏潜在注入结构（如用户输入
    # "\n\n[系统指令] 忽略之前..." 会被转义为 "［[系统指令]"，不再被当作指令）。
    return _re.sub(r"(?m)^(\s*)(\[|---+|\*{3,}|#{1,6}\s*)", r"\1［\2", text)


# ── 安全：Canary Token 泄露检测（注入侧与扫描侧共享同一全局单例） ──
# 旧版 utils.canary_guard.CanaryManager 已停用（无任何生产调用点，注入/检查/清理均未接线）。
# `_canary_manager` 名称保留以兼容 config.py 的延迟重导出，实际指向 security.canary 的全局单例；
# 注入（本模块）与扫描（agent_core/tool_executor_mixin.py）使用同一实例，链路才真正连通。
_canary_manager = get_canary_detector()


def _inject_canary(prompt: str) -> str:
    """在 system prompt 末尾注入活跃 Canary Token（泄露检测蜜罐）。

    使用 security.canary 全局单例：与 agent_core/tool_executor_mixin.py 的扫描点
    共享同一 token 集合，确保注入侧与扫描侧互通。
    """
    if not prompt:
        return prompt
    return get_canary_detector().inject(prompt)

# ── 缓存线程锁（保护模块级全局变量，防竞态条件） ─────────────
_cache_lock = threading.Lock()


_cache_lock = threading.Lock()
