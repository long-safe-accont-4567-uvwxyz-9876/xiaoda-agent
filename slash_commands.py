from typing import Any, Optional
import asyncio
import os
import shutil
import time
from loguru import logger


OWNER_ONLY_COMMANDS: set[str] = set()  # 所有命令均不设权限限制

COMMAND_DESCRIPTIONS = {
    "/cost": "查看 API 消费成本（可加 7d）",
    "/status": "查看运行时状态",
    "/model": "切换模型",
    "/forget": "删除指定记忆",
    "/reset": "系统重置",
    "/learn": "学习管理",
    "/note": "笔记管理",
    "/help": "命令帮助",
    "/voice": "语音回复开关",
    "/agent": "切换子代理",
    "/hw": "硬件状态",
    "/sys": "系统命令",
    "/cam": "摄像头控制",
    "/memory": "记忆统计",
    "/emotion": "情绪检测",
    "/knowledge": "知识图谱查询",
    "/debug": "调试信息",
    "/doctor": "自检 (零 API 调用, <2s)",
    "/self": "查看 Agent 内心状态 (元认知自省)",
}


def list_commands() -> list[dict]:
    """供 Web UI 斜杠命令自动补全使用。"""
    return [
        {"name": name, "description": desc, "owner_only": name in OWNER_ONLY_COMMANDS}
        for name, desc in COMMAND_DESCRIPTIONS.items()
    ]


