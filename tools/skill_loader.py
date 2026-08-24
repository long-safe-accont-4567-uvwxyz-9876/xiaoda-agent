"""Skill Loader — 渐进式加载，借鉴 OpenWorker coworker/skills/base.py 设计。

将工具/能力按 Skill 分组，启动时只注入 catalog（名称+描述+关键词），
执行时按需加载完整定义，显著节省上下文 token。

设计要点：
- ``SkillManifest`` 描述单个 Skill 的元数据（名称、描述、关键词、工具列表）
- ``SkillLoader`` 管理所有 Skill 的发现、catalog 生成、按需加载
- 约定 ``tools/<skill_name>/SKILL.md`` 格式（YAML frontmatter + Markdown body）
- 与现有 ``_builtin_manifest.py`` 兼容：先走 SkillLoader，找不到再回退到内置 manifest
- 不破坏现有的 Tool Search v2（BM25+Vector+RRF 混合检索）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from loguru import logger


@dataclass
class SkillManifest:
    """单个 Skill 的精简清单 — 启动时注入上下文的格式。

    只包含名称、描述和关键词，不包含完整工具定义（schema 等），
    以最小化 token 占用。
    """
    name: str
    description: str
    keywords: list[str] = field(default_factory=list)
    # 完整加载后才填充
    tools: list[dict[str, Any]] = field(default_factory=list)
    # SKILL.md 的完整指令文本
    instructions: str = ""
    # SKILL.md 文件路径（如果有）
    path: Optional[str] = None
    # 是否已加载完整定义
    _loaded: bool = False

    def to_catalog_entry(self) -> dict[str, Any]:
        """转换为 catalog 条目（精简摘要）。"""
        return {
            "name": self.name,
            "description": self.description,
            "keywords": self.keywords,
        }

    @property
    def is_loaded(self) -> bool:
        """是否已加载完整定义。"""
        return self._loaded


class SkillLoader:
    """Skill 加载器 — 渐进式加载管理。

    用法：
        loader = SkillLoader()
        # 启动时只加载 catalog
        catalog = loader.load_catalog()
        # 执行时按需加载完整定义
        skill = loader.load_skill("file_operations")

    兼容性：
        - load_skill 找不到时返回 None，调用方可回退到 _builtin_manifest.py
        - 不影响 Tool Search v2 的 BM25+Vector+RRF 混合检索
    """

    def __init__(self, skill_dirs: list[str | Path] | None = None) -> None:
        """初始化 SkillLoader。

        Args:
            skill_dirs: 搜索 SKILL.md 的目录列表。默认为 [tools/]。
        """
        if skill_dirs is None:
            # 默认搜索 tools/ 目录下的子目录
            base = Path(__file__).parent
            skill_dirs = [base]
        self._skill_dirs = [Path(d) for d in skill_dirs]
        self._skills: dict[str, SkillManifest] = {}
        self._discover()

    def _discover(self) -> None:
        """扫描所有 skill 目录，发现 SKILL.md 文件。

        只解析 frontmatter（名称、描述、关键词），不加载完整内容。
        """
        for directory in self._skill_dirs:
            if not directory.is_dir():
                continue
            for sub in sorted(directory.iterdir()):
                if not sub.is_dir():
                    continue
                md_path = sub / "SKILL.md"
                if md_path.is_file():
                    skill = self._parse_skill_md(md_path)
                    if skill:
                        self._skills[skill.name] = skill
                        logger.debug("skill_loader.discovered",
                                     name=skill.name, path=str(md_path))

    def _parse_skill_md(self, md_path: Path) -> SkillManifest | None:
        """解析 SKILL.md 的 YAML frontmatter（不加载 body）。

        约定格式：
            ---
            name: file_operations
            description: 文件读写和目录操作
            keywords: [文件, 读取, 写入, 目录]
            ---
            # 完整指令内容（按需加载）
        """
        try:
            text = md_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            logger.warning("skill_loader.parse_failed", path=str(md_path), error=str(e))
            return None

        name = md_path.parent.name
        description = ""
        keywords: list[str] = []

        if text.startswith("---"):
            end = text.find("\n---", 3)
            if end != -1:
                frontmatter = text[3:end]
                for line in frontmatter.splitlines():
                    if ":" not in line:
                        continue
                    key, value = line.split(":", 1)
                    key = key.strip().lower()
                    value = value.strip()
                    if key == "name" and value:
                        name = value
                    elif key == "description":
                        description = value
                    elif key in ("keywords", "tags"):
                        # 支持两种格式：[a, b, c] 或 a, b, c
                        v = value.strip("[]")
                        keywords = [k.strip().strip('"\'') for k in v.split(",") if k.strip()]

        return SkillManifest(
            name=name,
            description=description,
            keywords=keywords,
            instructions="",  # 不加载完整 body，按需加载
            path=str(md_path),
        )

    def load_catalog(self) -> list[dict[str, Any]]:
        """加载所有 Skill 的精简 catalog（启动时调用）。

        Returns:
            catalog 条目列表，每条包含 name、description、keywords。
        """
        return [skill.to_catalog_entry() for skill in self._skills.values()]

    def load_skill(self, name: str) -> SkillManifest | None:
        """按需加载单个 Skill 的完整定义。

        Args:
            name: Skill 名称

        Returns:
            完整的 SkillManifest（含 instructions 和 tools），未找到返回 None。
        """
        skill = self._skills.get(name)
        if skill is None:
            return None

        if skill._loaded:
            return skill

        # 加载完整 SKILL.md body
        if skill.path:
            md_path = Path(skill.path)
            try:
                text = md_path.read_text(encoding="utf-8")
                if text.startswith("---"):
                    end = text.find("\n---", 3)
                    if end != -1:
                        skill.instructions = text[end + 4:].lstrip("\n").strip()
                    else:
                        skill.instructions = text
                else:
                    skill.instructions = text
            except (OSError, UnicodeDecodeError) as e:
                logger.warning("skill_loader.load_body_failed",
                               name=name, error=str(e))
                # 不标记 _loaded，下次 load() 重试，避免失败被永久缓存为空 instructions
                return skill

        skill._loaded = True
        logger.debug("skill_loader.loaded", name=name)
        return skill

    def names(self) -> list[str]:
        """返回所有已发现的 Skill 名称。"""
        return list(self._skills.keys())

    def get(self, name: str) -> SkillManifest | None:
        """获取 Skill manifest（未加载完整定义时 instructions 为空）。"""
        return self._skills.get(name)

    def catalog_text(self) -> str:
        """生成 catalog 文本（注入到 system prompt 用）。

        格式：
            Available skills — call load_skill(name) to load full instructions:
            - file_operations: 文件读写和目录操作 [keywords: 文件, 读取]
            - web_search: 网络搜索 [keywords: 搜索, 新闻]
        """
        catalog = self.load_catalog()
        if not catalog:
            return ""
        lines = []
        for entry in catalog:
            kw = f" [keywords: {', '.join(entry['keywords'])}]" if entry.get("keywords") else ""
            lines.append(f"- {entry['name']}: {entry['description']}{kw}")
        return (
            "可用技能 — 当任务需要时调用 load_skill(name) 加载完整指令：\n"
            + "\n".join(lines)
        )

    def find_by_keyword(self, keyword: str) -> list[str]:
        """根据关键词查找匹配的 Skill 名称。

        匹配 name、description、keywords 中的关键词。
        """
        keyword_lower = keyword.lower()
        matches = []
        for skill in self._skills.values():
            if keyword_lower in skill.name.lower():
                matches.append(skill.name)
            elif keyword_lower in skill.description.lower():
                matches.append(skill.name)
            elif any(keyword_lower in kw.lower() for kw in skill.keywords):
                matches.append(skill.name)
        return list(dict.fromkeys(matches))  # 去重保序


# ── 全局单例 ──────────────────────────────────────────────

_default_loader: SkillLoader | None = None


def get_skill_loader() -> SkillLoader:
    """获取全局 SkillLoader 单例。"""
    global _default_loader
    if _default_loader is None:
        _default_loader = SkillLoader()
    return _default_loader
