"""测试 get_ack_message() 的核心逻辑。"""
import json

import pytest

from emotion.emoji_config import get_ack_message


@pytest.fixture
def tmp_agent_config(tmp_path):
    """创建临时 agent JSON 配置文件。"""
    import config
    original_dir = config.AGENTS_CONFIG_DIR
    config.AGENTS_CONFIG_DIR = tmp_path
    yield tmp_path
    config.AGENTS_CONFIG_DIR = original_dir


def test_default_ack_when_not_configured(tmp_agent_config):
    """未配置 ack_messages → 返回默认。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({"name": "xiaoda", "display_name": "小妲"}), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert "收到啦" in msg
    assert "小妲" in msg


def test_default_ack_when_empty_list(tmp_agent_config):
    """ack_messages 为空列表 → 返回默认。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({"display_name": "小妲", "ack_messages": []}), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert "收到啦" in msg


def test_single_ack_message(tmp_agent_config):
    """配置1条 → 返回该条。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({
        "display_name": "小妲",
        "ack_messages": ["嗯嗯，小妲在听呢～💭"]
    }), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert msg == "嗯嗯，小妲在听呢～💭"


def test_multiple_ack_messages_random(tmp_agent_config):
    """配置多条 → 随机返回其中一条。"""
    fp = tmp_agent_config / "xiaoda.json"
    messages = ["第一条～🌿", "第二条～🌱", "第三条～💭"]
    fp.write_text(json.dumps({
        "display_name": "小妲",
        "ack_messages": messages
    }), encoding="utf-8")
    results = {get_ack_message("xiaoda") for _ in range(50)}
    assert results.issubset(set(messages))
    assert len(results) >= 1


def test_name_replacement_deprecated_name(tmp_agent_config):
    """包含旧名（纳西妲）→ 替换为当前 display_name。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({
        "display_name": "小妲",
        "deprecated_names": ["纳西妲"],
        "ack_messages": ["纳西妲收到啦～🌿"]
    }), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert "小妲" in msg
    assert "纳西妲" not in msg


def test_name_replacement_agent_key(tmp_agent_config):
    """包含 agent key（xiaoda）→ 替换为 display_name。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({
        "display_name": "小妲",
        "ack_messages": ["xiaoda收到啦～🌿"]
    }), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert "小妲" in msg
    assert "xiaoda" not in msg


def test_no_name_silent_passthrough(tmp_agent_config):
    """不包含任何 agent 名称标识 → 原样输出（静默降级）。"""
    fp = tmp_agent_config / "xiaoda.json"
    fp.write_text(json.dumps({
        "display_name": "小妲",
        "ack_messages": ["收到收到～让我想想哦🌱"]
    }), encoding="utf-8")
    msg = get_ack_message("xiaoda")
    assert msg == "收到收到～让我想想哦🌱"


def test_missing_agent_json(tmp_agent_config):
    """agent JSON 文件不存在 → 返回默认。"""
    msg = get_ack_message("nonexistent_agent")
    assert "收到啦" in msg
