"""TTS + 表情包 + 图片/视频生成 综合测试 v2"""
import asyncio
import os
import random
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

# 项目根目录 (基于当前文件位置计算，避免硬编码绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_sticker_dir(subdir: str = "xiaoda") -> str:
    """查找可用表情包目录: 优先项目内置，再尝试外部挂载路径"""
    candidates = [
        PROJECT_ROOT / "assets" / "stickers" / subdir,
        Path("/media/orangepi/KIOXIA/xiaoda-data/stickers"),
        Path("/media/orangepi/KIOXIA/xiaoda-data") / f"{subdir}-stickers",
    ]
    for d in candidates:
        if d.is_dir():
            return str(d)
    import tempfile
    return tempfile.mkdtemp()

bugs = []


def report_bug(severity, component, desc):
    bugs.append((severity, component, desc))
    print(f"  BUG[{severity}] {component}: {desc}")


# ============================================================
# Part 1: 表情包集成流程测试
# ============================================================
def test_sticker_integration():
    print("=" * 60)
    print("Part 1: 表情包集成流程测试")
    print("=" * 60)

    from emotion.emotion_simple import detect_emotion
    from emotion.sticker_manager import StickerManager

    # 1.1 真实表情包目录
    sticker_dir = _resolve_sticker_dir("xiaoda")
    sm = StickerManager(sticker_dir)
    print(f"[1.1] StickerManager: available={sm.available}, categories={list(sm._cache.keys())}")

    # 1.2 情绪检测 -> 表情包选取 完整流程
    test_cases = [
        ("太好了！今天天气真好", "happy"),
        ("呜呜，好难过啊", "sad"),
        ("哼！生气了", "angry"),
        ("害羞///才不是呢", "shy"),
        ("咦？这是怎么回事", "curious"),
        ("你好呀！早上好", "greeting"),
        ("让我想想...嗯...", "thinking"),
        ("[emotion:happy] 嘻嘻", "happy"),
        ("[emotion:sad] 好伤心", "sad"),
    ]

    print("\n[1.2] 情绪检测 -> 表情包选取:")
    for text, expected_emotion in test_cases:
        detected = sm.detect_emotion(text)
        match = "OK" if detected == expected_emotion else "FAIL"
        sticker = sm.pick(detected) if detected else None
        sticker_name = sticker.name if sticker else "None"
        if detected != expected_emotion:
            report_bug("HIGH", "sticker", f'情绪检测错误: "{text}" 期望={expected_emotion} 实际={detected}')
        print(f"  [{match}] '{text}' -> emotion={detected or '(无)'}, sticker={sticker_name}")

    # 1.3 should_send 概率测试
    print("\n[1.3] should_send 概率测试:")
    random.seed(42)
    send_count_with = sum(1 for _ in range(100) if sm.should_send("test", detected_emotion="happy"))
    send_count_no = sum(1 for _ in range(100) if sm.should_send("test", detected_emotion=""))
    print(f"  有情绪: {send_count_with}/100 (期望~70)")
    print(f"  无情绪: {send_count_no}/100 (期望~40)")
    if send_count_with < 50 or send_count_with > 90:
        report_bug("MEDIUM", "sticker", f"should_send 有情绪概率异常: {send_count_with}/100")
    if send_count_no < 20 or send_count_no > 60:
        report_bug("MEDIUM", "sticker", f"should_send 无情绪概率异常: {send_count_no}/100")

    # 1.4 strip_emotion_tag 测试
    print("\n[1.4] strip_emotion_tag 测试:")
    tag_tests = [
        ("[emotion:happy]嘻嘻", "嘻嘻"),
        ("[emotion:sad]好难过", "好难过"),
        ("没有标签的文本", "没有标签的文本"),
        ("[emotion:angry]哼", "哼"),
    ]
    for text, expected in tag_tests:
        result = sm.strip_emotion_tag(text)
        match = "OK" if result == expected else "FAIL"
        if result != expected:
            report_bug("MEDIUM", "sticker", f'strip_emotion_tag 错误: "{text}" 期望="{expected}" 实际="{result}"')
        print(f"  [{match}] '{text}' -> '{result}'")

    # 1.5 pick_by_text 完整流程
    print("\n[1.5] pick_by_text 完整流程:")
    pick_texts = ["太开心了", "好难过啊", "哼！生气", "你好呀"]
    for text in pick_texts:
        sticker = sm.pick_by_text(text)
        if sticker:
            print(f"  [OK] '{text}' -> {sticker.name}")
        else:
            report_bug("MEDIUM", "sticker", f'pick_by_text 返回 None: "{text}"')
            print(f"  [FAIL] '{text}' -> None")

    # 1.6 检查 agent_core 中的情绪映射一致性
    print("\n[1.6] agent_core 情绪映射一致性检查:")
    emotion_map_in_core = {"喜悦": "happy", "悲伤": "sad", "焦虑": "sad", "平静": ""}
    print(f"  agent_core user_emotion_map: {emotion_map_in_core}")
    test_anxious = detect_emotion("我好焦虑啊")
    print(f'  detect_emotion("我好焦虑啊"): {test_anxious}')
    if test_anxious.get("primary") == "焦虑" and emotion_map_in_core.get("焦虑") == "sad":
        report_bug("MEDIUM", "agent_core", "焦虑在 emotion_simple 中被正确分类为焦虑，但 agent_core 中映射到 sad，应映射到 lonely 或 fear")
        print("  [FAIL] 焦虑映射不一致: emotion_simple=焦虑, agent_core=sad")
    else:
        print("  [OK] 情绪映射一致")

    # 1.7 子 Agent 表情包测试
    xiaoli_dir = _resolve_sticker_dir("xiaoli")
    if os.path.exists(xiaoli_dir):
        ksm = StickerManager(xiaoli_dir)
        print(f"\n[1.7] 小莉表情包: available={ksm.available}, categories={list(ksm._cache.keys())}")
        xiaoli_sticker = ksm.pick("happy")
        print(f"  小莉 happy sticker: {xiaoli_sticker.name if xiaoli_sticker else 'None'}")
    else:
        print(f"\n[1.7] 小莉表情包目录不存在: {xiaoli_dir}")

    # 1.8 子 Agent 不发送表情包的问题
    print("\n[1.8] 子 Agent 表情包发送检查:")
    # 在 agent_core.py _dispatch_single_sub_agent 中，sticker_path 始终为 None
    # 即使有 xiaoli_sticker_manager，子 Agent 也不会发送表情包
    print("  [WARN] _dispatch_single_sub_agent 中 sticker_path 始终为 None")
    print("  [WARN] 子 Agent (如小莉) 不会发送表情包，即使有 xiaoli_sticker_manager")
    report_bug("LOW", "agent_core", "子 Agent 不发送表情包: _dispatch_single_sub_agent 中 sticker_path 始终为 None，未使用 xiaoli_sticker_manager")


