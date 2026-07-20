"""tests/test_count_project_stats.py

TDD 单元测试：scripts/count_project_stats.py

覆盖 Task 3（项目统计脚本）与 Task 12（路由端点统计合并）的所有统计函数、
CLI 入口、JSON/Markdown 输出与 README 校验。

所有测试使用 tmp_path 构造临时项目结构，避免依赖真实仓库。
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
SCRIPT_PATH = PROJECT_ROOT / "scripts" / "count_project_stats.py"


def _load_module():
    """从文件路径加载 scripts/count_project_stats.py（scripts/ 不是 package）。"""
    spec = importlib.util.spec_from_file_location("count_project_stats", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cps():
    """已加载的 count_project_stats 模块。"""
    return _load_module()


# ============================================================
# count_python_modules
# ============================================================

class TestCountPythonModules:
    def test_counts_py_files_recursively(self, cps, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "b.py").write_text("y = 2\n")
        assert cps.count_python_modules(tmp_path) == 2

    def test_excludes_venv(self, cps, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "lib").mkdir()
        (tmp_path / ".venv" / "lib" / "site.py").write_text("x = 1\n")
        assert cps.count_python_modules(tmp_path) == 1

    def test_excludes_node_modules(self, cps, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        (tmp_path / "node_modules").mkdir()
        (tmp_path / "node_modules" / "dep.py").write_text("x = 1\n")
        assert cps.count_python_modules(tmp_path) == 1

    def test_excludes_pycache(self, cps, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "skip.py").write_text("x = 1\n")
        assert cps.count_python_modules(tmp_path) == 1

    def test_excludes_web_frontend(self, cps, tmp_path):
        (tmp_path / "good.py").write_text("x = 1\n")
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "backend.py").write_text("x = 1\n")
        (tmp_path / "web" / "frontend").mkdir()
        (tmp_path / "web" / "frontend" / "src").mkdir()
        (tmp_path / "web" / "frontend" / "src" / "app.py").write_text("x = 1\n")
        # good.py + web/backend.py = 2; web/frontend 排除
        assert cps.count_python_modules(tmp_path) == 2

    def test_excludes_nested_pycache(self, cps, tmp_path):
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "a.py").write_text("x = 1\n")
        (tmp_path / "core" / "__pycache__").mkdir()
        (tmp_path / "core" / "__pycache__" / "a.cpython.py").write_text("x = 1\n")
        assert cps.count_python_modules(tmp_path) == 1

    def test_empty_dir_returns_zero(self, cps, tmp_path):
        assert cps.count_python_modules(tmp_path) == 0


# ============================================================
# count_loc
# ============================================================

class TestCountLoc:
    def test_counts_total_code_blank_comments(self, cps, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\ny = 2\n\n# comment\n")
        result = cps.count_loc(tmp_path)
        assert result["total"] == 4
        assert result["code"] == 2
        assert result["blank"] == 1
        assert result["comments"] == 1

    def test_sums_across_files(self, cps, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\nz = 3\n")
        result = cps.count_loc(tmp_path)
        assert result["total"] == 3
        assert result["code"] == 3

    def test_excludes_venv(self, cps, tmp_path):
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / ".venv").mkdir()
        (tmp_path / ".venv" / "b.py").write_text("y = 2\nz = 3\n")
        result = cps.count_loc(tmp_path)
        assert result["total"] == 1

    def test_inline_comment_counted_as_code(self, cps, tmp_path):
        """行内注释（code # comment）按代码行统计。"""
        (tmp_path / "a.py").write_text("x = 1  # inline\n")
        result = cps.count_loc(tmp_path)
        assert result["code"] == 1
        assert result["comments"] == 0

    def test_empty_file(self, cps, tmp_path):
        (tmp_path / "empty.py").write_text("")
        result = cps.count_loc(tmp_path)
        assert result["total"] == 0


