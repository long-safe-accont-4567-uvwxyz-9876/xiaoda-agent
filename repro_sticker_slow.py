"""复现 list_stickers 工具执行慢的问题：启动真实 core（跳过 bootstrap），直接调用工具链。"""
import asyncio
import time
import sys


def log(*a):
    print(*a, flush=True)


async def main() -> None:
    from agent_core.core import AgentCore
    from core.bootstrap import AgentCoreBootstrapper

    t0 = time.time()
    core = AgentCore()
    log(f"[BOOT] AgentCore init: {time.time()-t0:.2f}s")

    # 注册 list_stickers 工具（不跑完整 bootstrap，避免联网/连 QQ）
    boot = AgentCoreBootstrapper(core)
    try:
        boot._register_sticker_tool()
        log("[BOOT] _register_sticker_tool ok")
    except Exception as e:
        log(f"[BOOT] _register_sticker_tool err: {e}")

    # 预热：测首次访问 sticker_manager 耗时
    t_start = time.time()
    mgr = core.sticker_manager
    ok = mgr.available
    log(f"[WARM] first sticker_manager access: {time.time()-t_start:.2f}s available={ok}")

    # 直接调用带钩子的工具执行
    t2 = time.time()
    result = await core._execute_tool_with_hooks("list_stickers", {})
    elapsed = time.time() - t2
    log(f"\n[RESULT] _execute_tool_with_hooks elapsed={elapsed:.2f}s")
    log(f"[RESULT] success={getattr(result, 'success', None)}")
    data = getattr(result, "data", None)
    if isinstance(data, dict):
        log(f"[RESULT] total={data.get('total')}")
    log(f"[RESULT] error={getattr(result, 'error', None)}")


if __name__ == "__main__":
    asyncio.run(main())