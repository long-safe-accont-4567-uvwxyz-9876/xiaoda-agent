"""security/recovery_qa.py 单元测试。

全部通过 monkeypatch 把恢复文件重定向到 tmp_path，绝不触碰真实
credentials/ 下的文件。
"""
from __future__ import annotations

import json
import os

import pytest

import security.recovery_qa as rqa


@pytest.fixture
def cred_dir(tmp_path, monkeypatch):
    """将恢复文件路径重定向到临时目录。"""
    monkeypatch.setattr(rqa, "_get_path", lambda: tmp_path / "webui_recovery.json")
    return tmp_path


def test_set_get_verify_clear_flow(cred_dir):
    """set/get/verify/clear 全流程。"""
    assert rqa.get_question() is None
    assert rqa.verify_answer("anything") is False

    rqa.set_recovery("我的第一只宠物叫什么？", "miaomiao")
    assert rqa.get_question() == "我的第一只宠物叫什么？"
    assert rqa.verify_answer("miaomiao") is True
    assert rqa.verify_answer("wrong-answer") is False

    # 文件格式：答案绝不落明文
    path = cred_dir / "webui_recovery.json"
    raw = path.read_text(encoding="utf-8")
    data = json.loads(raw)
    assert "miaomiao" not in raw
    assert set(data) >= {"question", "salt", "iterations", "answer_hash"}
    assert data["iterations"] == 200000
    assert len(bytes.fromhex(data["salt"])) == 16

    rqa.clear_recovery()
    assert not path.exists()
    assert rqa.get_question() is None
    assert rqa.verify_answer("miaomiao") is False


def test_file_permission_0600(cred_dir):
    rqa.set_recovery("q", "answer123")
    path = cred_dir / "webui_recovery.json"
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_question_stripped_and_stored(cred_dir):
    rqa.set_recovery("  我的宠物叫什么？  ", "  miaomiao  ")
    assert rqa.get_question() == "我的宠物叫什么？"
    assert rqa.verify_answer("miaomiao") is True
    assert rqa.verify_answer("  miaomiao  ") is True


def test_invalid_inputs_raise_value_error(cred_dir):
    with pytest.raises(ValueError):
        rqa.set_recovery("", "answer")
    with pytest.raises(ValueError):
        rqa.set_recovery("   ", "answer")
    with pytest.raises(ValueError):
        rqa.set_recovery("q" * 201, "answer")
    with pytest.raises(ValueError):
        rqa.set_recovery("q", "a")
    # 校验失败不应写出文件
    assert not (cred_dir / "webui_recovery.json").exists()


def test_corrupted_json_returns_none(cred_dir):
    (cred_dir / "webui_recovery.json").write_text("{not valid json", encoding="utf-8")
    assert rqa.get_question() is None
    assert rqa.verify_answer("x") is False


def test_wrong_shape_returns_none(cred_dir):
    # 缺字段
    (cred_dir / "webui_recovery.json").write_text('{"question": "q"}', encoding="utf-8")
    assert rqa.get_question() is None
    # 非 dict
    (cred_dir / "webui_recovery.json").write_text('["not", "a", "dict"]', encoding="utf-8")
    assert rqa.get_question() is None


def test_clear_when_missing_is_noop(cred_dir):
    rqa.clear_recovery()  # 幂等，不应抛异常
    assert rqa.get_question() is None


def test_rotate_recovery_overwrites_old(cred_dir):
    rqa.set_recovery("旧问题", "old-answer")
    rqa.set_recovery("新问题", "new-answer")
    assert rqa.get_question() == "新问题"
    assert rqa.verify_answer("new-answer") is True
    assert rqa.verify_answer("old-answer") is False
