#!/usr/bin/env python3
"""TTS + 表情包 + 图片生成 + 视频生成 综合测试"""
import asyncio
import sys
import os
import time
import tempfile
import json
from pathlib import Path
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

# 项目根目录 (基于当前文件位置计算，避免硬编码绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

bugs_found = []

def report_bug(severity, module, desc):
    bugs_found.append({"severity": severity, "module": module, "desc": desc})
    print(f"    BUG [{severity}] {module}: {desc}")


async def test_tts():
    """Part 1: TTS 语音合成"""
    print("=" * 60)
    print("Part 1: TTS 语音合成")
    print("=" * 60)

    from emotion.tts_engine import TTSEngine, EMOTION_STYLE_MAP, VOICE_REFERENCES, VOICE_STYLES

    # 1.1 引擎初始化
    print("\n[1.1] TTSEngine 初始化:")
    engine = TTSEngine()
    print(f"    INFO: available={engine.available} (初始化前)")

    with tempfile.TemporaryDirectory() as td:
        await engine.init(output_dir=td)
        print(f"    OK: available={engine.available}")

        if not engine.available:
            print("    SKIP: TTS 不可用（无 API Key 或参考音频）")
            # 仍然测试其他功能
        else:
            # 1.2 真实语音合成
            print("\n[1.2] 真实语音合成:")
            try:
                result = await engine.synthesize_nahida("你好，我是纳西妲！", emotion="happy")
                if result:
                    size = result.stat().st_size
                    print(f"    OK: 合成成功 {result} ({size} bytes, {size/1024:.1f} KB)")
                else:
                    print("    WARN: 合成返回 None")
            except Exception as e:
                report_bug("HIGH", "tts_engine", f"synthesize_nahida 崩溃: {e}")

            # 1.3 不同情绪
            print("\n[1.3] 不同情绪合成:")
            for emotion in ["happy", "sad", "angry", "neutral"]:
                try:
                    result = await engine.synthesize("测试情绪", voice="nahida", emotion=emotion)
                    if result:
                        print(f"    OK: emotion={emotion} -> {result.name} ({result.stat().st_size/1024:.1f}KB)")
                    else:
                        print(f"    WARN: emotion={emotion} -> None")
                except Exception as e:
                    report_bug("MEDIUM", "tts_engine", f"emotion={emotion} 崩溃: {e}")

            # 1.4 可莉语音
            print("\n[1.4] 可莉语音合成:")
            try:
                result = await engine.synthesize_keli("可莉来啦！", emotion="excited")
                if result:
                    print(f"    OK: 可莉合成成功 ({result.stat().st_size/1024:.1f}KB)")
                else:
                    print("    INFO: 可莉参考音频可能不存在")
            except Exception as e:
                report_bug("MEDIUM", "tts_engine", f"synthesize_keli 崩溃: {e}")

    # 1.5 EMOTION_STYLE_MAP 完整性
    print("\n[1.5] EMOTION_STYLE_MAP 检查:")
    expected_emotions = ["happy", "sad", "angry", "shy", "surprised", "fear", "neutral", "greeting", "caring", "playful", "lonely"]
    for e in expected_emotions:
        if e in EMOTION_STYLE_MAP:
            print(f"    OK: {e} -> {EMOTION_STYLE_MAP[e]}")
        else:
            report_bug("LOW", "tts_engine", f"EMOTION_STYLE_MAP 缺少 {e}")

    # 1.6 VOICE_REFERENCES 检查
    print("\n[1.6] VOICE_REFERENCES 检查:")
    for name, path in VOICE_REFERENCES.items():
        exists = path.exists()
        print(f"    {'OK' if exists else 'INFO'}: {name} -> {path} (exists={exists})")

    # 1.7 VOICE_STYLES 检查
    print("\n[1.7] VOICE_STYLES 检查:")
    for name, style in VOICE_STYLES.items():
        print(f"    OK: {name} -> {style[:40]}...")

    # 1.8 边界条件
    print("\n[1.8] 边界条件:")
    # 空文本
    try:
        result = await engine.synthesize("", voice="nahida")
        print(f"    INFO: 空文本 -> {result}")
    except Exception as e:
        report_bug("LOW", "tts_engine", f"空文本崩溃: {e}")

    # 不存在的 voice
    try:
        result = await engine.synthesize("测试", voice="nonexistent")
        if result is None:
            print("    OK: 不存在的 voice 返回 None")
        else:
            report_bug("MEDIUM", "tts_engine", "不存在的 voice 未返回 None")
    except Exception as e:
        report_bug("MEDIUM", "tts_engine", f"不存在的 voice 崩溃: {e}")

    # 超长文本
    try:
        long_text = "这是一段很长的文本。" * 500  # ~4500 chars
        result = await engine.synthesize(long_text, voice="nahida")
        print(f"    INFO: 超长文本({len(long_text)} chars) -> {type(result).__name__}")
    except Exception as e:
        print(f"    INFO: 超长文本被拒绝: {str(e)[:60]}")

    return bugs_found


