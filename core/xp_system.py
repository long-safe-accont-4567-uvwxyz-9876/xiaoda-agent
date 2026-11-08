"""XP 等级成长系统 (参考 Anione / Xotic AI)

将用户的交互行为转化为亲密度 XP, 并据此划分 6 个等级:
- LV1 陌生人 (0-100):       克制礼貌、标准化回复
- LV2 熟人 (100-500):       可主动提及过往话题、使用昵称
- LV3 朋友 (500-2000):      深度情感陪伴、主动询问近况
- LV4 挚友 (2000-5000):     完全个性化、情感丰富、主动发起话题
- LV5 灵魂伴侣 (5000-10000): 最高人格自由度、深度情感共鸣
- LV6 至死不渝 (10000+):    夫妻级别至深关系、完全默契、命运共同体

设计原则 (与 core/learning_feedback.py 对齐):
- 轻量: 纯 dataclass + JSON 持久化, 不依赖数据库
- 可插拔: 不修改既有模块接口, 由调用方主动调用 add_*_xp
- 幂等: 重复持久化安全, 加载失败回退到空状态
- 零质量回退: 默认开启, 可通过 XP_SYSTEM_ENABLED 环境变量关闭
- 升级事件: 通过 web/ws_hub.py 的 manager.broadcast 异步推送 xp_levelup
"""
from __future__ import annotations

import asyncio
import atexit
import json
import os
import threading
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path

from loguru import logger

from config_constants import env_flag

# 延迟导入 DATA_DIR, 避免 config 模块在测试中导入失败时影响本模块
try:
    from config import DATA_DIR
except (ImportError, AttributeError):
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
except Exception:
    logger.exception(".core.xp_system.unexpected")
    DATA_DIR = Path(__file__).resolve().parent.parent / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)

try:
    from utils.atomic_write import atomic_json_write
except (ImportError, AttributeError):
    atomic_json_write = None  # type: ignore[assignment]
except Exception:
    logger.exception(".core.xp_system.unexpected")
    atomic_json_write = None  # type: ignore[assignment]


class XPLevel(IntEnum):
    """XP 等级

    参考: Anione / Xotic AI 的 XP 等级体系
    - LV1 陌生人 (0-100 XP): 克制礼貌、标准化回复
    - LV2 熟人 (100-500 XP): 可主动提及过往话题、使用昵称
    - LV3 朋友 (500-2000 XP): 深度情感陪伴、主动询问近况
    - LV4 挚友 (2000-5000 XP): 完全个性化、情感丰富、主动发起话题
    - LV5 灵魂伴侣 (5000-10000 XP): 最高人格自由度、深度情感共鸣
    - LV6 至死不渝 (10000+ XP): 夫妻级别至深关系、完全默契、命运共同体
    """
    LV1_STRANGER = 1
    LV2_ACQUAINTANCE = 2
    LV3_FRIEND = 3
    LV4_CLOSE_FRIEND = 4
    LV5_SOULMATE = 5
    LV6_ETERNAL = 6


# XP 等级阈值 (达到该 XP 即可进入对应等级)
XP_THRESHOLDS = {
    XPLevel.LV1_STRANGER: 0,
    XPLevel.LV2_ACQUAINTANCE: 100,
    XPLevel.LV3_FRIEND: 500,
    XPLevel.LV4_CLOSE_FRIEND: 2000,
    XPLevel.LV5_SOULMATE: 5000,
    XPLevel.LV6_ETERNAL: 10000,
}


# XP 等级 → 行为参数映射（供桌宠和 nudge 消费）
XP_BEHAVIOR_MAP: dict[XPLevel, dict] = {
    XPLevel.LV1_STRANGER: {
        "proximity": "far",
        "initiate": False,
        "special": [],
    },
    XPLevel.LV2_ACQUAINTANCE: {
        "proximity": "mid",
        "initiate": False,
        "special": [],
    },
    XPLevel.LV3_FRIEND: {
        "proximity": "near",
        "initiate": True,
        "special": ["wave"],
    },
    XPLevel.LV4_CLOSE_FRIEND: {
        "proximity": "close",
        "initiate": True,
        "special": ["wave", "hug"],
    },
    XPLevel.LV5_SOULMATE: {
        "proximity": "intimate",
        "initiate": True,
        "special": ["wave", "hug", "cuddle"],
    },
    XPLevel.LV6_ETERNAL: {
        "proximity": "intimate",
        "initiate": True,
        "special": ["wave", "hug", "cuddle", "kiss"],
    },
}