# ============================================================
# count_api_endpoints
# ============================================================

class TestCountApiEndpoints:
    def test_counts_modules_excluding_init(self, cps, tmp_path):
        (tmp_path / "__init__.py").write_text("router = None\n")
        (tmp_path / "users.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/users')\n"
            "async def list_users(): pass\n"
        )
        result = cps.count_api_endpoints(tmp_path)
        assert result["modules"] == 1

    def test_counts_methods_and_total(self, cps, tmp_path):
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "users.py").write_text(
            "router = APIRouter()\n"
            "@router.get('/users')\n"
            "async def list_users(): pass\n"
            "@router.post('/users')\n"
            "async def create_user(): pass\n"
        )
        (tmp_path / "items.py").write_text(
            "router = APIRouter()\n"
            "@router.get('/items')\n"
            "async def list_items(): pass\n"
            "@router.put('/items/{id}')\n"
            "async def update_item(): pass\n"
            "@router.delete('/items/{id}')\n"
            "async def delete_item(): pass\n"
            "@router.patch('/items/{id}')\n"
            "async def patch_item(): pass\n"
        )
        result = cps.count_api_endpoints(tmp_path)
        assert result["modules"] == 2
        assert result["get"] == 2
        assert result["post"] == 1
        assert result["put"] == 1
        assert result["delete"] == 1
        assert result["patch"] == 1
        assert result["total"] == 6

    def test_handles_multiline_decorator(self, cps, tmp_path):
        """多行装饰器：@router.get( 跨行也应正确识别。"""
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "multi.py").write_text(
            "router = APIRouter()\n"
            "@router.get(\n"
            "    '/long/path',\n"
            "    response_model=dict\n"
            ")\n"
            "async def get_long(): pass\n"
        )
        result = cps.count_api_endpoints(tmp_path)
        assert result["get"] == 1
        assert result["total"] == 1
        assert result["modules"] == 1

    def test_empty_dir_returns_zeros(self, cps, tmp_path):
        result = cps.count_api_endpoints(tmp_path)
        assert result["modules"] == 0
        assert result["total"] == 0
        assert result["get"] == 0

    def test_only_init_file_returns_zero_modules(self, cps, tmp_path):
        (tmp_path / "__init__.py").write_text("router = APIRouter()\n")
        result = cps.count_api_endpoints(tmp_path)
        assert result["modules"] == 0
        assert result["total"] == 0

    def test_does_not_count_non_router_decorators(self, cps, tmp_path):
        """@app.get 或 @other.get 不应被统计。"""
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "a.py").write_text(
            "router = APIRouter()\n"
            "app = APIRouter()\n"
            "@router.get('/a')\n"
            "async def a(): pass\n"
            "@app.get('/b')\n"
            "async def b(): pass\n"
        )
        result = cps.count_api_endpoints(tmp_path)
        assert result["get"] == 1
        assert result["total"] == 1


# ============================================================
# count_db_tables
# ============================================================