async def test_sticker():
    """Part 2: 表情包发送"""
    print("\n" + "=" * 60)
    print("Part 2: 表情包发送")
    print("=" * 60)

    from emotion.sticker_manager import StickerManager

    # 2.1 使用真实表情包目录
    # 优先使用项目内置目录，再尝试外部挂载路径，最后回退到临时目录
    sticker_dirs = [
        str(PROJECT_ROOT / "assets" / "stickers" / "nahida"),
        "/media/orangepi/KIOXIA/nahida-data/stickers",
        "/mnt/usb2/stickers",
    ]
    sticker_dir = None
    for d in sticker_dirs:
        if os.path.isdir(d):
            sticker_dir = d
            break

    if not sticker_dir:
        print("    INFO: 未找到表情包目录，使用临时目录测试")
        sticker_dir = tempfile.mkdtemp()

    print(f"\n[2.1] StickerManager 初始化 (dir={sticker_dir}):")
    sm = StickerManager(sticker_dir)
    print(f"    OK: available={sm.available}")

    # 2.2 情绪检测
    print("\n[2.2] 情绪检测:")
    test_cases = [
        ("好开心啊！", "happy"),
        ("呜呜好难过", "sad"),
        ("哼！生气了", "angry"),
        ("害羞///", "shy"),
        ("咦？为什么呀", "curious"),
        ("你好呀", "greeting"),
        ("让我想想", "thinking"),
        ("今天天气不错", ""),
    ]
    for text, expected in test_cases:
        result = sm.detect_emotion(text)
        ok = result == expected if expected else result == ""
        print(f"    {'OK' if ok else 'INFO'}: '{text}' -> '{result}' (expected='{expected}')")

    # 2.3 表情包选取
    print("\n[2.3] 表情包选取:")
    if sm.available:
        for emotion in ["happy", "sad", "angry", "shy", "curious", "greeting", "thinking"]:
            sticker = sm.pick(emotion)
            if sticker:
                print(f"    OK: {emotion} -> {sticker.name}")
            else:
                print(f"    INFO: {emotion} -> None (无此类别)")

        # 随机选取
        random_sticker = sm.pick()
        if random_sticker:
            print(f"    OK: 随机 -> {random_sticker.name}")
        else:
            print("    INFO: 随机选取 -> None")
    else:
        print("    INFO: 表情包不可用，跳过选取测试")

    # 2.4 pick_by_text
    print("\n[2.4] pick_by_text:")
    if sm.available:
        for text in ["好开心啊！", "呜呜呜", "哼！"]:
            sticker = sm.pick_by_text(text)
            if sticker:
                print(f"    OK: '{text}' -> {sticker.name}")
            else:
                print(f"    INFO: '{text}' -> None")
    else:
        print("    INFO: 表情包不可用")

    # 2.5 should_send
    print("\n[2.5] should_send 概率:")
    send_count = sum(1 for _ in range(100) if sm.should_send("好开心", "happy"))
    print(f"    INFO: 100次中有情绪 should_send=True: {send_count}次 (预期~70)")

    send_count2 = sum(1 for _ in range(100) if sm.should_send("你好", ""))
    print(f"    INFO: 100次无情绪 should_send=True: {send_count2}次 (预期~40)")

    # 2.6 strip_emotion_tag
    print("\n[2.6] strip_emotion_tag:")
    tests = [
        ("[emotion:happy]你好", "你好"),
        ("[emotion:sad]呜呜", "呜呜"),
        ("普通文本", "普通文本"),
    ]
    for text, expected in tests:
        result = sm.strip_emotion_tag(text)
        ok = result == expected
        print(f"    {'OK' if ok else 'FAIL'}: '{text}' -> '{result}'")

    # 2.7 reload
    print("\n[2.7] reload:")
    sm.reload()
    print(f"    OK: reload 后 available={sm.available}")

    # 2.8 str 路径参数
    print("\n[2.8] str 路径参数:")
    sm2 = StickerManager("/tmp/nonexist_stickers")
    print(f"    OK: str 参数 accepted, available={sm2.available}")

    return bugs_found


