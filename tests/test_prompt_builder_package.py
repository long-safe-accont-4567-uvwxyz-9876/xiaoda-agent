"""prompt_builder 包拆分契约测试（2026-08-25 技术债专项 P2）。

单文件 prompt_builder.py（1669 行）拆为包后,守护三条兼容契约:
1. 历史顶层名称全部可从门面 import（re-export 面不破,含 __all__ 成员）;
2. 可变缓存状态单一事实源在门面——生产代码经 _pkg 前缀访问同一绑定,
   monkeypatch.setattr(prompt_builder, ...) 对生产路径生效（防双命名空间分裂,
   test_prompt_integration / test_harness_verification 的 patch 行为依赖此契约）;
3. clear_module_cache() 跨区清空 workspace/scene 两侧缓存。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import prompt_builder as pb


def test_all_declared_names_importable():
    """__all__ 里每个名字都必须真实存在于门面命名空间（F822 防腐）。"""
    missing = [n for n in pb.__all__ if not hasattr(pb, n)]
    assert not missing, f"__all__ 含不存在的名称: {missing}"


def test_historical_top_level_names_intact():
    """原单文件的全部公开 API 与关键私有名（tests/外部消费方引用面）。"""
    required = (
        # 公开入口
        "build_system_prompt", "build_safe_system_prompt",
        "build_scene_aware_prompt", "load_skills", "load_workspace_file",
        "clear_module_cache", "get_scene_cache_stats", "reset_scene_cache",
        # tests 直接引用的私有协作函数/常量
        "_classify_scene", "_build_stable_prompt", "_build_dynamic_prompt",
        "_inject_canary", "_cache_lock", "_canary_manager",
        "_SYSTEM_PROMPT_CACHE", "_SAFE_PROMPT_CACHE", "_stable_prompt_cache",
        "_module_cache", "_scene_prompt_cache", "_current_scene_sig",
        "_BASE_STICKINESS_THRESHOLD", "_MD_MODULES", "_BUCKET_ORDERINGS",
    )
    missing = [n for n in required if not hasattr(pb, n)]
    assert not missing, f"兼容面缺失: {missing}"


def test_mutable_state_single_source_of_truth(monkeypatch):
    """可变状态必须门面与子模块同源——patch 门面对生产写入点可见。

    生产代码(_prompt_scene)在函数内 `import prompt_builder as _pkg` 后读写
    门面绑定;若子模块另持私有副本(双命名空间分裂),场景粘性语义会静默失效
    (test_scene_stickiness 读门面 _current_scene_sig 恒为 ())。
    行为级验证:生产函数 _build_scene_middle 写入后,门面绑定同步变化。
    """
    from prompt_builder import _prompt_scene

    pb.reset_scene_cache()
    sig = ("function_bucket", "USER.md")
    _prompt_scene._build_scene_middle(sig, {}, "爸爸", "小妲")
    assert pb._current_scene_sig == sig, (
        "生产写入应落在门面绑定上——双命名空间分裂"
    )


def test_clear_module_cache_clears_both_regions():
    """跨区缓存清空:workspace 模块缓存 + scene 场景缓存同时归零。"""
    pb._prompt_workspace._module_cache["__probe__"] = "x"
    pb._scene_prompt_cache[("__probe__",)] = "y"
    pb.clear_module_cache()
    assert "__probe__" not in pb._prompt_workspace._module_cache
    assert ("__probe__" not in pb._scene_prompt_cache)


def test_config_lazy_forwarding_still_works():
    """config.py 的 PEP 562 懒转发链（config.__getattr__ → 本包）不断裂。"""
    from config import build_system_prompt  # noqa: F401