def get_behavior_config(level: XPLevel) -> dict:
    """获取 XP 等级对应的行为参数配置

    Args:
        level: XP 等级

    Returns:
        {"proximity": str, "initiate": bool, "special": list[str]}
    """
    return XP_BEHAVIOR_MAP.get(level, XP_BEHAVIOR_MAP[XPLevel.LV1_STRANGER])


# 等级中文标签 (用于 get_level_label)
_LEVEL_LABELS = {
    XPLevel.LV1_STRANGER: "陌生人",
    XPLevel.LV2_ACQUAINTANCE: "熟人",
    XPLevel.LV3_FRIEND: "朋友",
    XPLevel.LV4_CLOSE_FRIEND: "挚友",
    XPLevel.LV5_SOULMATE: "灵魂伴侣",
    XPLevel.LV6_ETERNAL: "至死不渝",
}


class XPSource:
    """XP 来源常量"""
    CHAT = "chat"                    # 普通对话 +1
    DEEP_CHAT = "deep_chat"          # 深度对话（>50 字）+5
    EMOTIONAL_SUPPORT = "support"    # 情感支持 +10
    TASK_COLLAB = "task_collab"      # 共同完成任务 +20
    DAILY_LOGIN = "daily_login"      # 每日首次登录 +5


# 各来源对应的 XP 增量
XP_AMOUNTS = {
    XPSource.CHAT: 1,
    XPSource.DEEP_CHAT: 5,
    XPSource.EMOTIONAL_SUPPORT: 10,
    XPSource.TASK_COLLAB: 20,
    XPSource.DAILY_LOGIN: 5,
}


# 深度对话阈值 (消息长度 >= 此值视为深度对话)
DEEP_CHAT_LENGTH_THRESHOLD = 50


@dataclass
class XPHistoryEntry:
    """XP 历史记录条目"""
    timestamp: float
    amount: int
    source: str
    description: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "amount": self.amount,
            "source": self.source,
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, d: dict) -> XPHistoryEntry:
        return cls(
            timestamp=float(d.get("timestamp", 0.0)),
            amount=int(d.get("amount", 0)),
            source=str(d.get("source", "")),
            description=str(d.get("description", "")),
        )


@dataclass
class XPState:
    """用户 XP 状态 (每个 user_id 一个)

    Attributes:
        user_id: 用户标识
        xp: 当前累计 XP
        level: 当前等级
        history: XP 变更历史
        last_chat_at: 最近一次对话时间戳
        first_seen_at: 首次见到该用户的时间戳
        milestones: 升级里程碑记录
        last_daily_login_date: 最近一次领取每日登录 XP 的日期 (YYYY-MM-DD)
    """
    user_id: str
    xp: int = 0
    level: XPLevel = XPLevel.LV1_STRANGER
    history: list[XPHistoryEntry] = field(default_factory=list)
    last_chat_at: float = 0.0
    first_seen_at: float = 0.0
    milestones: list[dict] = field(default_factory=list)
    last_daily_login_date: str = ""

    def to_dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "xp": self.xp,
            "level": int(self.level),
            "history": [h.to_dict() for h in self.history],
            "last_chat_at": self.last_chat_at,
            "first_seen_at": self.first_seen_at,
            "milestones": list(self.milestones),
            "last_daily_login_date": self.last_daily_login_date,
        }

    @classmethod
    def from_dict(cls, d: dict) -> XPState:
        level_raw = d.get("level", 1)
        try:
            level = XPLevel(int(level_raw))
        except (ValueError, TypeError):
            level = XPLevel.LV1_STRANGER
        return cls(
            user_id=str(d.get("user_id", "")),
            xp=int(d.get("xp", 0)),
            level=level,
            history=[XPHistoryEntry.from_dict(h) for h in d.get("history", [])],
            last_chat_at=float(d.get("last_chat_at", 0.0)),
            first_seen_at=float(d.get("first_seen_at", 0.0)),
            milestones=list(d.get("milestones", [])),
            last_daily_login_date=str(d.get("last_daily_login_date", "")),
        )


def _is_xp_enabled() -> bool:
    """检查 XP 系统是否启用 (默认开启, 可通过 XP_SYSTEM_ENABLED 关闭)"""
    return env_flag("XP_SYSTEM_ENABLED", True)