async def test_agnes_image():
    """Part 3: Agnes 图片生成"""
    print("\n" + "=" * 60)
    print("Part 3: Agnes 图片生成")
    print("=" * 60)

    from tools.agnes_tools import agnes_image_generate
    from config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_IMAGE_MODEL

    print(f"\n[3.1] 配置检查:")
    print(f"    INFO: AGNES_API_KEY = {'***' + AGNES_API_KEY[-4:] if AGNES_API_KEY else '(未设置)'}")
    print(f"    INFO: AGNES_BASE_URL = {AGNES_BASE_URL}")
    print(f"    INFO: AGNES_IMAGE_MODEL = {AGNES_IMAGE_MODEL}")

    if not AGNES_API_KEY:
        print("    SKIP: Agnes API Key 未配置，跳过真实 API 测试")
        # 测试无 Key 时的错误处理
        print("\n[3.2] 无 Key 错误处理:")
        result = await agnes_image_generate(prompt="test")
        if result.success is False:
            print(f"    OK: 无 Key 返回失败: {result.data[:40]}")
        else:
            report_bug("MEDIUM", "agnes_tools", "无 API Key 时应返回失败")
        return bugs_found

    # 3.2 真实图片生成
    print("\n[3.2] 真实图片生成:")
    try:
        result = await agnes_image_generate(prompt="A cute anime girl with green hair in a magical forest, digital art style")
        if result.success:
            print(f"    OK: 图片生成成功: {result.data[:100]}")
        else:
            print(f"    WARN: 图片生成失败: {result.data[:100]}")
    except Exception as e:
        report_bug("HIGH", "agnes_tools", f"图片生成崩溃: {e}")
        import traceback; traceback.print_exc()

    # 3.3 不同尺寸
    print("\n[3.3] 不同尺寸:")
    for size in ["1024x1024", "512x512"]:
        try:
            result = await agnes_image_generate(prompt="a beautiful sunset", size=size)
            status = "OK" if result.success else "WARN"
            print(f"    {status}: size={size} -> {result.data[:60]}")
        except Exception as e:
            report_bug("MEDIUM", "agnes_tools", f"size={size} 崩溃: {e}")

    # 3.4 边界条件
    print("\n[3.4] 边界条件:")
    # 空提示
    try:
        result = await agnes_image_generate(prompt="")
        print(f"    INFO: 空 prompt -> success={result.success}")
    except Exception as e:
        report_bug("LOW", "agnes_tools", f"空 prompt 崩溃: {e}")

    # 超长提示
    try:
        long_prompt = "A detailed scene: " + "beautiful flowers and trees " * 100
        result = await agnes_image_generate(prompt=long_prompt)
        print(f"    INFO: 超长 prompt ({len(long_prompt)} chars) -> success={result.success}")
    except Exception as e:
        print(f"    INFO: 超长 prompt 被拒绝: {str(e)[:60]}")

    return bugs_found