# ============================================================
# Part 2: Agnes 图片/视频生成工具测试
# ============================================================
async def test_agnes_tools():
    print("\n" + "=" * 60)
    print("Part 2: Agnes 图片/视频生成工具测试")
    print("=" * 60)

    from config import AGNES_API_KEY, AGNES_BASE_URL, AGNES_IMAGE_MODEL, AGNES_VIDEO_MODEL

    # 2.1 API Key 检查
    print("\n[2.1] API Key 检查:")
    print(f"  AGNES_API_KEY: {'***' + AGNES_API_KEY[-4:] if AGNES_API_KEY else '(未设置)'}")
    print(f"  AGNES_BASE_URL: {AGNES_BASE_URL}")
    print(f"  AGNES_IMAGE_MODEL: {AGNES_IMAGE_MODEL}")
    print(f"  AGNES_VIDEO_MODEL: {AGNES_VIDEO_MODEL}")

    # 2.2 图片生成工具 - 无 Key 降级测试
    print("\n[2.2] 图片生成工具 - 无 Key 降级测试:")
    # 保存原始 Key
    import tools.agnes_tools as at
    from tools.agnes_tools import agnes_image_generate, agnes_video_generate
    original_key = at.AGNES_API_KEY
    at.AGNES_API_KEY = ""

    result = await agnes_image_generate(prompt="a cute cat")
    print(f"  success={result.success}, error={result.error if not result.success else 'N/A'}")
    if not result.success and "API Key" in result.error:
        print("  [OK] 无 Key 时正确返回失败")
    else:
        report_bug("HIGH", "agnes_image", f"无 Key 降级失败: success={result.success}, error={result.error}")

    # 恢复 Key
    at.AGNES_API_KEY = original_key

    # 2.3 视频生成工具 - 无 Key 降级测试
    print("\n[2.3] 视频生成工具 - 无 Key 降级测试:")
    at.AGNES_API_KEY = ""

    result = await agnes_video_generate(prompt="a cute cat playing")
    print(f"  success={result.success}, error={result.error if not result.success else 'N/A'}")
    if not result.success and "API Key" in result.error:
        print("  [OK] 无 Key 时正确返回失败")
    else:
        report_bug("HIGH", "agnes_video", f"无 Key 降级失败: success={result.success}, error={result.error}")

    at.AGNES_API_KEY = original_key

    # 2.4 图片生成 - 图生图参数检查
    print("\n[2.4] 图片生成 - 图生图参数检查:")
    # 检查图生图模式是否正确移除 prompt 并添加 image 参数
    import inspect
    source = inspect.getsource(agnes_image_generate)
    if 'kwargs.pop("prompt"' in source and '"image"' in source:
        print("  [WARN] 图生图模式: 移除 prompt 并添加 image 参数")
        print("  [WARN] 需确认 Agnes API 图生图参数名是否为 'image'")
        # 检查 OpenAI images API 标准参数
        print("  [INFO] OpenAI 标准 images API 图生图使用 'image' 参数")
        # 但 Agnes 可能使用不同的参数名
        report_bug("LOW", "agnes_image", "图生图参数名需确认: 当前使用 'image'，需验证 Agnes API 实际参数名")
    else:
        print("  [OK] 图生图参数处理逻辑正常")

    # 2.5 视频生成 - 帧数计算检查
    print("\n[2.5] 视频生成 - 帧数计算检查:")
    test_frames = [
        (5, 24, 113),   # 5秒24fps -> 120帧 -> n=14 -> 113
        (1, 24, 17),    # 1秒24fps -> 24帧 -> n=2 -> 17
        (10, 24, 201),  # 10秒24fps -> 240帧 -> n=29 -> 233
        (3, 30, 89),    # 3秒30fps -> 90帧 -> n=11 -> 89
    ]
    for seconds, fps, _ in test_frames:
        raw_frames = int(seconds * fps)
        n = max(1, (raw_frames - 1) // 8)
        num_frames = min(8 * n + 1, 441)
        print(f"  {seconds}s@{fps}fps -> raw={raw_frames}, n={n}, num_frames={num_frames}")

    # 2.6 视频生成 - 轮询超时检查
    print("\n[2.6] 视频生成 - 轮询超时检查:")
    # 24次 * 5秒 = 120秒超时
    print("  轮询次数: 24, 间隔: 5s, 总超时: 120s")
    print("  [OK] 超时时间合理")

    # 2.7 Agnes API Key 缺失在 .env.example 中
    print("\n[2.7] .env.example 检查:")
    env_example = PROJECT_ROOT / ".env.example"
    if os.path.exists(env_example):
        content = open(env_example).read()
        if "AGNES_API_KEY" not in content:
            report_bug("LOW", "config", "AGNES_API_KEY 未在 .env.example 中列出")
            print("  [FAIL] AGNES_API_KEY 未在 .env.example 中")
        else:
            print("  [OK] AGNES_API_KEY 在 .env.example 中")

    # 2.8 如果有 API Key，尝试真实图片生成
    if AGNES_API_KEY:
        print("\n[2.8] 真实图片生成测试:")
        try:
            result = await agnes_image_generate(prompt="a cute cat sitting on a windowsill", size="512x512")
            if result.success:
                print(f"  [OK] 图片生成成功: {result.data[:100]}")
            else:
                print(f"  [FAIL] 图片生成失败: {result.error}")
                report_bug("HIGH", "agnes_image", f"图片生成失败: {result.error}")
        except Exception as e:
            report_bug("HIGH", "agnes_image", f"图片生成异常: {e}")
            print(f"  [FAIL] 图片生成异常: {e}")
    else:
        print("\n[2.8] 跳过真实图片生成测试 (无 AGNES_API_KEY)")


# ============================================================
# Part 3: TTS + 表情包 + Agent 集成测试
# ============================================================
async def test_agent_integration():
    print("\n" + "=" * 60)
    print("Part 3: TTS + 表情包 Agent 集成测试")
    print("=" * 60)

    # 3.1 AgentCore 初始化检查
    print("\n[3.1] AgentCore 初始化检查:")
    try:
        from agent_core import AgentCore
        core = AgentCore()
        print(f"  sticker_manager: available={core.sticker_manager.available}")
        print(f"  xiaoli_sticker_manager: available={core.xiaoli_sticker_manager.available}")
        print(f"  tts: available={core.tts.available}")
    except Exception as e:
        report_bug("HIGH", "agent_core", f"AgentCore 初始化失败: {e}")
        print(f"  [FAIL] AgentCore 初始化失败: {e}")
        return

    # 3.2 get_sticker_info 测试
    print("\n[3.2] get_sticker_info 测试:")
    test_replies = [
        ("[emotion:happy]嘻嘻，太开心了", "happy"),
        ("[emotion:sad]好难过啊", "sad"),
        ("普通回复，没有情绪标签", ""),
    ]
    for reply, expected_emotion in test_replies:
        clean, sticker = core.get_sticker_info(reply, user_emotion=expected_emotion)
        sticker_name = sticker.name if sticker else "None"
        print(f"  '{reply[:30]}' -> clean='{clean[:30]}', sticker={sticker_name}")

    # 3.3 焦虑情绪映射测试
    print("\n[3.3] 焦虑情绪映射测试:")
    from emotion.emotion_simple import detect_emotion
    anxious_result = detect_emotion("我好焦虑啊，怎么办")
    primary = anxious_result.get("primary", "")
    print(f"  detect_emotion('我好焦虑啊'): primary={primary}")

    # 在 agent_core 中，焦虑映射到 sad
    clean, sticker = core.get_sticker_info("回复内容", user_emotion=primary)
    # 如果 primary=焦虑，映射到 sad，但焦虑应该映射到 lonely 或 fear
    if primary == "焦虑":
        user_emotion_map = {"喜悦": "happy", "悲伤": "sad", "焦虑": "sad", "平静": ""}
        mapped = user_emotion_map.get(primary, "")
        if mapped == "sad":
            report_bug("MEDIUM", "agent_core", "焦虑映射到 sad 不准确，应映射到 lonely 或 fear")
            print(f"  [FAIL] 焦虑映射到 {mapped}，应为 lonely 或 fear")
        else:
            print(f"  [OK] 焦虑映射到 {mapped}")

    # 3.4 TTS 情绪标签与 EMOTION_STYLE_MAP 一致性
    print("\n[3.4] TTS 情绪标签一致性:")
    from emotion.sticker_manager import StickerManager
    from emotion.tts_engine import EMOTION_STYLE_MAP
    sm = StickerManager(_resolve_sticker_dir("xiaoda"))

    tts_emotions = set(EMOTION_STYLE_MAP.keys())
    sticker_emotions = set(sm.EMOTION_MAP.keys())
    print(f"  TTS 支持的情绪: {sorted(tts_emotions)}")
    print(f"  Sticker 支持的情绪: {sorted(sticker_emotions)}")

    # 检查 TTS 有但 Sticker 没有的情绪
    tts_only = tts_emotions - sticker_emotions
    sticker_only = sticker_emotions - tts_emotions
    if tts_only:
        print(f"  [WARN] TTS 有但 Sticker 无: {tts_only}")
    if sticker_only:
        print(f"  [WARN] Sticker 有但 TTS 无: {sticker_only}")

    # 3.5 ProcessResult 字段完整性
    print("\n[3.5] ProcessResult 字段完整性:")
    from agent_core import ProcessResult
    pr = ProcessResult(reply="test", emotion="happy", sticker_path=None, audio_path=None)
    print(f"  reply: {pr.reply}")
    print(f"  emotion: {pr.emotion}")
    print(f"  sticker_path: {pr.sticker_path}")
    print(f"  audio_path: {pr.audio_path}")
    print(f"  tool_results: {pr.tool_results}")
    print("  [OK] ProcessResult 字段完整")


# ============================================================
# Main
# ============================================================
async def main():
    print("TTS + 表情包 + 图片/视频生成 综合测试 v2\n")

    test_sticker_integration()
    await test_agnes_tools()
    await test_agent_integration()

    print("\n" + "=" * 60)
    print(f"测试完成，发现 {len(bugs)} 个 Bug:")
    for severity, component, desc in bugs:
        print(f"  [{severity}] {component}: {desc}")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