class TestCountDbTables:
    def test_counts_tables_virtual_indexes(self, cps, tmp_path):
        schema = tmp_path / "schema.sql"
        schema.write_text(
            "CREATE TABLE IF NOT EXISTS t1 (id INTEGER);\n"
            "CREATE TABLE t2 (id INTEGER);\n"
            "CREATE VIRTUAL TABLE IF NOT EXISTS t1_fts USING fts5(content);\n"
            "CREATE INDEX idx_1 ON t1(id);\n"
            "CREATE UNIQUE INDEX idx_2 ON t1(name);\n"
            "CREATE TRIGGER trg AFTER INSERT ON t1 BEGIN END;\n"
        )
        result = cps.count_db_tables(schema)
        assert result["tables"] == 2
        assert result["virtual_tables"] == 1
        assert result["indexes"] == 2  # regular + unique

    def test_does_not_count_virtual_as_regular_table(self, cps, tmp_path):
        """CREATE VIRTUAL TABLE 不应被计入 tables。"""
        schema = tmp_path / "schema.sql"
        schema.write_text(
            "CREATE VIRTUAL TABLE t_fts USING fts5(content);\n"
        )
        result = cps.count_db_tables(schema)
        assert result["tables"] == 0
        assert result["virtual_tables"] == 1

    def test_does_not_count_trigger_as_table(self, cps, tmp_path):
        schema = tmp_path / "schema.sql"
        schema.write_text(
            "CREATE TABLE t1 (id INTEGER);\n"
            "CREATE TRIGGER trg AFTER INSERT ON t1 BEGIN END;\n"
        )
        result = cps.count_db_tables(schema)
        assert result["tables"] == 1

    def test_case_insensitive(self, cps, tmp_path):
        schema = tmp_path / "schema.sql"
        schema.write_text(
            "create table t1 (id INTEGER);\n"
            "CREATE index idx_1 ON t1(id);\n"
        )
        result = cps.count_db_tables(schema)
        assert result["tables"] == 1
        assert result["indexes"] == 1

    def test_empty_file(self, cps, tmp_path):
        schema = tmp_path / "schema.sql"
        schema.write_text("")
        result = cps.count_db_tables(schema)
        assert result["tables"] == 0
        assert result["virtual_tables"] == 0
        assert result["indexes"] == 0


# ============================================================
# count_emotion_enum
# ============================================================

class TestCountEmotionEnum:
    def test_counts_emotion_enum_members(self, cps, tmp_path):
        enum_file = tmp_path / "emotion_enum.py"
        enum_file.write_text(
            "from enum import Enum\n"
            "class Emotion(str, Enum):\n"
            "    HAPPY = 'happy'\n"
            "    SAD = 'sad'\n"
            "    ANGRY = 'angry'\n"
            "# alias comment\n"
            "OTHER = 1\n"
        )
        assert cps.count_emotion_enum(enum_file) == 3

    def test_returns_zero_when_no_emotion_class(self, cps, tmp_path):
        enum_file = tmp_path / "emotion_enum.py"
        enum_file.write_text(
            "from enum import Enum\n"
            "class Color(Enum):\n"
            "    RED = 1\n"
        )
        assert cps.count_emotion_enum(enum_file) == 0

    def test_ignores_methods_in_emotion_class(self, cps, tmp_path):
        """Emotion 类内的方法不应被计为枚举成员。"""
        enum_file = tmp_path / "emotion_enum.py"
        enum_file.write_text(
            "from enum import Enum\n"
            "class Emotion(str, Enum):\n"
            "    HAPPY = 'happy'\n"
            "    SAD = 'sad'\n"
            "    def describe(self):\n"
            "        return self.value\n"
        )
        assert cps.count_emotion_enum(enum_file) == 2


# ============================================================
# count_builtin_tools
# ============================================================

class TestCountBuiltinTools:
    def test_counts_list_length(self, cps, tmp_path):
        manifest = tmp_path / "_builtin_manifest.py"
        manifest.write_text(
            "BUILTIN_TOOLS = [\n"
            "    {'name': 'tool1'},\n"
            "    {'name': 'tool2'},\n"
            "    {'name': 'tool3'},\n"
            "]\n"
        )
        assert cps.count_builtin_tools(manifest) == 3

    def test_handles_empty_list(self, cps, tmp_path):
        manifest = tmp_path / "_builtin_manifest.py"
        manifest.write_text("BUILTIN_TOOLS = []\n")
        assert cps.count_builtin_tools(manifest) == 0

    def test_returns_zero_when_missing_assignment(self, cps, tmp_path):
        manifest = tmp_path / "_builtin_manifest.py"
        manifest.write_text("OTHER = [1, 2, 3]\n")
        assert cps.count_builtin_tools(manifest) == 0

    def test_ignores_other_module_level_assignments(self, cps, tmp_path):
        manifest = tmp_path / "_builtin_manifest.py"
        manifest.write_text(
            "_INTERNAL = [1, 2]\n"
            "BUILTIN_TOOLS = [{'name': 'a'}, {'name': 'b'}]\n"
            "_OTHER = [3, 4, 5]\n"
        )
        assert cps.count_builtin_tools(manifest) == 2

    def test_handles_annotated_assignment(self, cps, tmp_path):
        """带类型注解的赋值 BUILTIN_TOOLS: list[...] = [...] 也应识别。"""
        manifest = tmp_path / "_builtin_manifest.py"
        manifest.write_text(
            "from typing import Any\n"
            "BUILTIN_TOOLS: list[dict[str, Any]] = [\n"
            "    {'name': 'tool1'},\n"
            "    {'name': 'tool2'},\n"
            "]\n"
        )
        assert cps.count_builtin_tools(manifest) == 2