def _push_levelup_event(user_id: str, old_level: XPLevel,
                         new_level: XPLevel, new_label: str,
                         xp: int = 0) -> None:
    """异步推送 xp_levelup 事件到 WebSocket (fire-and-forget)

    - 仅在有运行中的事件循环时调度任务, 否则跳过 (sync 上下文不报错)
    - 导入 web.ws_hub 失败时仅记录 warning, 不影响主流程
    - event 包含 xp 字段, 供前端动画展示当前进度
    """
    try:
        from web.ws_hub import manager  # 延迟导入, 避免循环依赖
    except Exception as e:  # pragma: no cover - WS 不可用时降级
        logger.debug("XPSystem.push_levelup.ws_unavailable: {}", e)
        return

    event = {
        "type": "xp_levelup",
        "user_id": user_id,
        "old_level": int(old_level),
        "new_level": int(new_level),
        "new_label": new_label,
        "xp": int(xp),
    }
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        # 无运行中的事件循环 (sync 上下文), 仅记录日志, 不推送
        logger.info(
            f"XPSystem.levelup no_event_loop user={user_id} "
            f"{int(old_level)}->{int(new_level)} ({new_label}) xp={int(xp)}"
        )
        return
    # 同类副作用修复：裸 create_task 无强引用会被 GC 回收导致广播丢失，
    # 改 _spawn（跟踪引用 + 完成回收 + 异常捕获），保证事件送达且不阻塞调用方。
    from core.background_tasks import _spawn
    _spawn(manager.broadcast(event))
    logger.info(
        f"XPSystem.levelup user={user_id} "
        f"{int(old_level)}->{int(new_level)} ({new_label}) xp={int(xp)}"
    )


