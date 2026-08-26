"""情绪分类学契约测试

2026-08 review 根因：提示词列表（11 种，含非法 lonely）与表情包分类（17 种）
长期脱节却无任何测试锁定。本文件把「枚举 ↔ 提示词 ↔ web 分类 ↔ 物理目录 ↔
下游派生表」全部锁在单一事实源 emotion_enum.Emotion 上，
任何一侧漂移都会在此红灯。
"""
import re
from pathlib import Path

import pytest

from emotion.emotion_enum import (
    CN_TO_EN,
    EMOTION_TAG_GUIDE,
    EMOTION_VOCAB_SLASH,
    TTS_STYLE_VALUES,
    VALID_EMOTION_TAGS,
)
from emotion.pad_model import EMOTION_PAD_REFERENCE
from memory.emotional_memory import CN_TO_EN_MAP, EN_TO_CN_PAD

_REPO = Path(__file__).resolve().parent.parent
_HOME_CFG = Path.home() / ".ai-agent" / "config"

# ── 1. 枚举内部一致性 ────────────────────────────────────────


class TestEnumInternals:
    def test_guide_covers_exactly_the_enum(self):
        """EMOTION_TAG_GUIDE 键集 == 枚举值集（动态规则块的完整性前提）"""
        assert set(EMOTION_TAG_GUIDE) == VALID_EMOTION_TAGS

    def test_cn_to_en_is_canonical_bijective(self):
        """CN_TO_EN 17 项、英文值互异且全覆盖——EN_TO_CN_PAD 反转推导的前提"""
        assert len(CN_TO_EN) == len(VALID_EMOTION_TAGS)
        assert set(CN_TO_EN.values()) == VALID_EMOTION_TAGS

    def test_tts_style_values_subset_of_known_styles(self):
        """TTS 风格值必须落在 tts_engine.EMOTION_STYLE_MAP 实际键内（防拼错）"""
        src = (_REPO / "emotion" / "tts_engine.py").read_text(encoding="utf-8")
        block = re.search(r"EMOTION_STYLE_MAP\s*=\s*\{(.*?)\n\}", src, re.S).group(1)
        real_keys = set(re.findall(r'^\s{4}"(\w+)":', block, re.M))
        assert TTS_STYLE_VALUES <= real_keys, TTS_STYLE_VALUES - real_keys


# ── 2. 下游派生表与 PAD 参考键对齐 ───────────────────────────


class TestDerivedMaps:
    def test_en_to_cn_pad_hits_pad_reference(self):
        """EN_TO_CN_PAD 每个反查结果都是 EMOTION_PAD_REFERENCE 的键
        （review #4：曾因 喜欢/喜爱 键位错位导致 LOVE 静默降级 neutral）"""
        misses = [en for en, cn in EN_TO_CN_PAD.items() if cn not in EMOTION_PAD_REFERENCE]
        assert not misses, f"PAD 反查缺键: {misses}"

    def test_pad_reference_key_set_matches_canonical_cn(self):
        """PAD 参考表键集 == 规范中文标签集（CN_TO_EN 的键）"""
        assert set(EMOTION_PAD_REFERENCE) == set(CN_TO_EN)

    def test_legacy_variant_labels_still_resolve(self):
        """存量记忆可能存旧标签（喜欢），归一化链路不得回归"""
        assert CN_TO_EN_MAP["喜欢"] == "love"
        assert EN_TO_CN_PAD["love"] in EMOTION_PAD_REFERENCE


# ── 3. 提示词层：代码内动态规则 + 人格文件 ────────────────────


def _parse_md_emotion_tags(text: str) -> set[str]:
    m = re.search(r"\[emotion:xxx\]。xxx 为以下之一：\n\n(.*?)\n\n规则", text, re.S)
    if m is None:
        return set()
    return set(re.findall(r"^- (\w+) —", m.group(1), re.M))