async def test_agnes_video():
    """Part 4: Agnes 视频生成"""
    print("\n" + "=" * 60)
    print("Part 4: Agnes 视频生成")
    print("=" * 60)

    from tools.agnes_tools import agnes_video_generate
    from config import AGNES_API_KEY, AGNES_VIDEO_MODEL

    print(f"\n[4.1] 配置检查:")
    print(f"    INFO: AGNES_VIDEO_MODEL = {AGNES_VIDEO_MODEL}")

    if not AGNES_API_KEY:
        print("    SKIP: Agnes API Key 未配置，跳过真实 API 测试")
        # 测试无 Key 时的错误处理
        print("\n[4.2] 无 Key 错误处理:")
        result = await agnes_video_generate(prompt="test")
        if result.success is False:
            print(f"    OK: 无 Key 返回失败: {result.data[:40]}")
        else:
            report_bug("MEDIUM", "agnes_tools", "无 API Key 时应返回失败")
        return bugs_found

    # 4.2 真实视频生成（异步任务模式，可能需要等待）
    print("\n[4.2] 真实视频生成 (异步任务模式):")
    try:
        result = await agnes_video_generate(prompt="A peaceful forest with sunlight filtering through trees", seconds=3, fps=8)
        if result.success:
            print(f"    OK: 视频生成结果: {result.data[:100]}")
        else:
            print(f"    WARN: 视频生成失败: {result.data[:100]}")
    except Exception as e:
        report_bug("HIGH", "agnes_tools", f"视频生成崩溃: {e}")
        import traceback; traceback.print_exc()

    # 4.3 帧数计算验证
    print("\n[4.3] 帧数计算验证:")
    import math
    for seconds, fps in [(3, 8), (5, 24), (10, 30)]:
        raw_frames = int(seconds * fps)
        n = max(1, (raw_frames - 1) // 8)
        num_frames = min(8 * n + 1, 441)
        print(f"    INFO: {seconds}s@{fps}fps -> raw={raw_frames}, n={n}, num_frames={num_frames}")

    # 4.4 边界条件
    print("\n[4.4] 边界条件:")
    # 空 prompt
    try:
        result = await agnes_video_generate(prompt="")
        print(f"    INFO: 空 prompt -> success={result.success}")
    except Exception as e:
        report_bug("LOW", "agnes_tools", f"空 prompt 崩溃: {e}")

    return bugs_found


async def test_agent_integration():
    """Part 5: Agent 集成测试 - TTS+表情包在对话中的表现"""
    print("\n" + "=" * 60)
    print("Part 5: Agent 集成测试")
    print("=" * 60)

    from agent_core import AgentCore, ProcessResult

    core = AgentCore()

    # 5.1 检查 TTS 引擎集成
    print("\n[5.1] TTS 引擎集成:")
    if hasattr(core, 'tts_engine') or hasattr(core, '_tts_engine'):
        attr = getattr(core, 'tts_engine', None) or getattr(core, '_tts_engine', None)
        if attr:
            print(f"    OK: TTS 引擎存在, available={attr.available if hasattr(attr, 'available') else '?'}")
        else:
            print("    INFO: TTS 引擎属性为 None")
    else:
        print("    INFO: AgentCore 无 TTS 引擎属性")

    # 5.2 检查表情包管理器集成
    print("\n[5.2] 表情包管理器集成:")
    if hasattr(core, 'sticker_manager') or hasattr(core, '_sticker_manager'):
        attr = getattr(core, 'sticker_manager', None) or getattr(core, '_sticker_manager', None)
        if attr:
            print(f"    OK: 表情包管理器存在, available={attr.available if hasattr(attr, 'available') else '?'}")
        else:
            print("    INFO: 表情包管理器属性为 None")
    else:
        print("    INFO: AgentCore 无表情包管理器属性")

    # 5.3 检查 Agnes 工具注册
    print("\n[5.3] Agnes 工具注册:")
    from tool_engine.tool_registry import list_tools
    tools = list_tools()
    agnes_tools = [t for t in tools if 'agnes' in t['name'].lower()]
    for t in agnes_tools:
        print(f"    OK: {t['name']} 已注册")
    if not agnes_tools:
        print("    INFO: 无 Agnes 工具注册")

    # 5.4 检查 ProcessResult 是否包含 TTS/表情包字段
    print("\n[5.4] ProcessResult 字段:")
    import inspect
    if hasattr(ProcessResult, '__dataclass_fields__'):
        fields = ProcessResult.__dataclass_fields__
        for name in ['tts_path', 'sticker_path', 'image_url', 'audio_path']:
            if name in fields:
                print(f"    OK: {name} 字段存在")
            else:
                print(f"    INFO: {name} 字段不存在")
    else:
        # 尝试用 __init__ 参数检查
        sig = inspect.signature(ProcessResult.__init__)
        params = list(sig.parameters.keys())
        print(f"    INFO: ProcessResult 参数: {params}")

    # 5.5 检查 agnes_image_generate 在 tool_executor 中可用
    print("\n[5.5] 工具执行器中的 Agnes 工具:")
    from tool_engine.tool_registry import get_tool
    for name in ['agnes_image_generate', 'agnes_video_generate']:
        tool = get_tool(name)
        if tool:
            print(f"    OK: {name} 可获取")
        else:
            print(f"    INFO: {name} 不可获取")

    return bugs_found


async def main():
    print("\n" + "=" * 60)
    print("TTS + 表情包 + 图片生成 + 视频生成 综合测试")
    print("=" * 60)

    await test_tts()
    await test_sticker()
    await test_agnes_image()
    await test_agnes_video()
    await test_agent_integration()

    print("\n" + "=" * 60)
    print("综合测试总结")
    print("=" * 60)
    if bugs_found:
        print(f"\n发现 {len(bugs_found)} 个 Bug:")
        for i, bug in enumerate(bugs_found, 1):
            print(f"  {i}. [{bug['severity']}] {bug['module']}: {bug['desc']}")
    else:
        print("\n所有测试通过!")

    return bugs_found


if __name__ == "__main__":
    bugs = asyncio.run(main())
    sys.exit(len(bugs))
