# tests/test_behavioral_direction.py
import os
import tempfile
from pathlib import Path

from core.behavioral_direction import DirectionRegistry, DirectionVector


def test_direction_vector_creation():
    d = DirectionVector("helpfulness", {"prompt": 0.3, "route": 0.2}, "manual")
    assert d.name == "helpfulness"
    assert d.dimensions["prompt"] == 0.3
    assert d.source == "manual"
    assert d.magnitude == 1.0


def test_direction_vector_mul_scalar():
    d = DirectionVector("calm", {"emotion": -0.4, "prompt": 0.2}, "manual")
    scaled = d * 0.5
    assert scaled.dimensions["emotion"] == -0.2
    assert scaled.dimensions["prompt"] == 0.1
    assert scaled.magnitude == 0.5


def test_direction_vector_add():
    d1 = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
    d2 = DirectionVector("safety", {"prompt": 0.2, "tool": -0.3}, "manual")
    merged = d1 + d2
    assert merged.dimensions["prompt"] == 0.5
    assert merged.dimensions["tool"] == -0.3


def test_apply_to_context_prompt():
    d = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
    context = {"existing": "value"}
    result = d.apply_to_context(context)
    assert result["prompt_modifier"] == 0.3
    assert result["existing"] == "value"


def test_apply_to_context_all_dimensions():
    d = DirectionVector("test", {"prompt": 0.1, "tool": 0.2, "emotion": -0.3, "route": 0.4}, "manual")
    context = {}
    result = d.apply_to_context(context)
    assert result["prompt_modifier"] == 0.1
    assert result["tool_bias"] == 0.2
    assert result["emotion_offset"] == -0.3
    assert result["route_bias"] == 0.4


def test_apply_to_context_cumulative():
    d1 = DirectionVector("a", {"prompt": 0.3}, "manual")
    d2 = DirectionVector("b", {"prompt": 0.2}, "manual")
    context = {}
    context = d1.apply_to_context(context)
    context = d2.apply_to_context(context)
    assert context["prompt_modifier"] == 0.5


def test_save_and_load():
    d = DirectionVector("test_dir", {"prompt": 0.3, "emotion": -0.2}, "manual", 0.8, {"auc": 0.9})
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
        path = f.name
    try:
        d.save(path)
        loaded = DirectionVector.load(path)
        assert loaded.name == "test_dir"
        assert loaded.dimensions["prompt"] == 0.3
        assert loaded.dimensions["emotion"] == -0.2
        assert loaded.source == "manual"
        assert loaded.magnitude == 0.8
        assert loaded.meta["auc"] == 0.9
    finally:
        os.unlink(path)


def test_registry_register_and_get():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        registry = DirectionRegistry(storage_path=path)
        d = DirectionVector("helpfulness", {"prompt": 0.3}, "manual")
        registry.register(d)
        assert "helpfulness" in registry.list_directions()
        got = registry.get("helpfulness")
        assert got is not None
        assert got.dimensions["prompt"] == 0.3


def test_registry_get_nonexistent():
    registry = DirectionRegistry()
    assert registry.get("nonexistent") is None


def test_registry_persistence():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        registry1 = DirectionRegistry(storage_path=path)
        registry1.register(DirectionVector("calm", {"emotion": -0.4}, "manual"))
        # 新实例从同一文件加载
        registry2 = DirectionRegistry(storage_path=path)
        assert "calm" in registry2.list_directions()
        assert registry2.get("calm").dimensions["emotion"] == -0.4


def test_registry_load_corrupted_falls_back():
    with tempfile.TemporaryDirectory() as tmpdir:
        path = os.path.join(tmpdir, "registry.json")
        Path(path).write_text("invalid json {{{")
        registry = DirectionRegistry(storage_path=path)
        # 损坏文件不应崩溃，返回空注册表
        assert registry.list_directions() == []


def test_registry_load_valid_json_wrong_type_falls_back():
    """Valid JSON of wrong type (null/list/int) must not crash the registry."""
    for content in ("null", "[]", "42", "\"string\"", "true"):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = os.path.join(tmpdir, "registry.json")
            Path(path).write_text(content)
            registry = DirectionRegistry(storage_path=path)
            assert registry.list_directions() == [], f"failed for content: {content}"
