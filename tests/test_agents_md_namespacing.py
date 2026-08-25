"""根目录开发者契约与 workspace 业务 AGENTS.md 互不相扰守卫。

背景:项目运行时在 config/workspace/AGENTS.md 维护小妲的团队规则业务文件
(启动时 _init_workspace_templates 强制更新、load_workspace_file 注入
system prompt、_BUCKET_ORDERINGS 场景分桶依赖)。2026-08-25 曾短暂在仓库
根创建同名开发者契约,虽经核验路径不重叠(打包只收 config/ 子目录,
load 只读 WORKSPACE_DIR),但重名是长期混淆源——某次"同步文档到 config"
类操作可能误覆盖业务文件。

本守卫钉死三条不变式:
1. 根目录不存在 AGENTS.md(开发契约已改名 CONTRIBUTING-AGENTS.md);
2. 打包源 config/workspace/AGENTS.md 是业务版(含团队规则标记),
   且打包清单(xiaoda-agent.spec)只收 config/ 子目录,不含仓库根散文件;
3. 开发契约文件存在且自指正确。
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_repo_root_has_no_agents_md():
    """根目录不得出现 AGENTS.md——与运行时业务文件同名即混淆源。"""
    assert not (ROOT / "AGENTS.md").exists(), (
        "仓库根出现了 AGENTS.md。config/workspace/AGENTS.md 是运行时 Agent "
        "业务文件(注入 system prompt),根目录同名文件是长期混淆源;"
        "开发者契约请写入 CONTRIBUTING-AGENTS.md"
    )


def test_dev_contract_exists_and_renamed():
    dev = ROOT / "CONTRIBUTING-AGENTS.md"
    assert dev.exists(), "开发者行为契约缺失(CONTRIBUTING-AGENTS.md)"
    content = dev.read_text(encoding="utf-8")
    assert "CONTRIBUTING-AGENTS.md" in content, "契约应自指新文件名"


def test_bundled_business_agents_md_is_intact():
    """打包源的业务版必须仍是小妲团队规则(防被开发内容覆盖)。"""
    biz = ROOT / "config" / "workspace" / "AGENTS.md"
    assert biz.exists(), "业务模板 config/workspace/AGENTS.md 缺失"
    content = biz.read_text(encoding="utf-8")
    # 业务版特征:总行为规则标题 + address_term 占位符(注入链路依赖)
    assert "# AGENTS.md - 总行为规则" in content, (
        "config/workspace/AGENTS.md 内容异常——疑似被开发者契约覆盖,"
        "该文件是 system prompt 注入源,牵连场景分桶/强制更新链路"
    )
    assert "{address_term}" in content


def test_spec_does_not_bundle_repo_root_files():
    """spec 的 datas 白名单断言:收集点只许是 config / web/dist / web/splash / assets(静态资源)。

    防止未来有人加 `SPECPATH` 整树或根目录散文件(如把开发者契约打进包,
    与运行时业务 AGENTS.md 混居 _internal)。
    """
    import re
    spec = (ROOT / "xiaoda-agent.spec").read_text(encoding="utf-8")
    collected = re.findall(r"_tree_datas\(\s*os\.path\.join\(SPECPATH,\s*([^)]+)\)", spec)
    allowed_prefixes = ("'config'", "'web'", "'assets'", '"config"', '"web"', '"assets"')
    bad = [c.strip() for c in collected if not c.strip().startswith(allowed_prefixes)]
    assert not bad, (
        f"spec 新增了非白名单目录的打包: {bad}——"
        "仓库根散文件(尤其任何 AGENTS.md)不得进包"
    )
    assert collected, "未找到任何 _tree_datas 收集点(spec 结构变更?请同步本守卫)"
