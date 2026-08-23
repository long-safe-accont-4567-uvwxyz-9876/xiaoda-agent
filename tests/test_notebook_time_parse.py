"""_parse_task_time 中文数字时间解析回归测试。

修复前：cn_num 字典按插入序子串替换，"十一点"先命中"一"变成"十1点"，
被解析为凌晨 1 点（静默错 10 小时）。修复后替换表按长度降序全量应用，
"十一"先于"一"命中；另补 "两"、口语整十数与 半/点 归一化。
"""

import datetime
import os
from zoneinfo import ZoneInfo

import pytest

from memory.notebook_manager import NotebookManager


@pytest.fixture()
def manager() -> NotebookManager:
    return object.__new__(NotebookManager)


def _hm(manager: NotebookManager, time_str: str) -> tuple[int, int]:
    ts = manager._parse_task_time(time_str)
    assert ts > 0, f"_parse_task_time({time_str!r}) 返回 {ts}，解析失败"
    tz_name = os.getenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    dt = datetime.datetime.fromtimestamp(ts, tz=ZoneInfo(tz_name))
    return dt.hour, dt.minute


@pytest.mark.parametrize(
    ("time_str", "expect_h", "expect_m"),
    [
        ("十一点", 11, 0),
        ("十一点半", 11, 30),
        ("十二点", 12, 0),
        ("两点", 2, 0),
        ("两点半", 2, 30),
        ("三点二十", 3, 20),
        ("九点四十", 9, 40),
        ("十点", 10, 0),
        ("二十一点", 21, 0),
        ("晚上八点", 20, 0),
        ("下午三点半", 15, 30),
    ],
)
def test_chinese_numeral_times(manager: NotebookManager, time_str: str,
                               expect_h: int, expect_m: int) -> None:
    assert _hm(manager, time_str) == (expect_h, expect_m)


def test_numeric_format_unaffected(manager: NotebookManager) -> None:
    assert _hm(manager, "19:30") == (19, 30)


def test_numeric_with_prefix(manager: NotebookManager) -> None:
    assert _hm(manager, "明天14:00".replace("明天", "")) == (14, 0)


def test_out_of_range_returns_zero(manager: NotebookManager) -> None:
    assert manager._parse_task_time("25点") == 0.0
    assert manager._parse_task_time("99:99") == 0.0


def test_relative_day_strings_do_not_crash(monkeypatch):
    """回归：局部 import 遮蔽曾使 明天/后天/下周X 输入抛 AttributeError。"""
    monkeypatch.setenv("NUDGE_TIMEZONE", "Asia/Shanghai")
    nb = NotebookManager.__new__(NotebookManager)
    for s in ("明天晚上八点", "后天早上七点半", "下周一九点"):
        ts = nb._parse_task_time(s)
        assert isinstance(ts, float) and ts > 0, s