# ============================================================
# count_test_files
# ============================================================

class TestCountTestFiles:
    def test_counts_py_files(self, cps, tmp_path):
        (tmp_path / "test_a.py").write_text("")
        (tmp_path / "test_b.py").write_text("")
        (tmp_path / "helper.py").write_text("")
        assert cps.count_test_files(tmp_path) == 3

    def test_excludes_init_and_conftest(self, cps, tmp_path):
        (tmp_path / "test_a.py").write_text("")
        (tmp_path / "__init__.py").write_text("")
        (tmp_path / "conftest.py").write_text("")
        assert cps.count_test_files(tmp_path) == 1

    def test_counts_recursively(self, cps, tmp_path):
        (tmp_path / "test_a.py").write_text("")
        (tmp_path / "sub").mkdir()
        (tmp_path / "sub" / "test_b.py").write_text("")
        assert cps.count_test_files(tmp_path) == 2

    def test_excludes_pycache(self, cps, tmp_path):
        (tmp_path / "test_a.py").write_text("")
        (tmp_path / "__pycache__").mkdir()
        (tmp_path / "__pycache__" / "test_a.cpython.py").write_text("")
        assert cps.count_test_files(tmp_path) == 1

    def test_empty_dir(self, cps, tmp_path):
        assert cps.count_test_files(tmp_path) == 0


# ============================================================
# count_subsystem_modules
# ============================================================

