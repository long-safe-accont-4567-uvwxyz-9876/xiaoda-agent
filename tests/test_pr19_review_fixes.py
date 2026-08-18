"""PR #19 CodeRabbit 审查发现的回归测试。

1. config._migrate_old_data：新目录中已存在被忽略子目录（workspace）时，
   不能误判为非空而跳过 config 迁移（否则 agent.json5/agents 永久丢失）。
2. web.routers.setup 的 _is_template_section / _preserve_extra_sections：
   只能精确匹配 _build_user_md 生成的模板标题，不能把 "## 账户信息"
   等非模板区块误判为模板并删除。
"""
from pathlib import Path

import config
from web.routers import setup

# ── config._migrate_old_data ─────────────────────────────


def test_migrate_old_data_ignores_workspace_subdir(tmp_path: Path) -> None:
    """新目录只有 workspace 子目录时，config 迁移仍应执行（数据不丢失）。"""
    old = tmp_path / "old-config"
    old.mkdir()
    (old / "agent.json5").write_text("{}\n", encoding="utf-8")
    (old / "agents").mkdir()

    new = tmp_path / "new-config"
    new.mkdir()
    (new / "workspace").mkdir()  # 模拟 WORKSPACE_DIR 顶层 mkdir 预先创建的子目录

    config._migrate_old_data(old, new, "config", ignore_names=("workspace",))

    assert (new / "agent.json5").exists(), "旧 agent.json5 应被迁移"
    assert (new / "agents").is_dir(), "旧 agents 目录应被迁移"


def test_migrate_old_data_no_ignore_skips_nonempty(tmp_path: Path) -> None:
    """不带 ignore_names 时保持原行为：新目录非空即跳过。"""
    old = tmp_path / "old-config"
    old.mkdir()
    (old / "agent.json5").write_text("{}\n", encoding="utf-8")

    new = tmp_path / "new-config"
    new.mkdir()
    (new / "something").mkdir()

    config._migrate_old_data(old, new, "config")
    assert not (new / "agent.json5").exists(), "新目录非空时应跳过迁移"


def test_migrate_old_data_skips_when_only_ignored_name_used(tmp_path: Path) -> None:
    """ignore_names 只忽略指定名字，其他文件仍触发跳过。"""
    old = tmp_path / "old-config"
    old.mkdir()
    (old / "agent.json5").write_text("{}\n", encoding="utf-8")

    new = tmp_path / "new-config"
    new.mkdir()
    (new / "workspace").mkdir()
    (new / "webui_overrides.json").write_text("{}", encoding="utf-8")

    config._migrate_old_data(old, new, "config", ignore_names=("workspace",))
    assert not (new / "agent.json5").exists(), "存在其他文件时应跳过迁移"


# ── setup._is_template_section ───────────────────────────


def test_template_section_exact_titles() -> None:
    """模板区块：固定标题 + 当前称呼标题 + 默认称呼旧格式。"""
    assert setup._is_template_section("偏好设置")
    assert setup._is_template_section("历史交互要点")
    assert setup._is_template_section("爸爸信息", addr="爸爸")
    assert setup._is_template_section("用户信息")  # 默认称呼"用户"，兼容旧格式


def test_template_section_does_not_match_other_info() -> None:
    """非模板区块（标题以"信息"结尾但非当前称呼）不能被误判为模板。"""
    assert not setup._is_template_section("账户信息")
    assert not setup._is_template_section("账户信息", addr="爸爸")
    assert not setup._is_template_section("爸爸信息")  # 默认称呼时"爸爸信息"非模板


# ── setup._preserve_extra_sections ───────────────────────


def test_preserve_extra_sections_keeps_non_template(tmp_path: Path) -> None:
    """重建 USER.md 时保留非模板区块（法律与声明/XP 动态认知/账户信息）。"""
    old_content = (
        "## 爸爸信息\n\n- 称呼：爸爸\n\n"
        "## 法律与声明\n\n- disclaimer_agreed: true\n\n"
        "## XP 动态认知\n\n- 认知：测试\n\n"
        "## 账户信息\n\n- 账号：abc"
    )
    new_content = "## 爸爸信息\n\n- 称呼：爸爸\n\n## 偏好设置\n\n（空）"
    result = setup._preserve_extra_sections(old_content, new_content, addr="爸爸")

    assert "## 法律与声明" in result, "法律与声明区块必须保留"
    assert "## XP 动态认知" in result, "XP 动态认知区块必须保留"
    assert "## 账户信息" in result, "非模板的账户信息区块必须保留"
    assert "disclaimer_agreed: true" in result


def test_preserve_extra_sections_drops_template(tmp_path: Path) -> None:
    """模板区块（当前称呼信息/偏好设置/历史交互要点）不重复保留。"""
    old_content = (
        "## 爸爸信息\n\n- 称呼：爸爸（旧值）\n\n"
        "## 偏好设置\n\n- 旧偏好\n\n"
        "## 历史交互要点\n\n- 旧历史"
    )
    new_content = "## 爸爸信息\n\n- 称呼：爸爸（新值）"
    result = setup._preserve_extra_sections(old_content, new_content, addr="爸爸")

    assert "旧值" not in result, "模板区块内容应被新内容替换"
    assert "旧偏好" not in result
    assert "旧历史" not in result
    assert "（新值）" in result
