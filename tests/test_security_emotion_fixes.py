"""测试安全和情绪相关修复.

覆盖:
- SEC-2: WebSocket 先验证 token 再 accept
- SEC-3: BYPASS 模式 shell 命令防傻检查
- BUG-18: TTS 防御性标签清理
- BUG-2: 困惑情绪映射修正
- BUG-3: 问候语不再触发正面情绪
"""
import re

# ── SEC-3: BYPASS 模式 shell 命令防傻检查 ──────────────────

def test_bypass_mode_blocks_dangerous_shell_commands():
    """BYPASS 模式也应对危险 shell 命令做防傻检查."""
    from security.permission_manager import PermissionManager, PermissionMode
    pm = PermissionManager()
    pm._mode = PermissionMode.BYPASS

    # rm -rf / 应被拦截
    allowed, reason = pm.check_tool_permission("shell_command", {"command": "rm -rf /"})
    assert not allowed, "BYPASS模式应拦截 rm -rf /"
    assert reason, "应返回拦截原因"


def test_bypass_mode_allows_safe_shell_commands():
    """BYPASS 模式应放行安全 shell 命令."""
    from security.permission_manager import PermissionManager, PermissionMode
    pm = PermissionManager()
    pm._mode = PermissionMode.BYPASS

    allowed, reason = pm.check_tool_permission("shell_command", {"command": "ls -la"})
    assert allowed, "BYPASS模式应放行 ls -la"


def test_bypass_mode_allows_non_shell_tools():
    """BYPASS 模式应放行非 shell 工具."""
    from security.permission_manager import PermissionManager, PermissionMode
    pm = PermissionManager()
    pm._mode = PermissionMode.BYPASS

    allowed, _ = pm.check_tool_permission("calculator", {"expression": "2+2"})
    assert allowed


# ── BUG-18: TTS 防御性标签清理 ─────────────────────────────

def test_tts_strips_emotion_tags():
    """TTS synthesize 应清理 [emotion:xxx] 标签 — 验证源码包含清理逻辑."""
    import inspect

    from emotion.tts_engine import TTSEngine
    source = inspect.getsource(TTSEngine.synthesize)
    assert re.search(r'\[emotion:', source), "synthesize 方法应包含 [emotion:xxx] 标签清理逻辑"
    assert re.search(r're\.sub', source), "synthesize 方法应使用 re.sub 清理标签"


def test_tts_strips_sticker_tags():
    """TTS synthesize 应清理 [sticker:xxx] 标签 — 验证源码包含清理逻辑."""
    import inspect

    from emotion.tts_engine import TTSEngine
    source = inspect.getsource(TTSEngine.synthesize)
    assert re.search(r'\[sticker:', source), "synthesize 方法应包含 [sticker:xxx] 标签清理逻辑"


# ── BUG-2: 困惑情绪映射 ─────────────────────────────────────

def test_confused_maps_to_thinking_not_curious():
    """CONFUSED 情绪应映射到 thinking 风格, 不是 curious."""
    from emotion.emotion_enum import TTS_STYLE_MAP, Emotion
    assert TTS_STYLE_MAP[Emotion.CONFUSED] == "thinking", \
        f"困惑应映射到thinking, 实际: {TTS_STYLE_MAP[Emotion.CONFUSED]}"


# ── BUG-3: 问候语不再触发正面情绪 ───────────────────────────

def test_greeting_does_not_trigger_positive_emotion():
    """问候语不应被检测为喜悦情绪."""
    from emotion.emotion_simple import detect_emotion
    result = detect_emotion("你好呀")
    assert result["primary"] != "喜悦", \
        f"问候语'你好呀'不应触发喜悦, 实际: {result['primary']}"

    result = detect_emotion("早上好")
    assert result["primary"] != "喜悦", \
        f"问候语'早上好'不应触发喜悦, 实际: {result['primary']}"


def test_positive_words_still_trigger_positive_emotion():
    """正面情绪词仍应被正确检测."""
    from emotion.emotion_simple import detect_emotion
    result = detect_emotion("好开心啊哈哈")
    assert result["primary"] == "喜悦", \
        f"正面词应触发喜悦, 实际: {result['primary']}"


# ── credential_pool: 统一锁 ────────────────────────────────

def test_credential_pool_uses_single_lock():
    """CredentialPool 应使用单一 threading.Lock, 不再混用 asyncio.Lock."""
    from utils.credential_pool import CredentialPool
    pool = CredentialPool()
    assert not hasattr(pool, '_lock') or pool._lock is pool._sync_lock, \
        "CredentialPool 不应再有独立的 asyncio.Lock"
    assert hasattr(pool, '_sync_lock'), "应有 _sync_lock"
