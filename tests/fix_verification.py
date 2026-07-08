"""Bug 修复验证测试"""
import sys
import os
import asyncio
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv
load_dotenv()

# 项目根目录 (基于当前文件位置计算，避免硬编码绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

passed = 0
failed = 0


def check(name, condition, detail=""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  [PASS] {name}")
    else:
        failed += 1
        print(f"  [FAIL] {name}: {detail}")


async def main():
    print("=" * 60)
    print("Bug 修复验证测试")
    print("=" * 60)

    # ---- Fix 1: 焦虑映射 ----
    print("\n[Fix 1] 焦虑映射: sad -> lonely")
    from agent_core import AgentCore
    core = AgentCore()

    # get_sticker_info 中的映射
    from emotion.emotion_simple import detect_emotion
    anxious = detect_emotion("我好焦虑啊")
    primary = anxious.get("primary", "")
    clean, sticker = core.get_sticker_info("回复", user_emotion=primary)
    # 焦虑应该映射到 lonely，而不是 sad
    # lonely 在 sticker 目录中可能没有对应目录，但映射应该是正确的
    import inspect
    source = inspect.getsource(core.get_sticker_info)
    check("get_sticker_info 焦虑->lonely", '"焦虑": "lonely"' in source, "映射未更新")

    source2 = inspect.getsource(core._process_impl)
    check("_process_impl _emap 焦虑->lonely", '"焦虑": "lonely"' in source2, "映射未更新")

    source3 = inspect.getsource(core._dispatch_single_sub_agent)
    check("_dispatch_single_sub_agent _sub_emap 焦虑->lonely", '"焦虑": "lonely"' in source3, "映射未更新")

    # ---- Fix 2: _DESC_EMOTION_MAP ----
    print("\n[Fix 2] _DESC_EMOTION_MAP 分类修正")
    from emotion.sticker_manager import StickerManager
    # 表情包目录: 优先使用项目内置目录，找不到则使用临时目录
    sticker_dir = PROJECT_ROOT / "assets" / "stickers" / "xiaoda"
    if not sticker_dir.is_dir():
        import tempfile
        sticker_dir = Path(tempfile.mkdtemp())
    sm = StickerManager(str(sticker_dir))
    desc_map = sm._DESC_EMOTION_MAP

    check("害怕 -> shy", desc_map.get("害怕") == "shy", f"实际: {desc_map.get('害怕')}")
    check("惊恐 -> shy", desc_map.get("惊恐") == "shy", f"实际: {desc_map.get('惊恐')}")
    check("温柔 -> greeting", desc_map.get("温柔") == "greeting", f"实际: {desc_map.get('温柔')}")

    # 验证 "害怕"/"惊恐" 不再被归类为 angry
    print("\n[Fix 2b] 害怕/惊恐不再归类为 angry:")
    angry_stickers = sm._emotion_cache.get("angry", [])
    fear_as_angry = [s for s in angry_stickers if "害怕" in s.stem or "惊恐" in s.stem]
    check("害怕/惊恐不在 angry 缓存中", len(fear_as_angry) == 0,
          f"仍有 {len(fear_as_angry)} 个害怕/惊恐表情包被归类为 angry: {[s.name for s in fear_as_angry]}")

    # ---- Fix 3: 子 Agent 表情包 ----
    print("\n[Fix 3] 子 Agent 表情包发送")
    source3 = inspect.getsource(core._dispatch_single_sub_agent)
    check("sub_sticker_mgr 已添加", "sub_sticker_mgr" in source3, "未添加子Agent表情包逻辑")
    check("klee 判断逻辑", '"klee" in target.lower()' in source3, "未添加 klee 判断")
    check("should_send 调用", "should_send" in source3, "未调用 should_send")

    # ---- Fix 4: .env.example ----
    print("\n[Fix 4] .env.example AGNES_API_KEY")
    env_content = open(PROJECT_ROOT / ".env.example").read()
    check("AGNES_API_KEY 存在", "AGNES_API_KEY" in env_content, "未添加")
    check("AGNES_BASE_URL 存在", "AGNES_BASE_URL" in env_content, "未添加")
    check("AGNES_IMAGE_MODEL 存在", "AGNES_IMAGE_MODEL" in env_content, "未添加")

    # ---- TTS 回归测试 ----
    print("\n[TTS 回归] TTS 引擎可用性")
    from emotion.tts_engine import TTSEngine
    tts = TTSEngine()
    await tts.init()
    check("TTS available", tts.available, "TTS 不可用")

    if tts.available:
        result = await tts.synthesize_xiaoda("验证测试", emotion="happy")
        check("TTS 合成成功", result is not None, "合成返回 None")
        if result:
            check("TTS 文件存在", result.exists(), f"文件不存在: {result}")
            check("TTS 文件大小 > 0", result.stat().st_size > 0, "文件为空")

    # ---- Agnes 工具回归 ----
    print("\n[Agnes 回归] 无 Key 降级")
    from tools.agnes_tools import agnes_image_generate, agnes_video_generate
    import tools.agnes_tools as at
    original_key = at.AGNES_API_KEY
    at.AGNES_API_KEY = ""

    img_result = await agnes_image_generate(prompt="test")
    check("图片生成无Key降级", not img_result.success and "API Key" in img_result.error,
          f"success={img_result.success}, error={img_result.error}")

    vid_result = await agnes_video_generate(prompt="test")
    check("视频生成无Key降级", not vid_result.success and "API Key" in vid_result.error,
          f"success={vid_result.success}, error={vid_result.error}")

    at.AGNES_API_KEY = original_key

    # ---- 表情包完整流程回归 ----
    print("\n[表情包回归] 完整流程")
    test_texts = [
        ("太好了", "happy"),
        ("好难过", "sad"),
        ("哼生气", "angry"),
        ("害羞", "shy"),
        ("你好", "greeting"),
    ]
    for text, expected in test_texts:
        detected = sm.detect_emotion(text)
        check(f"detect '{text}' -> {expected}", detected == expected,
              f"实际: {detected}")

    # ---- 总结 ----
    print("\n" + "=" * 60)
    total = passed + failed
    print(f"测试结果: {passed}/{total} 通过, {failed} 失败")
    if failed == 0:
        print("所有 Bug 修复验证通过!")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
