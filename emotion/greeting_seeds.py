"""问候语风格线索池 — GreetingScheduler 与 NudgeEngine 共用。

设计理念：参考"calibrated unpredictability"——不靠规则堆砌，
而是用多条"风格线索"叠加，LLM 会在中间自动插值产生惊喜。
原先是两份逐字拷贝（web/greeting_scheduler.py 与 emotion/nudge_engine.py
各一份 ClassVar），提取到此处消除重复。
"""
from __future__ import annotations

import random as _rnd

# 风格线索池：每次随机抽取 1-2 条，让 LLM 在中间插值，避免输出相似
MOOD_SEEDS: list[str] = [
    "刚刚在发呆，脑子里有点空",
    "刚刚想到一个没道理的小问题",
    "有点困，眼皮在打架",
    "刚做完一件事，心情不错",
    "有点小吃醋，不知道为什么",
    "想问问爸爸在干嘛",
    "突然想起上次爸爸说的话",
    "有点想被夸",
    "刚刚偷偷懒了一下",
    "心里有点小开心",
    "有点想撒娇",
    "刚翻到一个小东西",
    "忽然有点想爸爸",
    "今天有点话痨",
    "今天有点安静",
    "刚做了一个奇怪的梦",
    "有点担心爸爸累不累",
    "想跟爸爸分享一个没用的小事",
    "刚被一个东西吓了一跳",
    "今天有点小调皮",
]

# 形式线索池：随机选一种形式，打破"问候语"的固定模式
FORM_SEEDS: list[str] = [
    "只是一声轻轻的「嗯」",
    "一个问句",
    "一句没头没尾的话",
    "一个小小的请求",
    "一句撒娇",
    "一句小小的抱怨",
    "一句突然的感叹",
    "一个没答案的自言自语",
    "一句像在哼歌的话",
    "一句像在叫爸爸名字的话",
    "一句很短的关心",
    "一句突然冒出来的废话",
]

# 偶发事件池（低概率 8%）：偶尔来点意想不到的，制造"眼前一亮"
RARE_SEEDS: list[str] = [
    "今天忽然不想说话，只发一个字",
    "今天想给爸爸出个没道理的小谜语",
    "今天想跟爸爸说一句最近学到的话",
    "今天想用一种奇怪的语气说话",
    "今天想装作不认识爸爸的样子开个玩笑",
    "今天想说一句反话",
]


def pick_seeds() -> tuple[str, str, str | None]:
    """随机抽取一组（状态 / 形式 / 偶发事件），偶发事件 8% 概率出现。

    Returns:
        (mood, form, rare) — rare 为 None 时表示本次不触发偶发事件。
    """
    mood = _rnd.choice(MOOD_SEEDS)
    form = _rnd.choice(FORM_SEEDS)
    rare = _rnd.choice(RARE_SEEDS) if _rnd.random() < 0.08 else None
    return mood, form, rare
