"""验证 Agent 能力感知"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dotenv import load_dotenv

load_dotenv()

# 项目根目录 (基于当前文件位置计算，避免硬编码绝对路径)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

print("=== Agent 能力感知验证 ===\n")

# 1. 系统提示包含新能力
from config import build_system_prompt

prompt = build_system_prompt()
checks = [
    ("语音合成", "语音合成" in prompt or "TTS" in prompt),
    ("表情包", "表情包" in prompt),
    ("图片生成", "图片生成" in prompt or "agnes_image" in prompt),
    ("视频生成", "视频生成" in prompt or "agnes_video" in prompt),
    ("lonely 情绪", "lonely" in prompt),
    ("playful 情绪", "playful" in prompt),
    ("surprised 情绪", "surprised" in prompt),
    ("fear 情绪", "fear" in prompt),
    ("AI 创作工具", "AI 创作工具" in prompt or "AI 创作能力" in prompt),
    ("语音能力", "语音能力" in prompt),
]
print("[1] 系统提示能力检查:")
ok = 0
for name, result in checks:
    print(f"  {'OK' if result else 'FAIL'}: {name}")
    if result:
        ok += 1
print(f"  通过: {ok}/{len(checks)}")

# 2. 工具注册
from tool_engine.tool_registry import to_openai_tools

tools = to_openai_tools()
tool_names = [t["function"]["name"] for t in tools]
print(f"\n[2] 已注册工具 ({len(tool_names)} 个):")
creative_tools = ["agnes_image_generate", "agnes_video_generate"]
for t in creative_tools:
    print(f"  {'OK' if t in tool_names else 'FAIL'}: {t}")

# 3. 工具描述
for t in tools:
    name = t["function"]["name"]
    if name in creative_tools:
        desc = t["function"]["description"]
        params = list(t["function"]["parameters"].get("properties", {}).keys())
        print(f"  {name}: {desc[:60]}")
        print(f"    参数: {params}")

# 4. IDENTITY.md
identity = open(PROJECT_ROOT / "config" / "workspace" / "IDENTITY.md").read()
identity_checks = [
    ("语音合成", "语音合成" in identity),
    ("AI 图片生成", "AI 图片生成" in identity),
    ("AI 视频生成", "AI 视频生成" in identity),
    ("智能表情包", "智能表情包" in identity),
    ("语音与多媒体", "语音与多媒体" in identity),
]
print("\n[3] IDENTITY.md 更新检查:")
for name, result in identity_checks:
    print(f"  {'OK' if result else 'FAIL'}: {name}")

# 5. TOOLS.md
tools_md = open(PROJECT_ROOT / "config" / "workspace" / "TOOLS.md").read()
tools_checks = [
    ("AI 创作工具", "AI 创作工具" in tools_md),
    ("agnes_image_generate", "agnes_image_generate" in tools_md),
    ("agnes_video_generate", "agnes_video_generate" in tools_md),
    ("语音合成", "语音合成" in tools_md),
    ("表情包发送", "表情包发送" in tools_md),
    ("使用场景", "使用场景" in tools_md),
]
print("\n[4] TOOLS.md 更新检查:")
for name, result in tools_checks:
    print(f"  {'OK' if result else 'FAIL'}: {name}")

# 6. SOUL.md
soul_md = open(PROJECT_ROOT / "config" / "workspace" / "SOUL.md").read()
soul_checks = [
    ("lonely 情绪", "lonely" in soul_md),
    ("playful 情绪", "playful" in soul_md),
    ("surprised 情绪", "surprised" in soul_md),
    ("fear 情绪", "fear" in soul_md),
    ("语音能力", "语音能力" in soul_md),
    ("AI 创作能力", "AI 创作能力" in soul_md),
    ("图片生成指引", "图片生成" in soul_md),
    ("视频生成指引", "视频生成" in soul_md),
]
print("\n[5] SOUL.md 更新检查:")
for name, result in soul_checks:
    print(f"  {'OK' if result else 'FAIL'}: {name}")

# 7. 模拟用户命令 -> 工具调用映射
print("\n[6] 用户命令 -> 工具调用映射:")
from agent_core import AgentCore

core = AgentCore()
mappings = [
    ("画一张猫", "agnes_image_generate"),
    ("生成图片", "agnes_image_generate"),
    ("做个视频", "agnes_video_generate"),
    ("生成视频", "agnes_video_generate"),
    ("发语音", "TTS (内置)"),
    ("听你说", "TTS (内置)"),
]
for cmd, tool in mappings:
    print(f"  '{cmd}' -> {tool}")

# 8. 简单任务判断 - 确保创作工具不被过滤
print("\n[7] 简单任务过滤检查:")
simple = core._is_simple_task("画一张猫")
print(f"  '画一张猫' 是简单任务: {simple} (应为 False)")
simple2 = core._is_simple_task("生成视频")
print(f"  '生成视频' 是简单任务: {simple2} (应为 False)")

print("\n=== 验证完成 ===")