class TestCountSubsystemModules:
    def test_counts_per_subsystem(self, cps, tmp_path):
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "a.py").write_text("")
        (tmp_path / "core" / "b.py").write_text("")
        (tmp_path / "memory").mkdir()
        (tmp_path / "memory" / "m.py").write_text("")
        (tmp_path / "emotion").mkdir()
        (tmp_path / "emotion" / "e.py").write_text("")
        result = cps.count_subsystem_modules(tmp_path, ["core", "memory", "emotion"])
        assert result == {"core": 2, "memory": 1, "emotion": 1}

    def test_missing_dir_returns_zero(self, cps, tmp_path):
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "a.py").write_text("")
        result = cps.count_subsystem_modules(tmp_path, ["core", "memory"])
        assert result == {"core": 1, "memory": 0}

    def test_default_subsystems(self, cps, tmp_path):
        """默认 subsystems = core/memory/tool_engine/emotion/security/plugins。"""
        for sub in ["core", "memory", "tool_engine", "emotion", "security", "plugins"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
            (tmp_path / sub / "x.py").write_text("")
        result = cps.count_subsystem_modules(tmp_path)
        assert set(result.keys()) == {"core", "memory", "tool_engine", "emotion", "security", "plugins"}
        assert all(v == 1 for v in result.values())

    def test_excludes_pycache_in_subsystem(self, cps, tmp_path):
        (tmp_path / "core").mkdir()
        (tmp_path / "core" / "a.py").write_text("")
        (tmp_path / "core" / "__pycache__").mkdir()
        (tmp_path / "core" / "__pycache__" / "a.py").write_text("")
        result = cps.count_subsystem_modules(tmp_path, ["core"])
        assert result == {"core": 1}


# ============================================================
# check_readme
# ============================================================

class TestCheckReadme:
    def test_no_mismatch_when_in_sync(self, cps, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text(
            "Python 模块 | 507\n"
            "数据库表 | 30 张 + 52 索引\n"
            "Web API 路由 | 17 模块 + 186 端点\n"
            "16 类情绪 → ok\n"
            "~131,984 行\n"
        )
        actual = {
            "python_modules": 507,
            "loc_total": 131984,
            "db_tables": 30,
            "db_indexes": 52,
            "router_modules": 17,
            "api_endpoints": 186,
            "emotion_enum": 16,
        }
        mismatches = cps.check_readme(readme, actual)
        assert mismatches == []

    def test_reports_outdated_table_count(self, cps, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("Schema（21 表 + 22 索引）\n")
        actual = {"db_tables": 30, "db_indexes": 52}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "30" in text  # actual tables
        assert "52" in text  # actual indexes

    def test_reports_outdated_table_count_zhang_pattern(self, cps, tmp_path):
        """模式 'X 张 + Y 索引' 也应识别（README 实际写法）。"""
        readme = tmp_path / "README.md"
        readme.write_text("| 数据库表 | 21 张 + 22 索引 |\n")
        actual = {"db_tables": 30, "db_indexes": 52}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "30" in text
        assert "52" in text

    def test_reports_outdated_router_endpoints(self, cps, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("| Web API 路由 | 15 模块 + 139 端点 |\n")
        actual = {"router_modules": 17, "api_endpoints": 186}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "17" in text
        assert "186" in text

    def test_reports_outdated_router_modules_count_phrase(self, cps, tmp_path):
        """'X 个 API 路由模块' 模式也应识别。"""
        readme = tmp_path / "README.md"
        readme.write_text("routers/  # 13 个 API 路由模块\n")
        actual = {"router_modules": 17}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "17" in text

    def test_reports_outdated_emotion_count(self, cps, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("9 类核心情绪\n")
        actual = {"emotion_enum": 16}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "16" in text

    def test_reports_outdated_emotion_count_zhong_pattern(self, cps, tmp_path):
        """'X 种情绪' 模式也应识别。"""
        readme = tmp_path / "README.md"
        readme.write_text("16 种情绪\n")
        actual = {"emotion_enum": 9}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "9" in text

    def test_reports_outdated_python_modules(self, cps, tmp_path):
        readme = tmp_path / "README.md"
        readme.write_text("| Python 模块 | 80+ |\n")
        actual = {"python_modules": 507}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "507" in text

    def test_reports_outdated_loc_comma_pattern(self, cps, tmp_path):
        """~20,000 行 模式。"""
        readme = tmp_path / "README.md"
        readme.write_text("| 生产代码 | ~20,000 行 |\n")
        actual = {"loc_total": 131984}
        mismatches = cps.check_readme(readme, actual)
        text = "\n".join(mismatches)
        assert "131984" in text or "131,984" in text

    def test_reports_outdated_loc_k_pattern(self, cps, tmp_path):
        """~20k 行 模式。"""
        readme = tmp_path / "README.md"
        readme.write_text("代码 ~20k 行\n")
        actual = {"loc_total": 131984}
        mismatches = cps.check_readme(readme, actual)
        assert len(mismatches) >= 1

    def test_missing_readme_returns_empty(self, cps, tmp_path):
        """README 不存在时返回空列表（不报错）。"""
        readme = tmp_path / "nonexistent.md"
        actual = {"db_tables": 30}
        mismatches = cps.check_readme(readme, actual)
        assert mismatches == []

    def test_does_not_match_v1_behavior_false_positive(self, cps, tmp_path):
        """'v1 行为' 中的 '1 行' 不应被识别为代码行数。"""
        readme = tmp_path / "README.md"
        readme.write_text("无 Embedding API 时自动降级为纯 BM25（v1 行为）\n")
        actual = {"loc_total": 131984}
        mismatches = cps.check_readme(readme, actual)
        assert mismatches == []

    def test_does_not_match_module_specific_loc(self, cps, tmp_path):
        """'1431 行 God Class' 是模块级行数，不应被识别为项目代码行数。"""
        readme = tmp_path / "README.md"
        readme.write_text("AgentCore 从 1431 行 God Class 拆分为 5 个子模块\n")
        actual = {"loc_total": 131984}
        mismatches = cps.check_readme(readme, actual)
        assert mismatches == []

    def test_does_not_match_test_code_loc(self, cps, tmp_path):
        """'测试代码 | ~6,000 行' 是测试代码行数，不应对比到总代码行数。"""
        readme = tmp_path / "README.md"
        readme.write_text("| 测试代码 | ~6,000 行 |\n")
        actual = {"loc_total": 131984}
        mismatches = cps.check_readme(readme, actual)
        assert mismatches == []


# ============================================================
# format_json / format_markdown
# ============================================================

class TestFormat:
    def _sample_stats(self):
        return {
            "python_modules": 507,
            "loc": {"total": 131984, "code": 100000, "comments": 10000, "blank": 21984},
            "router_modules": 17,
            "api_endpoints": {"get": 100, "post": 50, "put": 20, "delete": 10, "patch": 6, "total": 186},
            "db_tables": 30,
            "db_virtual_tables": 4,
            "db_indexes": 52,
            "emotion_enum": 16,
            "builtin_tools": 40,
            "test_files": 200,
            "subsystems": {"core": 30, "memory": 20, "tool_engine": 10, "emotion": 5, "security": 8, "plugins": 3},
        }

    def test_format_json_outputs_all_metrics(self, cps):
        out = cps.format_json(self._sample_stats())
        parsed = json.loads(out)
        assert parsed["python_modules"] == 507
        assert parsed["router_modules"] == 17
        assert parsed["api_endpoints"]["total"] == 186
        assert parsed["db_tables"] == 30
        assert parsed["db_virtual_tables"] == 4
        assert parsed["db_indexes"] == 52
        assert parsed["emotion_enum"] == 16
        assert parsed["builtin_tools"] == 40
        assert parsed["test_files"] == 200
        assert "subsystems" in parsed
        assert parsed["loc"]["total"] == 131984

    def test_format_markdown_outputs_table(self, cps):
        out = cps.format_markdown(self._sample_stats())
        assert "|" in out  # markdown table
        assert "507" in out
        assert "186" in out
        assert "30" in out
        assert "52" in out
        assert "16" in out

    def test_format_markdown_has_header(self, cps):
        out = cps.format_markdown(self._sample_stats())
        # 第一行是表头
        first_line = out.strip().splitlines()[0]
        assert "指标" in first_line
        assert "数值" in first_line


# ============================================================
# main (CLI)
# ============================================================

class TestMain:
    @pytest.fixture
    def mini_project(self, tmp_path):
        """构造最小项目结构用于 main 测试。"""
        (tmp_path / "a.py").write_text("x = 1\n")
        (tmp_path / "b.py").write_text("y = 2\nz = 3\n\n# comment\n")
        (tmp_path / "db").mkdir()
        (tmp_path / "db" / "schema.sql").write_text(
            "CREATE TABLE t (id INTEGER);\n"
            "CREATE VIRTUAL TABLE t_fts USING fts5(content);\n"
            "CREATE INDEX idx_t ON t(id);\n"
        )
        (tmp_path / "web").mkdir()
        (tmp_path / "web" / "routers").mkdir()
        (tmp_path / "web" / "routers" / "__init__.py").write_text("")
        (tmp_path / "web" / "routers" / "users.py").write_text(
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "@router.get('/users')\n"
            "async def list_users(): pass\n"
            "@router.post('/users')\n"
            "async def create_user(): pass\n"
        )
        (tmp_path / "emotion").mkdir()
        (tmp_path / "emotion" / "emotion_enum.py").write_text(
            "from enum import Enum\n"
            "class Emotion(str, Enum):\n"
            "    HAPPY = 'happy'\n"
            "    SAD = 'sad'\n"
        )
        (tmp_path / "tools").mkdir()
        (tmp_path / "tools" / "_builtin_manifest.py").write_text(
            "BUILTIN_TOOLS = [{'name': 't1'}, {'name': 't2'}]\n"
        )
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_a.py").write_text("")
        (tmp_path / "tests" / "__init__.py").write_text("")
        (tmp_path / "tests" / "conftest.py").write_text("")
        for sub in ["core", "memory", "tool_engine", "emotion", "security", "plugins"]:
            (tmp_path / sub).mkdir(parents=True, exist_ok=True)
            (tmp_path / sub / "mod.py").write_text("")
        return tmp_path

    def test_main_json_output(self, cps, mini_project, monkeypatch, capsys):
        monkeypatch.chdir(mini_project)
        rc = cps.main(["--format", "json"])
        assert rc == 0
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["python_modules"] == 15  # all .py files in mini_project tree
        assert parsed["db_tables"] == 1
        assert parsed["db_virtual_tables"] == 1
        assert parsed["db_indexes"] == 1
        assert parsed["router_modules"] == 1
        assert parsed["api_endpoints"]["total"] == 2
        assert parsed["emotion_enum"] == 2
        assert parsed["builtin_tools"] == 2
        assert parsed["test_files"] == 1  # only test_a.py (init/conftest excluded)

    def test_main_markdown_output(self, cps, mini_project, monkeypatch, capsys):
        monkeypatch.chdir(mini_project)
        rc = cps.main(["--format", "markdown"])
        assert rc == 0
        captured = capsys.readouterr()
        assert "|" in captured.out
        assert "2" in captured.out  # python_modules=2

    def test_main_default_format_is_markdown(self, cps, mini_project, monkeypatch, capsys):
        """默认 --format 为 markdown。"""
        monkeypatch.chdir(mini_project)
        rc = cps.main([])
        assert rc == 0
        captured = capsys.readouterr()
        assert "|" in captured.out

    def test_main_check_readme_returns_nonzero_when_outdated(self, cps, mini_project, monkeypatch, capsys):
        (mini_project / "README.md").write_text(
            "| Python 模块 | 80+ |\n"
            "| 数据库表 | 21 张 + 22 索引 |\n"
            "| Web API 路由 | 15 模块 + 139 端点 |\n"
            "9 类情绪\n"
            "~20,000 行\n"
        )
        monkeypatch.chdir(mini_project)
        rc = cps.main(["--check-readme"])
        assert rc != 0
        captured = capsys.readouterr()
        # 输出包含实际值
        assert "2" in captured.out  # python_modules=2
        assert "1" in captured.out  # db_tables=1

    def test_main_check_readme_returns_zero_when_in_sync(self, cps, mini_project, monkeypatch, capsys):
        (mini_project / "README.md").write_text(
            "| Python 模块 | 15 |\n"
            "| 数据库表 | 1 张 + 1 索引 |\n"
            "| Web API 路由 | 1 模块 + 2 端点 |\n"
            "2 类情绪\n"
        )
        monkeypatch.chdir(mini_project)
        rc = cps.main(["--check-readme"])
        assert rc == 0

    def test_main_check_readme_no_readme_returns_zero(self, cps, mini_project, monkeypatch, capsys):
        """无 README.md 时 --check-readme 返回 0（不报错）。"""
        monkeypatch.chdir(mini_project)
        rc = cps.main(["--check-readme"])
        assert rc == 0