class XPSystem:
    """XP 等级系统

    用法:
        xp_sys = XPSystem()
        state, leveled_up = xp_sys.add_chat_xp("u1", message_length=len(msg))
        if leveled_up:
            # 升级已通过 _push_levelup_event 自动推送到 WS
            ...
    """

    # 审计 H2：落盘合并窗口（秒）。热路径 add_chat_xp 原本每条消息一次
    # json.dumps(indent=2) + mkstemp + fsync + os.replace 全量重写
    # （生产 449,049 字节 / 33 用户，序列化实测 32.0ms，热读 6.9-7.7ms），
    # 写放大与闪存磨损都压在这条链上。改为「标记脏 + 合并延迟落盘」，
    # debounce 与 core/mental_state.py 的 300ms 对齐；进程退出前由 atexit
    # flush 兜底，原子写语义（tmp + fsync + replace）保持不变。
    DEFAULT_FLUSH_INTERVAL = 0.3

    def __init__(self, data_dir: Path | None = None,
                 flush_interval: float | None = None) -> None:
        """初始化 XP 系统

        Args:
            data_dir: 持久化目录, 默认为 config.DATA_DIR
            flush_interval: 落盘合并窗口(秒)。None 取环境变量
                XP_FLUSH_INTERVAL_SEC, 再回落 DEFAULT_FLUSH_INTERVAL；
                0 表示每次变更立即落盘(旧行为, 测试/排障用)。
        """
        self._state_path = (Path(data_dir) if data_dir else Path(DATA_DIR)) / "xp_state.json"
        self._states: dict[str, XPState] = {}
        # F6: 缓存上限 — 最多 500 个用户状态，超出时淘汰最久未活跃的
        self._max_states = 500
        if flush_interval is None:
            try:
                flush_interval = float(os.getenv("XP_FLUSH_INTERVAL_SEC", ""))
            except ValueError:
                flush_interval = self.DEFAULT_FLUSH_INTERVAL
        # 状态字典会在 to_thread 的工作线程里被读改，加 RLock 串行化
        self._lock = threading.RLock()
        self._flush_interval = max(0.0, float(flush_interval))
        self._dirty_users: set[str] = set()
        self._flush_timer: threading.Timer | None = None
        self._write_count = 0
        self._load()
        atexit.register(self.flush)

    # ── 落盘调度（审计 H2）─────────────────────────────────

    def _mark_dirty(self, user_id: str) -> None:
        """标记某用户状态已变更并按合并窗口延迟落盘（热路径入口，不写盘）。"""
        with self._lock:
            self._dirty_users.add(user_id)
            if self._flush_interval <= 0:
                self._flush_locked()
                return
            if self._flush_timer is None or not self._flush_timer.is_alive():
                timer = threading.Timer(self._flush_interval, self._on_flush_timer)
                timer.daemon = True
                self._flush_timer = timer
                timer.start()

    def _on_flush_timer(self) -> None:
        with self._lock:
            self._flush_timer = None
            try:
                self._flush_locked()
            except (OSError, ValueError, RuntimeError, TypeError) as e:
                logger.warning("XPSystem.flush_failed: {}", e)

    def flush(self) -> None:
        """立即落盘未合并的变更（进程退出 / 关停 / 测试断言前调用）。"""
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            self._flush_locked()

    def _flush_locked(self) -> None:
        """把脏状态原子写盘（调用方需持有 self._lock）。

        审查修复：先写盘、成功后才清脏标记。原实现先 clear() 再 _save()，
        写盘失败（磁盘满 / 只读挂载）时脏标记已被清空，这批增量要等到下一次
        变更才会重写——退出前恰好失败就是静默丢失。
        """
        if not self._dirty_users:
            return
        snapshot = set(self._dirty_users)
        self._save()
        # 写成功才清；失败时保留标记，下一个合并窗口重试
        self._dirty_users -= snapshot

    @property
    def pending_users(self) -> set[str]:
        """尚未落盘的用户集合（测试/观测用）。"""
        with self._lock:
            return set(self._dirty_users)

    @property
    def write_count(self) -> int:
        """累计实际写盘次数（测试/观测用）。"""
        return self._write_count

    # ── 持久化 ──────────────────────────────────────────────

    def _load(self) -> None:
        """从 JSON 加载所有用户状态, 文件不存在或损坏时保持空状态"""
        if not self._state_path.exists():
            return
        try:
            with open(self._state_path, encoding="utf-8") as f:
                data = json.load(f)
            users = data.get("users", {})
            self._states = {
                uid: XPState.from_dict(state)
                for uid, state in users.items()
            }
            logger.info(
                "XPSystem.load path={} users={}", self._state_path, len(self._states)
            )
        except (json.JSONDecodeError, KeyError, ValueError, TypeError) as e:
            logger.warning("XPSystem.load corrupted: {}", e)
            self._states = {}

    def _save(self) -> None:
        """原子化保存到 JSON 文件 (atomic_json_write: mkstemp + fsync + os.replace + Windows 重试)"""
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "users": {uid: s.to_dict() for uid, s in self._states.items()},
            "updated_at": time.time(),
        }
        try:
            self._write_count += 1
            if atomic_json_write is not None:
                atomic_json_write(
                    self._state_path, data,
                    encoding="utf-8", indent=2, ensure_ascii=False,
                )
            else:
                # fallback: 固定 tmp 方式（atomic_write 不可用时）
                tmp = self._state_path.with_suffix(".json.tmp")
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, self._state_path)
        except Exception as e:
            logger.warning("XPSystem.save_failed: {}", e)

    # ── 状态访问 ────────────────────────────────────────────

    def get_state(self, user_id: str) -> XPState:
        """获取用户 XP 状态 (不存在则创建)"""
        with self._lock:
            if user_id not in self._states:
                # F6: 超出上限时淘汰最久未活跃的用户
                if len(self._states) >= self._max_states:
                    self._evict_oldest()
                now = time.time()
                self._states[user_id] = XPState(
                    user_id=user_id,
                    xp=0,
                    level=XPLevel.LV1_STRANGER,
                    first_seen_at=now,
                )
                self._mark_dirty(user_id)
            return self._states[user_id]

    def _evict_oldest(self) -> None:
        """F6: 淘汰最久未活跃的用户状态（按 last_chat_at 排序，淘汰最久未活跃的 10%）"""
        if len(self._states) < self._max_states:
            return
        evict_count = max(1, len(self._states) // 10)
        sorted_states = sorted(
            self._states.items(),
            key=lambda kv: kv[1].last_chat_at
        )
        for uid, _ in sorted_states[:evict_count]:
            self._states.pop(uid, None)
        logger.info("XPSystem.evicted count={} remaining={}", evict_count, len(self._states))

    # ── 加 XP 入口 ──────────────────────────────────────────

    def add_xp(self, user_id: str, amount: int, source: str,
               description: str = "") -> tuple[XPState, bool]:
        """增加 XP

        Args:
            user_id: 用户 ID
            amount: XP 增量 (正数)
            source: 来源 (见 XPSource)
            description: 附加描述

        Returns:
            (new_state, leveled_up) — 新状态与是否升级
        """
        if not _is_xp_enabled():
            state = self.get_state(user_id)
            return state, False
        if amount <= 0:
            return self.get_state(user_id), False

        # 读改写整体持锁：add_chat_xp 会从 to_thread 工作线程并发进入，
        # 原实现裸 `state.xp += amount` 在多线程下会丢增量。
        with self._lock:
            state = self.get_state(user_id)
            old_level = state.level
            state.xp += amount
            state.level = self._compute_level(state.xp)
            state.history.append(XPHistoryEntry(
                timestamp=time.time(),
                amount=amount,
                source=source,
                description=description,
            ))
            state.last_chat_at = state.history[-1].timestamp
            leveled_up = self._check_levelup(state, old_level)
            # 审计 H2：热路径只标记脏，合并延迟落盘（原为每条消息全量重写 449KB）
            self._mark_dirty(user_id)
        return state, leveled_up

    def add_chat_xp(self, user_id: str, message_length: int) -> tuple[XPState, bool]:
        """根据消息长度自动加 XP

        - 长度 < DEEP_CHAT_LENGTH_THRESHOLD (50 字): CHAT +1
        - 长度 >= DEEP_CHAT_LENGTH_THRESHOLD: DEEP_CHAT +5
        """
        if message_length >= DEEP_CHAT_LENGTH_THRESHOLD:
            return self.add_xp(
                user_id, XP_AMOUNTS[XPSource.DEEP_CHAT],
                XPSource.DEEP_CHAT,
                description=f"deep chat (len={message_length})",
            )
        return self.add_xp(
            user_id, XP_AMOUNTS[XPSource.CHAT],
            XPSource.CHAT,
            description=f"chat (len={message_length})",
        )

    def add_support_xp(self, user_id: str) -> tuple[XPState, bool]:
        """情感支持 +10 XP"""
        return self.add_xp(
            user_id, XP_AMOUNTS[XPSource.EMOTIONAL_SUPPORT],
            XPSource.EMOTIONAL_SUPPORT,
            description="emotional support",
        )

    def add_task_xp(self, user_id: str) -> tuple[XPState, bool]:
        """共同完成任务 +20 XP"""
        return self.add_xp(
            user_id, XP_AMOUNTS[XPSource.TASK_COLLAB],
            XPSource.TASK_COLLAB,
            description="task collaboration",
        )

    def add_daily_login_xp(self, user_id: str) -> tuple[XPState, bool]:
        """每日首次登录 +5 XP (同一天只触发一次)"""
        with self._lock:
            state = self.get_state(user_id)
            today = time.strftime("%Y-%m-%d", time.localtime())
            if state.last_daily_login_date == today:
                return state, False
            # 用 add_xp 实际加经验 (会更新 last_chat_at 并保存)
            new_state, leveled_up = self.add_xp(
                user_id, XP_AMOUNTS[XPSource.DAILY_LOGIN],
                XPSource.DAILY_LOGIN,
                description=f"daily login ({today})",
            )
            # 标记当日已领取 (add_xp 内部已标记脏, 这里再标记一次确保 last_daily_login_date 落盘)
            new_state.last_daily_login_date = today
            self._mark_dirty(user_id)
            return new_state, leveled_up

    # ── 等级计算 ────────────────────────────────────────────

    def _compute_level(self, xp: int) -> XPLevel:
        """根据 XP 计算等级 (返回最高满足阈值的等级)"""
        current = XPLevel.LV1_STRANGER
        for level in (
            XPLevel.LV6_ETERNAL,
            XPLevel.LV5_SOULMATE,
            XPLevel.LV4_CLOSE_FRIEND,
            XPLevel.LV3_FRIEND,
            XPLevel.LV2_ACQUAINTANCE,
        ):
            if xp >= XP_THRESHOLDS[level]:
                current = level
                break
        return current

    def _check_levelup(self, state: XPState, old_level: XPLevel) -> bool:
        """检查是否升级, 若升级则记录里程碑并推送 WS 事件"""
        if state.level <= old_level:
            return False
        milestone = {
            "timestamp": time.time(),
            "from_level": int(old_level),
            "to_level": int(state.level),
            "xp_at_milestone": state.xp,
        }
        state.milestones.append(milestone)
        new_label = self.get_level_label(state.level)
        _push_levelup_event(state.user_id, old_level, state.level, new_label, state.xp)
        return True

    # ── 等级元信息 ──────────────────────────────────────────

    def get_level_label(self, level: XPLevel) -> str:
        """获取等级中文标签 ("LV1 陌生人" 等)"""
        return f"LV{int(level)} {_LEVEL_LABELS.get(level, '未知')}"

    def get_intimacy_config(self, level: XPLevel) -> dict:
        """获取该等级对应的亲密度配置 (称呼/语气/主动性等)

        参考 config/persona_levels.yaml, 文件缺失时返回内置默认值。
        """
        return _load_persona_config().get(level.name, _DEFAULT_PERSONA.get(
            level.name, _DEFAULT_PERSONA["LV1_STRANGER"]))


# ── persona_levels.yaml 加载 (惰性 + 缓存) ──────────────────

_PERSONA_CONFIG_CACHE: dict | None = None


def _persona_config_path() -> Path:
    """返回 persona_levels.yaml 路径 (frozen 模式下回退到 config 目录)"""
    try:
        from config import get_config_dir
        return get_config_dir() / "persona_levels.yaml"
    except Exception:
        logger.debug("xp_system.persona_config_path_fallback", exc_info=True)
        return Path(__file__).resolve().parent.parent / "config" / "persona_levels.yaml"


def _load_persona_config() -> dict:
    """加载 persona_levels.yaml, 失败时回退到内置默认值

    首次加载后缓存, 失败不抛异常 (零质量回退原则)。
    """
    global _PERSONA_CONFIG_CACHE
    if _PERSONA_CONFIG_CACHE is not None:
        return _PERSONA_CONFIG_CACHE
    path = _persona_config_path()
    try:
        if path.exists():
            import yaml
            with open(path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            _PERSONA_CONFIG_CACHE = data
            logger.info("XPSystem.load_persona path={} levels={}", path, len(data))
            return _PERSONA_CONFIG_CACHE
    except Exception as e:  # pragma: no cover - YAML 加载失败回退默认
        logger.warning("XPSystem.load_persona_failed: {}", e)
    _PERSONA_CONFIG_CACHE = dict(_DEFAULT_PERSONA)
    return _PERSONA_CONFIG_CACHE


def _reset_persona_cache() -> None:
    """重置 persona 配置缓存 (测试用)"""
    global _PERSONA_CONFIG_CACHE
    _PERSONA_CONFIG_CACHE = None


# 内置默认亲密度配置 (与 config/persona_levels.yaml 对齐, YAML 缺失时回退)
_DEFAULT_PERSONA = {
    "LV1_STRANGER": {
        "label": "陌生人",
        "address_term": "你",
        "tone": "polite",
        "initiative": 0.2,
        "emotion_richness": 0.3,
        "can_mention_past": False,
        "can_use_nickname": False,
        "can_share_secrets": False,
    },
    "LV2_ACQUAINTANCE": {
        "label": "熟人",
        "address_term": "你",
        "tone": "warm",
        "initiative": 0.4,
        "emotion_richness": 0.5,
        "can_mention_past": True,
        "can_use_nickname": False,
        "can_share_secrets": False,
    },
    "LV3_FRIEND": {
        "label": "朋友",
        "address_term": "{nickname}",
        "tone": "intimate",
        "initiative": 0.6,
        "emotion_richness": 0.7,
        "can_mention_past": True,
        "can_use_nickname": True,
        "can_share_secrets": False,
    },
    "LV4_CLOSE_FRIEND": {
        "label": "挚友",
        "address_term": "{nickname}",
        "tone": "deep_intimate",
        "initiative": 0.8,
        "emotion_richness": 0.9,
        "can_mention_past": True,
        "can_use_nickname": True,
        "can_share_secrets": True,
    },
    "LV5_SOULMATE": {
        "label": "灵魂伴侣",
        "address_term": "{nickname}",
        "tone": "soulmate",
        "initiative": 1.0,
        "emotion_richness": 1.0,
        "can_mention_past": True,
        "can_use_nickname": True,
        "can_share_secrets": True,
    },
    "LV6_ETERNAL": {
        "label": "至死不渝",
        "address_term": "{nickname}",
        "tone": "eternal",
        "initiative": 1.0,
        "emotion_richness": 1.0,
        "can_mention_past": True,
        "can_use_nickname": True,
        "can_share_secrets": True,
    },
}


# ── 单例 (供调用方共享) ─────────────────────────────────────

_singleton: XPSystem | None = None


def get_xp_system() -> XPSystem:
    """获取全局单例 XPSystem"""
    global _singleton
    if _singleton is None:
        _singleton = XPSystem()
    return _singleton