class SlashCommandHandler:

    def __init__(self, db: Optional[Any]=None, router: Optional[Any]=None, context: Optional[Any]=None,
                 memory: Optional[Any]=None, learning_manager: Optional[Any]=None,
                 notebook_manager: Optional[Any]=None, security: Optional[Any]=None, agent: Optional[Any]=None) -> None:
        self._db = db
        self._router = router
        self._context = context
        self._memory = memory
        self._learning = learning_manager
        self._notebook = notebook_manager
        self._security = security
        self._agent = agent
        self._start_time = time.time()

    def is_slash_command(self, text: str) -> bool:
        stripped = text.strip()
        if not stripped.startswith("/"):
            return False
        if stripped.startswith("//"):
            return False
        return True

    def is_owner_command(self, command: str) -> bool:
        return command.split()[0] in OWNER_ONLY_COMMANDS

    def _is_owner(self, user_id: str) -> bool:
        return self._security and self._security.is_owner(user_id)

    async def handle(self, text: str, user_id: str = "") -> str | None:
        parts = text.strip().split(maxsplit=1)
        command = parts[0].lower()
        args = parts[1].strip() if len(parts) > 1 else ""

        if self.is_owner_command(command):
            if not self._security or not self._security.is_owner(user_id):
                return "这个命令只有主人才能用哦～"

        handlers = {
            "/cost": self._cmd_cost,
            "/status": self._cmd_status,
            "/model": self._cmd_model,
            "/forget": self._cmd_forget,
            "/reset": self._cmd_reset,
            "/learn": self._cmd_learn,
            "/note": self._cmd_note,
            "/help": self._cmd_help,
            "/voice": self._cmd_voice,
            "/agent": self._cmd_agent,
            "/hw": self._cmd_hw,
            "/sys": self._cmd_sys,
            "/cam": self._cmd_cam,
            "/memory": self._cmd_memory,
            "/emotion": self._cmd_emotion,
            "/knowledge": self._cmd_knowledge,
            "/debug": self._cmd_debug,
            "/doctor": self._cmd_doctor,
            "/self": self._cmd_self,
        }

        handler = handlers.get(command)
        if handler:
            try:
                return await handler(args, user_id)
            except Exception as e:
                logger.warning("slash.handle_error", command=command, error=str(e))
                if self._agent and hasattr(self._agent, '_error_handler') and self._agent._error_handler:
                    try:
                        smart_reply = await self._agent._error_handler.handle_error_with_intelligence(
                            error=e, user_query=text, context=f"执行命令 /{command} 参数: {args}"
                        )
                        return smart_reply
                    except Exception:
                        pass
                return f"执行 /{command} 时出了点问题：{str(e)[:100]}"

        return await self._cmd_help("", user_id)

    async def _cmd_cost(self, args: str, user_id: str) -> str:
        if not self._db:
            return "数据库还没准备好呢～"

        daily = await self._db.analytics.get_daily_cost()
        lines = ["📊 今日 API 消耗"]

        if daily["call_count"] == 0:
            return "今天还没有 API 调用哦～"

        cost_cny = daily["total_cost_usd"] * 7.2
        lines.append(f"💰 花费: ${daily['total_cost_usd']:.4f} (≈¥{cost_cny:.2f})")
        lines.append(f"📞 调用次数: {daily['call_count']}")
        lines.append(f"📥 输入: {daily['total_prompt_tokens']:,} tokens")
        lines.append(f"📤 输出: {daily['total_completion_tokens']:,} tokens")

        if daily["cache_hit_ratio"] > 0:
            lines.append(f"🎯 缓存命中率: {daily['cache_hit_ratio']:.1%}")

        if args in ("7d", "week"):
            breakdown = await self._db.analytics.get_cost_breakdown(days=7)
            if breakdown:
                lines.append("\n📋 近7天按类型:")
                for b in breakdown[:5]:
                    lines.append(f"  {b['task_type']}: ${b['total_cost']:.4f} ({b['call_count']}次)")

        return "\n".join(lines)

    async def _cmd_status(self, args: str, user_id: str) -> str:
        lines = ["🌿 纳西妲状态报告"]

        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        lines.append(f"⏰ 运行时间: {hours}h{minutes}m")

        if self._router:
            stats = self._router.get_cache_stats()
            label = self._router.get_model_preference_label()
            lines.append(f"🤖 模型: {label}")
            lines.append(f"📞 API调用: {stats['total_calls']}次")
            if stats["hit_tokens"] + stats["miss_tokens"] > 0:
                lines.append(f"🎯 缓存命中率: {stats['hit_ratio']:.1%}")

        if self._db:
            mem_count = await self._db.memory.get_episodic_count()
            lines.append(f"🧠 记忆条数: {mem_count}")

            daily = await self._db.analytics.get_daily_cost()
            if daily["call_count"] > 0:
                cost_cny = daily["total_cost_usd"] * 7.2
                lines.append(f"💰 今日花费: ${daily['total_cost_usd']:.4f} (≈¥{cost_cny:.2f})")

        if self._context:
            lines.append(f"💬 对话轮数: {len(self._context.history) // 2}")

        if self._learning:
            additions = await self._learning.get_system_prompt_additions()
            if additions:
                count = additions.count("·")
                lines.append(f"📚 学习规则: {count}条")

        return "\n".join(lines)

    async def _cmd_model(self, args: str, user_id: str) -> str:
        if not self._router:
            return "路由器还没准备好呢～"

        # MiMo 预设
        if args in ("mimo",):
            self._router.set_model_preference("mimo")
            if self._agent and hasattr(self._agent, 'klee'):
                self._agent.klee.set_preferred_provider("mimo")
            return "已切换到 MiMo 模式 🍊（使用小米 MiMo-V2.5）"
        elif args in ("mimo-pro", "pro", "mimo_pro"):
            self._router.set_model_preference("mimo-pro")
            if self._agent and hasattr(self._agent, 'klee'):
                self._agent.klee.set_preferred_provider("mimo")
            return "已切换到 MiMo Pro 模式 🧠（使用小米 MiMo-V2.5-Pro 深度思考）"
        elif args in ("mimo-flash", "flash", "mimo_flash"):
            self._router.set_model_preference("mimo-flash")
            return "已切换到 MiMo Flash 模式 ⚡（使用小米 MiMo-V2.5 快速响应）"
        elif args in ("mimo-mini", "mini", "mimo_mini"):
            self._router.set_model_preference("mimo-mini")
            return "已切换到 MiMo Mini 模式 🐣（使用小米 MiMo-V2.5 轻量任务）"
        # 第三方模型 provider/model_id 格式
        elif "/" in args:
            parts = args.split("/", 1)
            provider = parts[0]
            model_id = parts[1]
            ok = self._router.set_model_preference(args)
            if ok is False:
                return f"切换失败：不支持 {provider}/{model_id}"
            return f"已切换到 {model_id}（{provider}）"
        # 无参数：显示当前模型和可用第三方
        else:
            pref = self._router.get_model_preference()
            label = self._router.get_model_preference_label()
            lines = [f"当前: {label}"]
            lines.append("预设: /model [mimo|mimo-pro|mimo-flash|mimo-mini]")
            third_party = []
            if os.environ.get("SILICONFLOW_API_KEY", ""):
                third_party.append("siliconflow")
            if os.environ.get("OPENROUTER_API_KEY", ""):
                third_party.append("openrouter")
            if third_party:
                lines.append("第三方模型:")
                # 尝试从模型发现缓存中读取具体模型名
                cache_available = False
                try:
                    from web.routers.model_discovery import _cache as discovery_cache
                    cache_data = discovery_cache.get("data")
                    if cache_data:
                        for pg in cache_data:
                            provider = pg.get("provider", "")
                            models = pg.get("models", [])
                            if provider in third_party and models:
                                cache_available = True
                                model_names = [m["display_name"] for m in models[:6]]
                                suffix = "..." if len(models) > 6 else ""
                                lines.append(f"  {provider}: {', '.join(model_names)}{suffix}")
                except Exception:
                    pass
                if not cache_available:
                    for tp in third_party:
                        lines.append(f"  · {tp}: 已配置（使用 /model {tp}/模型名 切换）")
            return "\n".join(lines)

    async def _cmd_forget(self, args: str, user_id: str) -> str:
        if not self._context:
            return "上下文还没准备好呢～"

        cleared = len(self._context.history)
        self._context.history.clear()
        self._context.memory_retrieval = None
        self._context.emotion_hint = ""

        return f"已清除 {cleared} 条短期对话记忆～\n（情景记忆和画像还在哦，那些是人家珍贵的回忆）"

    async def _cmd_reset(self, args: str, user_id: str) -> str:
        if not self._context:
            return "上下文还没准备好呢～"

        self._context.clear()
        self._context.invalidate_dynamic_cache()

        return "对话上下文已重置！人家会从头开始认识你的～"

    async def _cmd_learn(self, args: str, user_id: str) -> str:
        if not self._db:
            return "数据库还没准备好呢～"

        promoted = await self._db.learning.get_promoted_learnings()
        all_learnings = await self._db.learning.search_learnings(limit=10)

        lines = []
        if promoted:
            lines.append("📚 已学习的经验:")
            for i, l in enumerate(promoted[:5], 1):
                summary = l.get("summary", "")[:60]
                count = l.get("recurrence_count", 1)
                lines.append(f"{i}. {summary} (×{count})")

        if all_learnings and len(all_learnings) > len(promoted):
            pending = [l for l in all_learnings if l.get("status") == "pending"]
            if pending:
                lines.append(f"\n📝 待确认的学习 ({len(pending)} 条):")
                for i, l in enumerate(pending[:5], 1):
                    summary = l.get("summary", "")[:60]
                    lines.append(f"{i}. {summary}")

        if not lines:
            return "人家还没有学到什么特别的经验呢～"

        return "\n".join(lines)

    async def _cmd_note(self, args: str, user_id: str) -> str:
        if not self._db:
            return "数据库还没准备好呢～"

        notes = await self._db.notebook.get_notebook_notes(limit=10)
        tasks = await self._db.notebook.get_pending_tasks(limit=5)

        lines = []
        if notes:
            lines.append("📓 笔记:")
            for i, n in enumerate(notes[:10], 1):
                kind = n.get("kind", "note")
                content = n.get("content", "")[:50]
                icon = "📌" if kind == "task" else "📝"
                lines.append(f"{i}. {icon} {content}")

        if tasks:
            lines.append(f"\n⏰ 待办 ({len(tasks)} 项):")
            for i, t in enumerate(tasks[:5], 1):
                content = t.get("content", "")[:40]
                due = t.get("due_date", 0)
                if due and due > 0:
                    import datetime
                    ds = datetime.datetime.fromtimestamp(due).strftime("%m/%d %H:%M")
                    lines.append(f"{i}. {content} @ {ds}")
                else:
                    lines.append(f"{i}. {content}")

        if not lines:
            return "笔记本还是空的呢～"

        return "\n".join(lines)

    async def _cmd_voice(self, args: str, user_id: str) -> str:
        if not self._is_owner(user_id):
            return "只有主人才能切换语音模式哦～"
        if not self._agent:
            return "Agent 还没准备好呢～"
        if args in ("on", "开", "1", "true"):
            self._agent.set_voice_mode(True)
            return "语音模式已开启 🎤（回复将附带语音）"
        elif args in ("off", "关", "0", "false"):
            self._agent.set_voice_mode(False)
            return "语音模式已关闭 🔇（仅文字回复）"
        else:
            mode = self._agent.get_voice_mode()
            status = "开启 🎤" if mode else "关闭 🔇"
            return f"语音模式: {status}\n用法: /voice [on|off]"

    async def _cmd_agent(self, args: str, user_id: str) -> str:
        if not self._agent:
            return "Agent 还没准备好呢～"

        agents = self._agent.dispatcher.list_agents()

        if not args:
            target = await self._agent.get_chat_target(user_id)
            target_display = "纳西妲" if target == "nahida" else target
            lines = [f"当前对话目标: {target_display}"]
            if agents:
                lines.append("可用子Agent:")
                for a in agents:
                    lines.append(f"  · {a['display_name']}（/agent {a['display_name']}）")
            lines.append("  · 纳西妲（/agent 纳西妲）")
            return "\n".join(lines)

        if args in ("纳西妲", "nahida"):
            await self._agent.set_chat_target(user_id, "nahida")
            return "已切换到纳西妲 🌿"

        for a in agents:
            if args in (a["display_name"], a["name"]):
                await self._agent.set_chat_target(user_id, a["name"])
                return f"已切换到{a['display_name']} 🔥"

        return f"没找到叫「{args}」的Agent哦～\n用法: /agent [名称]"

    async def _cmd_hw(self, args: str, user_id: str) -> str:
        if not self._db:
            return "数据库还没准备好呢～"
        lines = ["🖥️ 香橙派硬件状态"]
        try:
            import os
            temp_path = "/sys/class/thermal/thermal_zone0/temp"
            try:
                with open(temp_path) as f:
                    temp_c = int(f.read().strip()) / 1000
                temp_icon = "🌡️"
                if temp_c > 80:
                    temp_icon = "🔥⚠️"
                elif temp_c > 60:
                    temp_icon = "🌡️"
                lines.append(f"{temp_icon} CPU温度: {temp_c:.1f}°C")
            except Exception as e:
                logger.debug("slash.hw.temp_read_failed", error=str(e))
                lines.append("🌡️ CPU温度: 无法读取")
            try:
                freq_path = "/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq"
                with open(freq_path) as f:
                    freq_khz = int(f.read().strip())
                lines.append(f"⚡ CPU频率: {freq_khz // 1000} MHz")
            except Exception as e:
                logger.debug("slash.hw.freq_read_failed", error=str(e))
                lines.append("⚡ CPU频率: 无法读取")
            try:
                with open("/proc/meminfo") as f:
                    meminfo = f.read()
                mem_total = int([l for l in meminfo.split('\n') if 'MemTotal' in l][0].split()[1])
                mem_avail = int([l for l in meminfo.split('\n') if 'MemAvailable' in l][0].split()[1])
                mem_used = mem_total - mem_avail
                mem_pct = mem_used / mem_total * 100
                mem_icon = "💾"
                if mem_pct > 90:
                    mem_icon = "💾⚠️"
                lines.append(f"{mem_icon} 内存: {mem_used//1024}M / {mem_total//1024}M ({mem_pct:.0f}%)")
            except Exception as e:
                logger.debug("slash.hw.mem_read_failed", error=str(e))
                lines.append("💾 内存: 无法读取")
            try:
                usage = shutil.disk_usage('/')
                total = usage.total
                free = usage.free
                used = usage.used
                pct = used / total * 100 if total > 0 else 0
                lines.append(f"💿 磁盘: {used//1073741824}G / {total//1073741824}G ({pct:.0f}%)")
            except Exception as e:
                logger.debug("slash.hw.disk_read_failed", error=str(e))
                lines.append("💿 磁盘: 无法读取")
            try:
                with open("/proc/loadavg") as f:
                    load = f.read().strip().split()[:3]
                lines.append(f"📊 负载: {' '.join(load)}")
            except Exception as e:
                logger.debug("slash.hw.load_read_failed", error=str(e))
                lines.append("📊 负载: 无法读取")
        except Exception:
            pass
        return "\n".join(lines)

    async def _cmd_sys(self, args: str, user_id: str) -> str:
        lines = ["📋 系统运行状态"]
        uptime = time.time() - self._start_time
        hours = int(uptime // 3600)
        minutes = int((uptime % 3600) // 60)
        lines.append(f"⏰ Agent运行: {hours}h{minutes}m")
        if self._agent and hasattr(self._agent, '_error_handler') and self._agent._error_handler:
            recent = self._agent._error_handler._recent_errors
            if recent:
                last = recent[-1]
                lines.append(f"⚠️ 最近错误: {last.error_type} - {last.error_message[:60]}")
            else:
                lines.append("✅ 最近无错误")
        else:
            lines.append("✅ 错误监控: 未启用")
        try:
            result = await asyncio.create_subprocess_exec(
                "systemctl", "is-active", "qq-agent",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            stdout_bytes, _ = await asyncio.wait_for(result.communicate(), timeout=5)
            status = stdout_bytes.decode().strip() or "未知"
            status_icon = "🟢" if status == "active" else "🔴"
            lines.append(f"{status_icon} qq-agent: {status}")
        except Exception:
            lines.append("🔘 qq-agent: 状态未知")
        if self._router:
            label = self._router.get_model_preference_label()
            from model_router import ROUTE_TABLE
            model_id = ROUTE_TABLE.get("chat", {}).get("model", "unknown")
            lines.append(f"🤖 当前模型: {label} ({model_id})")
        return "\n".join(lines)

    async def _cmd_cam(self, args: str, user_id: str) -> str:
        if not self._is_owner(user_id):
            return "只有主人才能使用摄像头哦~"
        try:
            from utils.vision_service import VisionService
            vs = VisionService()
            ok, frame = vs.capture_frame(device=0)
            if not ok:
                return f"📷 摄像头不可用: {frame}"
            if args.strip() == "snap":
                path = vs.save_frame(frame)
                h, w = frame.shape[:2]
                return f"📸 已拍照保存\n分辨率: {w}x{h}\n路径: {path}"
            description = vs.describe_scene(frame)
            colors = vs.analyze_colors(frame)
            color_str = ", ".join([f"{c.color}({c.percentage:.0f}%)" for c in colors[:3]])
            path = vs.save_frame(frame)
            return f"📷 摄像头画面分析\n{description}\n主色调: {color_str}\n图片已保存: {path}"
        except Exception as e:
            return f"📷 摄像头操作失败: {str(e)[:100]}"

    async def _cmd_memory(self, args: str, user_id: str) -> str:
        lines = ["🧠 记忆统计"]
        if self._db:
            try:
                count = await self._db.memory.get_episodic_count()
                lines.append(f"📋 情景记忆条数: {count}")
            except Exception as e:
                lines.append(f"📋 情景记忆: 读取失败 ({str(e)[:50]})")
        else:
            lines.append("📋 数据库未就绪")
        if self._memory:
            try:
                last_encode = getattr(self._memory, '_last_encode_time', 0)
                if last_encode > 0:
                    import datetime
                    dt = datetime.datetime.fromtimestamp(last_encode).strftime("%m/%d %H:%M")
                    elapsed = time.time() - last_encode
                    lines.append(f"⏰ 上次编码: {dt}（{int(elapsed // 60)}分钟前）")
                else:
                    lines.append("⏰ 上次编码: 尚未编码")
            except Exception:
                lines.append("⏰ 上次编码: 未知")
        else:
            lines.append("⏰ 记忆管理器未就绪")
        return "\n".join(lines)

    async def _cmd_emotion(self, args: str, user_id: str) -> str:
        lines = ["💫 当前情绪状态"]
        if self._context:
            hint = getattr(self._context, 'emotion_hint', '')
            if hint:
                lines.append(f"🎭 情绪提示: {hint}")
            else:
                lines.append("🎭 情绪提示: 平静")
        else:
            lines.append("🎭 上下文未就绪")
        if self._agent:
            try:
                from emotion.emotion_simple import detect_emotion
                last_input = ""
                if self._context and hasattr(self._context, 'history') and self._context.history:
                    for msg in reversed(self._context.history):
                        if msg.get("role") == "user":
                            last_input = msg.get("content", "")
                            break
                if last_input:
                    emotion = detect_emotion(last_input)
                    primary = emotion.get("primary", "平静")
                    intensity = emotion.get("intensity", 0)
                    lines.append(f"😊 感知情绪: {primary}")
                    lines.append(f"📊 情绪强度: {intensity:.1f}")
                else:
                    lines.append("😊 感知情绪: 暂无对话")
            except Exception:
                lines.append("😊 情绪检测: 不可用")
        return "\n".join(lines)

    async def _cmd_knowledge(self, args: str, user_id: str) -> str:
        lines = ["🕸️ 知识图谱统计"]
        if self._db:
            try:
                entity_count = await self._db.knowledge.get_entity_count()
                lines.append(f"📌 实体数量: {entity_count}")
            except Exception as e:
                lines.append(f"📌 实体数量: 读取失败 ({str(e)[:50]})")
            try:
                relations = await self._db.knowledge.get_all_relations()
                lines.append(f"🔗 关系数量: {len(relations)}")
                if relations:
                    recent = relations[:3]
                    for r in recent:
                        fr = r.get("from_entity", "?")
                        rel = r.get("relation_type", "?")
                        to = r.get("to_entity", "?")
                        lines.append(f"  · {fr} —[{rel}]→ {to}")
            except Exception as e:
                lines.append(f"🔗 关系数量: 读取失败 ({str(e)[:50]})")
        else:
            lines.append("数据库未就绪")
        return "\n".join(lines)

    async def _cmd_debug(self, args: str, user_id: str) -> str:
        lines = ["🔧 内部状态（调试）"]
        # Metrics snapshot
        try:
            from utils.metrics import metrics
            snapshot = metrics.get_snapshot()
            counters = snapshot.get("counters", {})
            gauges = snapshot.get("gauges", {})
            if counters:
                lines.append("📊 计数器:")
                for k, v in list(counters.items())[:10]:
                    lines.append(f"  · {k}: {v}")
            if gauges:
                lines.append("📈 仪表:")
                for k, v in list(gauges.items())[:10]:
                    lines.append(f"  · {k}: {v:.3f}")
            timer_keys = [k for k in snapshot if k.startswith("timer.")]
            if timer_keys:
                lines.append("⏱️ 计时器:")
                for k in timer_keys[:5]:
                    info = snapshot[k]
                    lines.append(f"  · {k[6:]}: avg={info['avg']}s p95={info['p95']}s n={info['samples']}")
        except Exception as e:
            lines.append(f"📊 指标: 读取失败 ({str(e)[:50]})")
        # Router state
        if self._router:
            pref = self._router.get_model_preference()
            label = self._router.get_model_preference_label()
            stats = self._router.get_cache_stats()
            lines.append(f"🤖 路由: {label} (pref={pref})")
            lines.append(f"   调用: {stats['total_calls']}次")
        # Context state
        if self._context:
            hist_len = len(self._context.history) if hasattr(self._context, 'history') else 0
            lines.append(f"💬 上下文历史: {hist_len}条")
        return "\n".join(lines)

    async def _cmd_doctor(self, args: str, user_id: str) -> str:
        """Doctor 自检 — 零 API 调用, <2s 完成

        用法:
            /doctor          运行自检, 文本格式输出
            /doctor json     JSON 格式输出
            /doctor fix       自动修复可修复的问题
        """
        import asyncio
        from core.doctor import _create_default_doctor

        doc = _create_default_doctor()
        auto_fix = args.strip().lower() in ("fix", "--fix", "repair")
        json_out = args.strip().lower() in ("json", "--json")

        # doctor.run() 是同步的, 用 to_thread 避免阻塞事件循环
        report = await asyncio.to_thread(doc.run, auto_fix=auto_fix)

        if json_out:
            import json
            return f"```json\n{json.dumps(report, indent=2, ensure_ascii=False)}\n```"

        return doc.format_text(report)

    async def _cmd_self(self, args: str, user_id: str) -> str:
        """Agent 状态自省 — 查看当前内心状态

        用法:
            /self          文本格式输出
            /self json     JSON 格式输出
        """
        from core.agent_introspection import AgentIntrospector

        json_out = args.strip().lower() in ("json", "--json")
        introspector = AgentIntrospector(context=self._context, agent=self._agent)
        state = introspector.get_current_state()

        if json_out:
            import json
            return f"```json\n{json.dumps(introspector.to_dict(state), indent=2, ensure_ascii=False)}\n```"

        return introspector.to_text(state)

    async def _cmd_help(self, args: str, user_id: str) -> str:
        is_owner = self._security and self._security.is_owner(user_id)

        lines = ["🌿 纳西妲的命令列表\n"]

        public_cmds = [
            ("/cost [7d]", "查看API消耗（加7d看7天）"),
            ("/status", "查看Agent状态"),
            ("/forget", "清除短期对话记忆"),
            ("/learn", "查看学习记录"),
            ("/note", "查看笔记本"),
            ("/hw", "查看香橙派硬件状态"),
            ("/cam", "拍照并分析摄像头画面（/cam snap仅拍照）"),
            ("/sys", "查看系统运行状态"),
            ("/memory", "查看记忆统计"),
            ("/emotion", "查看当前情绪状态"),
            ("/knowledge", "查看知识图谱统计"),
            ("/doctor [json|fix]", "运行自检（零 API 调用, <2s）"),
            ("/self [json]", "查看 Agent 内心状态（元认知自省）"),
            ("/help", "显示此帮助"),
        ]

        owner_cmds = [
            ("/model [mimo|mimo-pro|mimo-flash|mimo-mini]", "切换模型模式"),
            ("/reset", "重置对话上下文"),
            ("/voice [on|off]", "切换语音模式"),
            ("/agent [名称]", "切换对话目标Agent"),
            ("/debug", "查看内部调试状态"),
        ]

        for cmd, desc in public_cmds:
            lines.append(f"  {cmd} — {desc}")

        if is_owner:
            lines.append("\n👑 主人专属:")
            for cmd, desc in owner_cmds:
                lines.append(f"  {cmd} — {desc}")

        return "\n".join(lines)
