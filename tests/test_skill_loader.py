"""P0-2: Skill Loader 渐进式加载 — 测试

测试 tools/skill_loader.py 的 SkillManifest 和 SkillLoader。
"""
import pytest
import tempfile
from pathlib import Path

from tools.skill_loader import SkillManifest, SkillLoader


class TestSkillManifest:
    """SkillManifest 数据类测试"""

    def test_to_catalog_entry(self):
        """catalog 条目只包含 name/description/keywords"""
        skill = SkillManifest(
            name="file_ops",
            description="文件操作",
            keywords=["文件", "读取", "写入"],
        )
        entry = skill.to_catalog_entry()
        assert entry["name"] == "file_ops"
        assert entry["description"] == "文件操作"
        assert entry["keywords"] == ["文件", "读取", "写入"]
        # catalog 条目不包含完整定义
        assert "tools" not in entry
        assert "instructions" not in entry

    def test_is_loaded_default_false(self):
        """未调用 load_skill 前 is_loaded 为 False"""
        skill = SkillManifest(name="test", description="test")
        assert not skill.is_loaded


class TestSkillLoader:
    """SkillLoader 测试"""

    @pytest.fixture
    def temp_skill_dir(self):
        """创建临时 skill 目录结构"""
        with tempfile.TemporaryDirectory() as tmpdir:
            # 创建一个 SKILL.md
            skill1_dir = Path(tmpdir) / "file_operations"
            skill1_dir.mkdir()
            (skill1_dir / "SKILL.md").write_text(
                "---\n"
                "name: file_operations\n"
                "description: 文件读写和目录操作\n"
                "keywords: [文件, 读取, 写入, 目录]\n"
                "---\n"
                "# 文件操作技能\n\n"
                "支持读取、写入、搜索文件等操作。\n",
                encoding="utf-8",
            )

            # 创建第二个 SKILL.md
            skill2_dir = Path(tmpdir) / "web_search"
            skill2_dir.mkdir()
            (skill2_dir / "SKILL.md").write_text(
                "---\n"
                "name: web_search\n"
                "description: 网络搜索\n"
                "keywords: [搜索, 新闻, 互联网]\n"
                "---\n"
                "# 网络搜索技能\n\n"
                "支持多引擎搜索。\n",
                encoding="utf-8",
            )

            # 创建一个不含 SKILL.md 的目录（应被忽略）
            (Path(tmpdir) / "no_skill").mkdir()

            yield tmpdir

    def test_discover_skills(self, temp_skill_dir):
        """扫描目录发现 SKILL.md"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        names = loader.names()
        assert "file_operations" in names
        assert "web_search" in names
        assert len(names) == 2

    def test_load_catalog(self, temp_skill_dir):
        """load_catalog 返回精简摘要"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        catalog = loader.load_catalog()
        assert len(catalog) == 2
        entries = {e["name"]: e for e in catalog}
        assert "file_operations" in entries
        assert entries["file_operations"]["description"] == "文件读写和目录操作"
        assert "文件" in entries["file_operations"]["keywords"]

    def test_load_skill_full(self, temp_skill_dir):
        """load_skill 加载完整定义"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        skill = loader.load_skill("file_operations")
        assert skill is not None
        assert skill.is_loaded
        assert "文件操作技能" in skill.instructions
        assert "支持读取" in skill.instructions

    def test_load_skill_not_found(self, temp_skill_dir):
        """加载不存在的 skill 返回 None"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        assert loader.load_skill("nonexistent") is None

    def test_load_skill_idempotent(self, temp_skill_dir):
        """多次 load_skill 返回同一对象"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        skill1 = loader.load_skill("file_operations")
        skill2 = loader.load_skill("file_operations")
        assert skill1 is skill2

    def test_catalog_text(self, temp_skill_dir):
        """catalog_text 生成可注入的文本"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        text = loader.catalog_text()
        assert "可用技能" in text
        assert "file_operations" in text
        assert "文件读写和目录操作" in text
        assert "keywords" in text

    def test_find_by_keyword(self, temp_skill_dir):
        """根据关键词查找 skill"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        # 按关键词搜索
        matches = loader.find_by_keyword("文件")
        assert "file_operations" in matches

        # 按 description 搜索
        matches = loader.find_by_keyword("网络")
        assert "web_search" in matches

        # 按 name 搜索
        matches = loader.find_by_keyword("file")
        assert "file_operations" in matches

    def test_get_unloaded_skill(self, temp_skill_dir):
        """get 返回未加载的 manifest（instructions 为空）"""
        loader = SkillLoader(skill_dirs=[temp_skill_dir])
        skill = loader.get("file_operations")
        assert skill is not None
        assert not skill.is_loaded
        assert skill.instructions == ""

    def test_nonexistent_directory(self):
        """不存在的目录不报错"""
        loader = SkillLoader(skill_dirs=["/nonexistent/path"])
        assert loader.names() == []
        assert loader.load_catalog() == []

    def test_empty_skill_dir(self):
        """空目录返回空 catalog"""
        with tempfile.TemporaryDirectory() as tmpdir:
            loader = SkillLoader(skill_dirs=[tmpdir])
            assert loader.load_catalog() == []

    def test_skill_md_without_frontmatter(self):
        """SKILL.md 无 frontmatter 时使用目录名"""
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = Path(tmpdir) / "my_skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "# My Skill\n\nNo frontmatter here.\n",
                encoding="utf-8",
            )
            loader = SkillLoader(skill_dirs=[tmpdir])
            skill = loader.get("my_skill")
            assert skill is not None
            assert skill.name == "my_skill"
            assert skill.description == ""
