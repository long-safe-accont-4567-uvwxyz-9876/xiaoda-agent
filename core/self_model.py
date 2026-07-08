"""持续身份模型 —— 让 agent 拥有连续的自我概念。

加载 ~/.ai-agent/self_model.md，注入到 prompt 的 Stable 层，
让 agent 每次对话都"记得自己是谁"。

文件可被 agent 自己通过 save_memory 工具更新，形成成长轨迹。
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from loguru import logger


_SELF_MODEL_PATH = Path(
    os.getenv("SELF_MODEL_PATH", str(Path.home() / ".ai-agent" / "self_model.md"))
)

# Token 预算：自我模型最多注入 ~400 token（约 1600 字符）
_MAX_CHARS = 1600

_cache: dict[str, object] = {"content": "", "mtime": 0.0}


def _refresh_if_stale() -> None:
    """检测文件变更并重载缓存。"""
    try:
        mtime = _SELF_MODEL_PATH.stat().st_mtime
        if mtime != _cache["mtime"]:
            content = _SELF_MODEL_PATH.read_text(encoding="utf-8")
            if len(content) > _MAX_CHARS:
                # 超限时按行截断，保留前面的核心部分
                lines = content.split("\n")
                truncated = []
                total = 0
                for line in lines:
                    if total + len(line) + 1 > _MAX_CHARS:
                        break
                    truncated.append(line)
                    total += len(line) + 1
                content = "\n".join(truncated)
                logger.debug("self_model.truncated",
                             original=len(_SELF_MODEL_PATH.read_text(encoding="utf-8")),
                             truncated=len(content))
            _cache["content"] = content
            _cache["mtime"] = mtime
            logger.debug("self_model.reloaded", chars=len(content))
    except FileNotFoundError:
        _cache["content"] = ""
        _cache["mtime"] = 0.0
    except Exception as e:
        logger.debug("self_model.refresh_failed", error=str(e))


def get_self_model() -> str:
    """获取自我模型文本（用于注入 Stable 层）。

    返回带标签的文本，如：
    [自我概念]
    # 小妲的自我模型
    ...
    """
    _refresh_if_stale()
    content = _cache.get("content", "")
    if not content:
        return ""
    return f"[自我概念]\n{content}"


def append_growth_entry(entry: str, section: str = "## 我的成长轨迹") -> bool:
    """在自我模型的指定章节追加一条记录（用于 agent 自我更新）。

    Args:
        entry: 要追加的内容（如 "2026-07-03：学会了 QQ群聊分片技巧"）
        section: 章节标题（默认"我的成长轨迹"）

    Returns:
        True 表示成功
    """
    try:
        content = _SELF_MODEL_PATH.read_text(encoding="utf-8") if _SELF_MODEL_PATH.exists() else ""
        timestamp = time.strftime("%Y-%m-%d", time.localtime())
        line = f"- {timestamp}：{entry}"

        # 查找章节位置
        section_marker = f"{section}"
        idx = content.find(section_marker)
        if idx == -1:
            # 章节不存在，追加到末尾
            if content and not content.endswith("\n"):
                content += "\n"
            content += f"\n{section}\n{line}\n"
        else:
            # 在章节标题后插入
            insert_pos = idx + len(section_marker)
            # 找到下一行开始
            while insert_pos < len(content) and content[insert_pos] != "\n":
                insert_pos += 1
            insert_pos += 1  # 跳过换行
            content = content[:insert_pos] + line + "\n" + content[insert_pos:]

        _SELF_MODEL_PATH.write_text(content, encoding="utf-8")
        # 失效缓存，下次读取时重载
        _cache["mtime"] = 0.0
        logger.info("self_model.growth_appended", entry=entry[:50])
        return True
    except Exception as e:
        logger.warning("self_model.append_failed", error=str(e))
        return False


def get_growth_entries() -> list[str]:
    """读取成长轨迹条目（用于回顾）。"""
    _refresh_if_stale()
    content = _cache.get("content", "")
    entries = []
    in_section = False
    for line in content.split("\n"):
        if line.startswith("## 我的成长轨迹"):
            in_section = True
            continue
        if in_section:
            if line.startswith("## "):
                break
            if line.strip().startswith("- "):
                entries.append(line.strip()[2:])
    return entries