class TestPromptLayer:
    def test_sub_agent_rule_generated_from_enum(self):
        """@ 子代理注入的硬性规则必须含全部 17 标签、无非法 lonely"""
        from agent_core.sub_agent_manager import _SUB_AGENT_EMOTION_RULE as rule
        tags = set(re.findall(r"^- (\w+) — ", rule, re.M))
        assert tags == VALID_EMOTION_TAGS, tags ^ VALID_EMOTION_TAGS
        assert "lonely" not in rule

    def test_per_agent_rule_filtered_to_sticker_dirs(self, monkeypatch, tmp_path):
        """规则按代理物理目录 ∩ 枚举裁剪（strict 模式出图率前提）"""
        from emotion.sticker_manager import StickerManager

        mgr = StickerManager(tmp_path)
        (tmp_path / "happy").mkdir()
        (tmp_path / "sad").mkdir()
        (tmp_path / "not_an_emotion").mkdir()  # 非法目录名应被 ∩ 枚举过滤
        for d in ("happy", "sad", "not_an_emotion"):
            (tmp_path / d / "a.png").write_bytes(b"x")
        mgr.reload()

        import agent_core.sub_agent_manager as sam

        class FakeCore:
            get_sticker_manager = lambda self, name: mgr  # noqa: E731

        rule = sam.SubAgentManagerMixin._sub_agent_emotion_rule(FakeCore(), "xiaoli")
        tags = set(re.findall(r"^- (\w+) — ", rule, re.M))
        assert tags == {"happy", "sad"}, tags

    def test_per_agent_rule_falls_back_to_full_enum(self, tmp_path):
        """表情包目录缺失/为空时退回全量枚举，保证规则非空"""
        import agent_core.sub_agent_manager as sam
        from emotion.sticker_manager import StickerManager

        empty_mgr = StickerManager(tmp_path / "nonexistent")

        class FakeCore:
            get_sticker_manager = lambda self, name: empty_mgr  # noqa: E731

        rule = sam.SubAgentManagerMixin._sub_agent_emotion_rule(FakeCore(), "ghost")
        tags = set(re.findall(r"^- (\w+) — ", rule, re.M))
        assert tags == VALID_EMOTION_TAGS

    @pytest.mark.parametrize("name", ["xiaoke", "xiaolang", "xiaolian", "xiaoli"])
    def test_repo_agent_personality_files_match_enum(self, name):
        """仓库副本 config/agents/*_personality.md 的标签清单 == 枚举"""
        text = (_REPO / "config" / "agents" / f"{name}_personality.md").read_text(encoding="utf-8")
        tags = _parse_md_emotion_tags(text)
        assert tags == VALID_EMOTION_TAGS, f"{name}: {tags ^ VALID_EMOTION_TAGS}"

    @pytest.mark.parametrize("path", [
        _HOME_CFG / "workspace" / "SOUL.md",
        *[_HOME_CFG / "agents" / f"{n}_personality.md"
          for n in ("xiaoke", "xiaolang", "xiaolian", "xiaoli")],
    ])
    def test_runtime_personality_files_match_enum(self, path):
        """运行时人格文件（~/.ai-agent）若存在，其标签清单必须 == 枚举"""
        if not path.exists():
            pytest.skip(f"运行时文件不存在: {path}")
        tags = _parse_md_emotion_tags(path.read_text(encoding="utf-8"))
        assert tags == VALID_EMOTION_TAGS, f"{path.name}: {tags ^ VALID_EMOTION_TAGS}"


# ── 4. web 表情包分类与物理目录 ───────────────────────────────


def _web_emotion_categories() -> set[str]:
    src = (_REPO / "web" / "routers" / "agents.py").read_text(encoding="utf-8")
    block = re.search(r"_EMOTION_CATEGORIES\s*=\s*\[(.*?)\]", src, re.S).group(1)
    return set(re.findall(r'"(\w+)"', block))


class TestStickerTaxonomy:
    def test_web_categories_match_enum(self):
        """Agent 管理页的表情包情绪分类 == 枚举"""
        cats = _web_emotion_categories()
        assert cats == VALID_EMOTION_TAGS, cats ^ VALID_EMOTION_TAGS

    def test_xiaoda_sticker_dirs_match_enum(self):
        """小妲主表情包物理目录 == 枚举（目录缺失=该情绪永远选不到图）"""
        sticker_dir = Path.home() / ".ai-agent" / "stickers"
        if not sticker_dir.exists():
            pytest.skip("小妲表情包目录不存在")
        dirs = {d.name for d in sticker_dir.iterdir() if d.is_dir()}
        if not dirs:
            pytest.skip("小妲表情包目录为空（CI/裸机无素材，部署机才铺 19 个情绪子目录）")
        assert dirs == VALID_EMOTION_TAGS, dirs ^ VALID_EMOTION_TAGS


# ── 5. 工具 schema / 工作区提示词词表对齐（2026-08 review 尾巴收口）──


class TestToolSchemaVocabAlignment:
    """对 LLM 宣传的情绪词表必须与枚举单一事实源（EMOTION_VOCAB_SLASH）一致。

    根因：tts_tools/_builtin_manifest/TOOLS.md 三处硬编码 15 种旧词表
    （含已废 lonely、缺 love/moved/pout 等），LLM 按旧词表传参只能靠
    运行时别名兜底。现三处全部派生自 EMOTION_VOCAB_SLASH，本组测试
    防回退到字面量。
    """

    def test_tts_tool_schema_uses_enum_vocab(self):
        import tools.tts_tools as tt

        desc = tt._emotion_param_description()
        assert EMOTION_VOCAB_SLASH in desc
        for banned in ("lonely", "caring/playful"):
            assert banned not in desc

    def test_builtin_manifest_entry_matches_enum_vocab(self):
        from tools._builtin_manifest import BUILTIN_TOOLS

        entry = next(t for t in BUILTIN_TOOLS if t["name"] == "synthesize_voice")
        desc = entry["schema"]["properties"]["emotion"]["description"]
        assert EMOTION_VOCAB_SLASH in desc
        assert "lonely" not in desc

    def test_workspace_tools_md_matches_enum_vocab(self):
        md = (_REPO / "config" / "workspace" / "TOOLS.md").read_text(
            encoding="utf-8")
        assert EMOTION_VOCAB_SLASH in md
        # 旧行的"15种"计数与废弃标签不得回流
        assert "15种情绪风格" not in md and "lonely" not in md

    def test_runtime_workspace_md_matches_enum_vocab(self):
        runtime_md = _HOME_CFG / "workspace" / "TOOLS.md"
        if not runtime_md.exists():
            pytest.skip("运行时 TOOLS.md 不存在")
        assert EMOTION_VOCAB_SLASH in runtime_md.read_text(encoding="utf-8")
