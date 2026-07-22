"""scripts/check_version_sync.py 的单元测试.

覆盖 4 源一致 / 不一致 / --fix 修复 / 缺文件 / 默认模式等场景。
使用 tmp_path fixture 构造临时 4 源文件，避免污染真实仓库。
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

# scripts/ 目录无 __init__.py，直接通过文件路径加载模块
_SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "check_version_sync.py"
_spec = importlib.util.spec_from_file_location("check_version_sync", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_version_sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_version_sync)


# ---------- fixtures ----------

PYPROJECT_TEMPLATE = """[build-system]
requires = ["setuptools>=61"]
build-backend = "setuptools.build_meta"

[project]
name = "xiaoda-agent"
version = "{version}"
description = "test"

[tool.pytest.ini_options]
testpaths = ["tests"]
"""

PACKAGE_JSON_TEMPLATE = """{{
  "name": "xiaoda-frontend",
  "version": "{version}",
  "private": true,
  "scripts": {{
    "dev": "vite"
  }}
}}
"""


def _write_sources(root: Path, version: str = "0.5.28", *, pyproject: str | None = None,
                   dot_version: str | None = None, package_json: str | None = None,
                   version_file: str | None = None) -> None:
    """在 root 下创建 4 源文件，缺省值都 = version，可单独覆盖."""
    (root / "VERSION").write_text(version_file if version_file is not None else version)
    (root / "pyproject.toml").write_text(
        PYPROJECT_TEMPLATE.format(version=pyproject if pyproject is not None else version)
    )
    (root / ".version").write_text(dot_version if dot_version is not None else version)
    pkg_root = root / "web" / "frontend"
    pkg_root.mkdir(parents=True, exist_ok=True)
    (pkg_root / "package.json").write_text(
        PACKAGE_JSON_TEMPLATE.format(version=package_json if package_json is not None else version)
    )


# ---------- 4 源一致 ----------

def test_all_sources_in_sync_returns_zero_exit_and_message(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "all versions in sync" in out
    assert "0.5.28" in out


def test_all_sources_in_sync_default_mode_acts_as_ci(capsys, tmp_path):
    _write_sources(tmp_path, "1.2.3")
    rc = check_version_sync.main(["--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "all versions in sync" in out


# ---------- 4 源不一致 ----------

def test_pyproject_out_of_sync_returns_nonzero_and_lists_diff(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28", pyproject="0.5.27")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out
    assert "0.5.27" in out  # current value
    assert "0.5.28" in out  # expected value


def test_dot_version_out_of_sync_returns_nonzero(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28", dot_version="0.4.0")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert ".version" in out
    assert "0.4.0" in out
    assert "0.5.28" in out


def test_package_json_out_of_sync_returns_nonzero(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28", package_json="9.9.9")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "package.json" in out
    assert "9.9.9" in out
    assert "0.5.28" in out


def test_version_file_differs_from_others_uses_version_as_truth(capsys, tmp_path):
    # VERSION 是 source of truth，pyproject/.version/package.json 都跟它不一致
    _write_sources(tmp_path, version_file="0.6.0", pyproject="0.5.28",
                   dot_version="0.5.28", package_json="0.5.28")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    # 期望值 = VERSION 的 0.6.0
    assert "0.6.0" in out


def test_multiple_files_out_of_sync_all_listed(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28", pyproject="0.5.27",
                   dot_version="0.5.26", package_json="0.5.25")
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out
    assert ".version" in out
    assert "package.json" in out
    assert "0.5.27" in out
    assert "0.5.26" in out
    assert "0.5.25" in out


# ---------- --fix 模式 ----------

def test_fix_mode_syncs_pyproject(tmp_path, capsys):
    _write_sources(tmp_path, "0.5.28", pyproject="0.5.27")
    rc = check_version_sync.main(["--fix", "--root", str(tmp_path)])
    assert rc == 0
    # pyproject.toml 现在应该 = 0.5.28
    pyproject_content = (tmp_path / "pyproject.toml").read_text()
    assert 'version = "0.5.28"' in pyproject_content
    # 其他结构保留
    assert "[build-system]" in pyproject_content
    assert "[tool.pytest.ini_options]" in pyproject_content
    out = capsys.readouterr().out
    assert "pyproject.toml" in out


def test_fix_mode_syncs_dot_version(tmp_path, capsys):
    _write_sources(tmp_path, "0.5.28", dot_version="0.4.0")
    rc = check_version_sync.main(["--fix", "--root", str(tmp_path)])
    assert rc == 0
    assert (tmp_path / ".version").read_text().strip() == "0.5.28"
    out = capsys.readouterr().out
    assert ".version" in out


def test_fix_mode_syncs_package_json(tmp_path, capsys):
    _write_sources(tmp_path, "0.5.28", package_json="9.9.9")
    rc = check_version_sync.main(["--fix", "--root", str(tmp_path)])
    assert rc == 0
    pkg = json.loads((tmp_path / "web" / "frontend" / "package.json").read_text())
    assert pkg["version"] == "0.5.28"
    # 其他字段保留
    assert pkg["name"] == "xiaoda-frontend"
    assert pkg["scripts"]["dev"] == "vite"
    out = capsys.readouterr().out
    assert "package.json" in out


def test_fix_mode_then_ci_passes(tmp_path, capsys):
    _write_sources(tmp_path, "0.5.28", pyproject="0.5.27",
                   dot_version="0.4.0", package_json="9.9.9")
    # fix
    rc1 = check_version_sync.main(["--fix", "--root", str(tmp_path)])
    assert rc1 == 0
    capsys.readouterr()  # clear
    # ci
    rc2 = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc2 == 0
    assert "all versions in sync" in capsys.readouterr().out


def test_fix_mode_when_already_in_sync_does_not_modify(tmp_path, capsys):
    _write_sources(tmp_path, "0.5.28")
    rc = check_version_sync.main(["--fix", "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    # 应该报告没有需要修复的（或一致）
    assert "in sync" in out or "no changes" in out.lower() or "nothing" in out.lower()


# ---------- 缺文件场景 ----------

def test_missing_version_file_returns_nonzero(capsys, tmp_path):
    # 只创建其他 3 个文件
    (tmp_path / "pyproject.toml").write_text(PYPROJECT_TEMPLATE.format(version="0.5.28"))
    (tmp_path / ".version").write_text("0.5.28")
    pkg_root = tmp_path / "web" / "frontend"
    pkg_root.mkdir(parents=True)
    (pkg_root / "package.json").write_text(PACKAGE_JSON_TEMPLATE.format(version="0.5.28"))
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "VERSION" in out


def test_missing_pyproject_returns_nonzero(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28")
    (tmp_path / "pyproject.toml").unlink()
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out


def test_missing_dot_version_returns_nonzero(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28")
    (tmp_path / ".version").unlink()
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert ".version" in out


def test_missing_package_json_returns_nonzero(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28")
    (tmp_path / "web" / "frontend" / "package.json").unlink()
    rc = check_version_sync.main(["--ci", "--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "package.json" in out


# ---------- 读取函数单元测试 ----------

def test_read_version_file_strips_whitespace(tmp_path):
    p = tmp_path / "VERSION"
    p.write_text("  0.5.28\n\n")
    assert check_version_sync.read_plain_version(p) == "0.5.28"


def test_read_pyproject_version_extracts_version(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text(PYPROJECT_TEMPLATE.format(version="1.2.3"))
    assert check_version_sync.read_pyproject_version(p) == "1.2.3"


def test_read_package_json_version_extracts_version(tmp_path):
    p = tmp_path / "package.json"
    p.write_text(PACKAGE_JSON_TEMPLATE.format(version="3.4.5"))
    assert check_version_sync.read_package_json_version(p) == "3.4.5"


def test_read_returns_none_when_file_missing(tmp_path):
    assert check_version_sync.read_plain_version(tmp_path / "nope") is None
    assert check_version_sync.read_pyproject_version(tmp_path / "nope") is None
    assert check_version_sync.read_package_json_version(tmp_path / "nope") is None


# ---------- 默认模式（无参数）----------

def test_no_args_acts_as_ci_mode(capsys, tmp_path):
    _write_sources(tmp_path, "0.5.28", pyproject="0.5.0")
    rc = check_version_sync.main(["--root", str(tmp_path)])
    assert rc != 0
    out = capsys.readouterr().out
    assert "pyproject.toml" in out
