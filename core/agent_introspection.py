"""Agent 状态自省 (A1 元认知: P2 自我意识)

参考:
- Self-Awareness in LLM Agents (Anthropic Constitutional AI)
- core/metacognition_lite.py (5 阶段元认知引擎)
- core/self_diagnostic.py (主动自诊断)
- core/degradation_strategy.py (4 级降级)

特性:
- 收集 Agent 当前内心状态: 认知负载/置信度/情绪/降级级别/健康度
- 不强依赖任何模块, 缺失时用默认值 (优雅降级)
- 提供文本/JSON 两种输出格式
- 供 /health/self 接口与 /self 斜杠命令复用

用法:
    intro = AgentIntrospector(context=ctx, agent=core)
    state = intro.get_current_state()
    print(AgentIntrospector.to_text(state))
    json_data = AgentIntrospector.to_dict(state)
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field, asdict
from typing import Any

from loguru import logger

_pending_tasks: set[asyncio.Task] = set()


def _fire_and_forget(coro) -> asyncio.Task:
    """安全创建后台 Task，持有引用防止 GC 回收。"""
    task = asyncio.create_task(coro)
    _pending_tasks.add(task)
    task.add_done_callback(_pending_tasks.discard)
    return task

# J-Space Hook: 行为信号流采集 (非阻塞, 失败不影响主流程)
try:
    from config import ENABLE_J_SPACE_HOOKS
    if ENABLE_J_SPACE_HOOKS:
        from core.behavioral_signal import BehavioralSignalStream
        _signal_stream: "BehavioralSignalStream | None" = None
    else:
        _signal_stream = None
except ImportError:
    _signal_stream = None


# ============================================================
# 状态数据结构
# ============================================================

@dataclass
class AgentState:
    """Agent 内心状态快照

    所有数值字段在依赖缺失时使用默认值, 保证可序列化。
    """
    # 认知状态
    cognitive_load: float = 0.0          # 0-1, 当前认知负载
    confidence: float = 0.5              # 0-1, 当前置信度
    active_goals: list[str] = field(default_factory=list)  # 当前活跃目标
    emotional_state: str = "平静"        # 当前情绪
    # 元认知
    metacog_phase: str = "idle"          # anticipate/plan/monitor/reflect/regulate/idle
    reflection_count: int = 0            # 本次会话反思次数
    lessons_active: int = 0              # 活跃教训数
    # 服务状态
    degradation_level: int = 0          # 当前降级级别 0-3
    health_score: float = 5.0           # 健康度评分 1-5
    uptime: float = 0.0                 # 运行时间秒
    last_error: str | None = None    # 最后一次错误
    timestamp: float = field(default_factory=time.time)


# ============================================================
# 自省器
# ============================================================

# 模块启动时间 (用于计算 uptime)
_MODULE_START_TIME: float = time.time()


class AgentIntrospector:
    """Agent 状态自省器

    通过可选注入的依赖收集状态, 缺失依赖时使用默认值。

    Args:
        context: AgentContext, 用于读取活跃目标/情绪提示
        agent: AgentCore 或类似对象, 用于读取运行时间/最近错误
        start_time: 启动时间戳 (优先于 agent 的 _start_time)
    """

    def __init__(
        self,
        context: Any | None = None,
        agent: Any | None = None,
        start_time: float | None = None,
    ) -> None:
        self._context = context
        self._agent = agent
        # 优先使用显式传入的 start_time, 否则用 agent 的 _start_time, 最后回退到模块启动时间
        self._start_time = (
            start_time
            if start_time is not None
            else getattr(agent, "_start_time", None)
            if agent is not None
            else _MODULE_START_TIME
        )

    # ─── 核心方法: 收集状态 ───

    def get_current_state(self) -> AgentState:
        """收集当前 Agent 内心状态

        每个字段独立 try/except, 单个来源失败不影响其他字段。
        """
        state = AgentState()

        # 认知负载 + 置信度 + 元认知阶段 (来自 metacognition_lite)
        self._collect_metacog(state)

        # 活跃目标 (来自 context)
        self._collect_active_goals(state)

        # 情绪状态 (来自 emotion 模块或 context.emotion_hint)
        self._collect_emotional_state(state)

        # 反思次数 + 活跃教训数 (来自 agent_r_reflection)
        self._collect_reflection_stats(state)

        # 降级级别 (来自 degradation_strategy)
        self._collect_degradation_level(state)

        # 健康度评分 (来自 behavioral_health)
        self._collect_health_score(state)

        # 运行时间
        try:
            state.uptime = max(0.0, time.time() - self._start_time)
        except Exception:
            state.uptime = 0.0

        # 最后一次错误 (来自 agent._error_handler 或 self_diagnostic)
        self._collect_last_error(state)

        # J-Space Hook: emit cognitive_load signal (non-blocking)
        if _signal_stream is not None:
            try:
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    pass
                else:
                    _fire_and_forget(_signal_stream.emit(
                        "cognitive_load", state.cognitive_load, "introspection"))
            except Exception:
                pass

        return state

    # ─── 各来源采集器 (独立失败隔离) ───

    def _collect_metacog(self, state: AgentState) -> None:
        """从 metacognition_lite 采集认知负载/置信度/阶段"""
        try:
            # duck typing: 查找带 get_state_dict 方法的对象
            mc = self._find_by_method("get_state_dict")
            if mc is None:
                return
            d = mc.get_state_dict()
            state.confidence = float(d.get("confidence", state.confidence))
            state.metacog_phase = str(d.get("phase", state.metacog_phase))
            # 认知负载近似: uncertainty + drift_score 加权 (0-1)
            uncertainty = float(d.get("uncertainty", 0.0))
            drift_score = float(d.get("drift_score", 0.0))
            state.cognitive_load = max(0.0, min(1.0, 0.5 * uncertainty + 0.5 * drift_score))
        except Exception as e:
            logger.debug(f"Introspect.metacog_failed: {e!r}")

    def _collect_active_goals(self, state: AgentState) -> None:
        """从 context 采集活跃目标"""
        try:
            if self._context is None:
                return
            goals: list[str] = []
            # 优先使用 context.active_goals (若存在)
            ctx_goals = getattr(self._context, "active_goals", None)
            if isinstance(ctx_goals, (list, tuple)):
                goals = [str(g) for g in ctx_goals if g]
            # 回退: 最近一条 user 消息作为隐式目标
            if not goals:
                history = getattr(self._context, "history", None) or []
                for msg in reversed(history):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        content = str(msg.get("content", "")).strip()
                        if content:
                            goals.append(content[:80])
                        break
            state.active_goals = goals[:5]  # 最多保留 5 条
        except Exception as e:
            logger.debug(f"Introspect.goals_failed: {e!r}")

    def _collect_emotional_state(self, state: AgentState) -> None:
        """从 emotion 模块或 context.emotion_hint 采集情绪"""
        try:
            # 优先用 context.emotion_hint
            if self._context is not None:
                hint = getattr(self._context, "emotion_hint", "")
                if hint:
                    state.emotional_state = str(hint)[:60]
                    return
            # 回退: 对最近一条 user 消息做情绪检测
            if self._context is not None:
                history = getattr(self._context, "history", None) or []
                last_input = ""
                for msg in reversed(history):
                    if isinstance(msg, dict) and msg.get("role") == "user":
                        last_input = str(msg.get("content", ""))
                        break
                if last_input:
                    from emotion.emotion_simple import detect_emotion
                    emo = detect_emotion(last_input)
                    state.emotional_state = str(emo.get("primary", "平静"))
        except Exception as e:
            logger.debug(f"Introspect.emotion_failed: {e!r}")

    def _collect_reflection_stats(self, state: AgentState) -> None:
        """从 agent_r_reflection 采集反思次数/活跃教训数"""
        try:
            # duck typing: 查找带 get_stats 方法的对象
            r = self._find_by_method("get_stats")
            if r is None:
                return
            stats = r.get_stats()
            state.reflection_count = int(stats.get("reflection_count", 0))
            state.lessons_active = int(stats.get("memories", 0))
        except Exception as e:
            logger.debug(f"Introspect.reflection_failed: {e!r}")

    def _collect_degradation_level(self, state: AgentState) -> None:
        """从 degradation_strategy 采集降级级别"""
        try:
            from core.degradation_strategy import get_degradation_strategy
            strat = get_degradation_strategy()
            # current_level 是 IntEnum, int() 得到 0-3
            state.degradation_level = int(strat.current_level)
        except Exception as e:
            logger.debug(f"Introspect.degradation_failed: {e!r}")

    def _collect_health_score(self, state: AgentState) -> None:
        """从 behavioral_health 采集健康度评分"""
        # 优先使用 core/behavioral_health.py 的 BehavioralHealthScorer (5 级)
        try:
            from core.behavioral_health import get_behavioral_health_scorer
            scorer = get_behavioral_health_scorer()
            score = scorer.calculate_from_runtime()
            state.health_score = float(score.score)
            return
        except Exception as e:
            logger.debug(f"Introspect.health_scorer_failed: {e!r}")
        # 回退: doctor/behavioral_health.py 的 BehavioralHealthMonitor
        try:
            from doctor.behavioral_health import get_behavioral_health_monitor
            monitor = get_behavioral_health_monitor()
            report = monitor.get_health_report()
            bhs = float(report.get("behavioral_health_score", 1.0))
            # 0-1 映射到 1-5
            state.health_score = max(1.0, min(5.0, round(bhs * 5)))
        except Exception as e:
            logger.debug(f"Introspect.health_monitor_failed: {e!r}")

    def _collect_last_error(self, state: AgentState) -> None:
        """从 agent._error_handler 或 self_diagnostic 采集最后一次错误"""
        # 优先: SmartErrorHandler 的最近错误队列
        try:
            if self._agent is not None:
                err_handler = getattr(self._agent, "_error_handler", None)
                if err_handler is not None:
                    recent = getattr(err_handler, "_recent_errors", None)
                    if recent:
                        last = recent[-1]
                        msg = getattr(last, "error_message", "") or str(last)
                        state.last_error = str(msg)[:200]
                        return
        except Exception as e:
            logger.debug(f"Introspect.last_error_handler_failed: {e!r}")
        # 回退: self_diagnostic 的最近 critical/warning 报告
        try:
            from core.self_diagnostic import get_self_diagnostic
            diag = get_self_diagnostic()
            reports = diag.get_recent_reports(limit=1)
            if reports:
                state.last_error = str(reports[0].message)[:200]
        except Exception as e:
            logger.debug(f"Introspect.self_diag_failed: {e!r}")

    # ─── 工具: 在 agent 及其属性中查找带指定方法的对象 (duck typing) ───

    def _find_by_method(self, method_name: str) -> Any | None:
        """在 agent 及其常见属性中查找带指定方法的对象

        duck typing 方式: 不要求具体类型, 只要有指定方法即可。
        这样可兼容用户自定义的实现, 也方便测试注入 fake 对象。

        查找顺序:
        1. agent 本身 (若 hasattr(agent, method_name))
        2. agent.<常见属性名>
        """
        if self._agent is None:
            return None
        # agent 本身
        if callable(getattr(self._agent, method_name, None)):
            return self._agent
        # 常见属性名 (覆盖 metacognition / reflector / 各种带前缀变体)
        candidates = (
            "metacognition", "metacog", "_metacog",
            "reflector", "agent_r", "_reflector",
        )
        for name in candidates:
            obj = getattr(self._agent, name, None)
            if callable(getattr(obj, method_name, None)):
                return obj
        return None

    # ─── 格式化输出 ───

    @staticmethod
    def to_text(state: AgentState) -> str:
        """格式化为可读文本 (供斜杠命令 /self 使用)"""
        # 运行时间格式化
        uptime = state.uptime
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        seconds = int(uptime % 60)
        uptime_str = f"{hours}h{minutes}m{seconds}s"

        # 降级级别名称
        level_names = {0: "正常", 1: "轻度降级", 2: "最小化", 3: "紧急"}
        level_str = level_names.get(state.degradation_level, "未知")

        # 健康度级别名称
        def _health_label(s: float) -> str:
            if s >= 5:
                return "优秀"
            if s >= 4:
                return "良好"
            if s >= 3:
                return "一般"
            if s >= 2:
                return "较差"
            return "危急"

        # 认知负载条
        load_bar = _progress_bar(state.cognitive_load, 10)
        conf_bar = _progress_bar(state.confidence, 10)

        lines = [
            "🧠 Agent 内心状态",
            "",
            f"⏰ 运行时间: {uptime_str}",
            f"🎯 元认知阶段: {state.metacog_phase}",
            f"📊 认知负载: {state.cognitive_load:.2f} {load_bar}",
            f"✨ 置信度:   {state.confidence:.2f} {conf_bar}",
            f"💫 情绪状态: {state.emotional_state}",
            f"🔻 降级级别: L{state.degradation_level} ({level_str})",
            f"💚 健康度:   {state.health_score:.1f}/5 ({_health_label(state.health_score)})",
            f"🔄 反思次数: {state.reflection_count}",
            f"📚 活跃教训: {state.lessons_active}",
        ]

        if state.active_goals:
            lines.append("")
            lines.append("🎯 活跃目标:")
            for i, g in enumerate(state.active_goals, 1):
                lines.append(f"  {i}. {g}")

        if state.last_error:
            lines.append("")
            lines.append(f"⚠️ 最近错误: {state.last_error}")

        return "\n".join(lines)

    @staticmethod
    def to_dict(state: AgentState) -> dict:
        """格式化为 JSON 可序列化字典 (供 /health/self 接口使用)"""
        return asdict(state)


def _progress_bar(value: float, width: int = 10) -> str:
    """生成简单的 ASCII 进度条 (0-1)"""
    v = max(0.0, min(1.0, value))
    filled = int(v * width)
    return "[" + "█" * filled + "░" * (width - filled) + "]"


# ============================================================
# 全局单例 (懒加载, 不依赖任何外部资源)
# ============================================================

_introspector: AgentIntrospector | None = None


def get_introspector() -> AgentIntrospector:
    """获取全局 AgentIntrospector 单例 (无依赖, 默认状态)"""
    global _introspector
    if _introspector is None:
        _introspector = AgentIntrospector()
    return _introspector


def set_introspector(introspector: AgentIntrospector) -> None:
    """注入带依赖的 introspector (供 bootstrap 调用)"""
    global _introspector
    _introspector = introspector