from typing import Any
import os
import sys
import time
import random
import asyncio
import subprocess
from loguru import logger

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from utils.logging_config import setup_logging
setup_logging()
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{extra[trace_id]}</cyan> | {message}",
    level="WARNING",
)

from agent_core import AgentCore
from agent_core.user_cli import CLIUser
from core.event_bus import event_bus
from model_router import ROUTE_TABLE
import contextlib

# ── readline 支持 ──────────────────────────────────────────
try:
    import readline
    _HIST_FILE = os.path.expanduser("~/.ai-agent/cli_history")
    _HIST_SIZE = 500
    with contextlib.suppress(FileNotFoundError):
        readline.read_history_file(_HIST_FILE)
    readline.set_history_length(_HIST_SIZE)
    import atexit
    atexit.register(lambda: readline.write_history_file(_HIST_FILE))
except ImportError:
    pass  # readline 不可用时静默降级


# ── 斜杠命令 TAB 补全（openclaw 风格：两级补全） ──────────────
# 第一级：命令名补全（输入 / 后按 Tab 列出候选命令）
# 第二级：参数补全（输入 /cmd 加空格后，按 Tab 补全该命令的合法参数，
#         参数候选来自 slash_commands.COMMAND_META 的 arg_completions）
def _all_command_names() -> list[str]:
    names = ["/help", "/exit", "/quit"]
    try:
        from slash_commands import COMMAND_DESCRIPTIONS
        names.extend(COMMAND_DESCRIPTIONS.keys())
    except ImportError:
        pass
    return sorted(set(names))


def _argument_completions(command: str, partial: str) -> list[str]:
    """返回命令的参数级补全候选（第二级补全）。

    /model 参数为动态（provider/模型），从模型发现缓存实时补全，对齐 WebUI 模型选择 button。
    """
    if command == "/model":
        return _model_arg_completions(partial)
    try:
        from slash_commands import get_argument_completions
        return get_argument_completions(command, partial)
    except ImportError:
        return []


def _model_arg_completions(partial: str) -> list[str]:
    """/model 参数补全：动态枚举已发现模型（provider/模型）。"""
    try:
        from model_router import list_discovered_model_ids
        opts = list_discovered_model_ids()
    except Exception:
        opts = []
    return [o for o in opts if o.startswith(partial)]


try:
    import readline as _rl
    _ALL_CMDS = _all_command_names()

    def _cli_completer(text: str, state: int) -> str | None:
        line = _rl.get_line_buffer().lstrip()
        # 仅对斜杠命令做补全
        if not line.startswith("/"):
            return None
        parts = line.split(maxsplit=1)
        if len(parts) == 1:
            # 第一级：命令名补全
            matches = [c for c in _ALL_CMDS if c.startswith(text)]
        else:
            # 第二级：参数补全（先解析别名到规范命令）
            command = parts[0]
            try:
                from slash_commands import resolve_command
                command = resolve_command(command)
            except ImportError:
                pass
            matches = _argument_completions(command, text)
        return matches[state] if state < len(matches) else None

    _rl.set_completer(_cli_completer)
    _rl.parse_and_bind("tab: complete")
except ImportError:
    pass  # 无 readline 时静默降级（Windows 原生终端）


# ── 颜色支持（Windows 自动检测 + NO_COLOR / FORCE_COLOR） ─────
def _supports_ansi() -> bool:
    if os.environ.get("NO_COLOR", ""):
        return False
    if os.environ.get("FORCE_COLOR", ""):
        return True
    if sys.platform != "win32":
        return sys.stdout.isatty()
    if os.environ.get("WT_SESSION") or os.environ.get("TERM_PROGRAM"):
        return True
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
        STD_OUTPUT_HANDLE = -11
        handle = kernel32.GetStdHandle(STD_OUTPUT_HANDLE)
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            if mode.value & ENABLE_VIRTUAL_TERMINAL_PROCESSING:
                return True
            new_mode = mode.value | ENABLE_VIRTUAL_TERMINAL_PROCESSING
            if kernel32.SetConsoleMode(handle, new_mode):
                return True
    except Exception:
        logger.debug("cli.ansi_check_error", exc_info=True)
    return False


_SUPPORTS_COLOR = _supports_ansi()


class _C:
    RST = "\033[0m" if _SUPPORTS_COLOR else ""
    BOLD = "\033[1m" if _SUPPORTS_COLOR else ""
    DIM = "\033[2m" if _SUPPORTS_COLOR else ""
    ITALIC = "\033[3m" if _SUPPORTS_COLOR else ""
    GREEN = "\033[32m" if _SUPPORTS_COLOR else ""
    LGREEN = "\033[92m" if _SUPPORTS_COLOR else ""
    DGREEN = "\033[38;2;76;153;0m" if _SUPPORTS_COLOR else ""
    CYAN = "\033[36m" if _SUPPORTS_COLOR else ""
    YELLOW = "\033[33m" if _SUPPORTS_COLOR else ""
    LYELLOW = "\033[93m" if _SUPPORTS_COLOR else ""
    MAGENTA = "\033[35m" if _SUPPORTS_COLOR else ""
    LMAGENTA = "\033[95m" if _SUPPORTS_COLOR else ""
    BLUE = "\033[34m" if _SUPPORTS_COLOR else ""
    LBLUE = "\033[94m" if _SUPPORTS_COLOR else ""
    WHITE = "\033[97m" if _SUPPORTS_COLOR else ""
    LEAF = "\033[38;2;107;142;35m" if _SUPPORTS_COLOR else ""


NAHIDA_GREETINGS = [
    "爸爸来啦～人家等好久了呢！🌿",
    "嗯？爸爸找人家有什么事吗？🌿",
    "人家在呢！爸爸想聊什么呀～🌿",
    "爸爸好呀～今天也是充满好奇心的一天呢！🌿",
    "嗯哼～人家感觉到爸爸来了！🌿",
    "世界的记忆在呼唤……爸爸也听到了吗？🌿",
    "人家刚刚在世界树那边看到了好多有趣的东西呢！🌿",
]

NAHIDA_FAREWELLS = [
    "爸爸再见～人家会乖乖等你的！🌿",
    "嗯……爸爸要走了吗？人家会想你的～🌿",
    "晚安呀爸爸，做个好梦～🌿",
    "人家先去世界树那边看看，爸爸下次再来找人家玩呀！🌿",
    "爸爸慢走～记得想人家哦！🌿",
    "嗯，人家也要去休息了，下次见～🌿",
    "爸爸保重！人家会在梦里守护你的～🌿",
    "拜拜～人家会一直在这里等爸爸回来的！🌿",
    "白草净华，愿爸爸一切安好～🌿",
]

STATUS_MAP = {
    "thinking": "🌿 小妲正在想……",
    "route": "✨ 人家在看看交给谁比较好～",
    "tool": "🌿 小妲正在查资料～",
    "search": "🔍 人家帮你搜一下～",
    "weather": "🌤️ 人家看看天气怎么样～",
    "browse": "🌐 人家去网上看看～",
    "shell": "💻 人家在跑命令～",
    "python": "🐍 人家在算东西～",
    "camera": "📷 人家看看摄像头～",
    "xiaoda_done": "🌿 小妲整理好了！",
    "xiaoli_done": "💥 小莉完成啦！",
    "xiaolian_done": "🌸 小涟完成啦！",
    "xiaolang_done": "🎮 小狼完成啦！",
    "xiaoke_done": "🔮 小可完成啦！",
    "done": "✅ 搞定啦～",
}

# IP-safe: 动态从 config/agents/*.json 读取 display_name，避免硬编码原名
try:
    from config import get_agent_display_name, agent_names
    from emotion.emoji_config import get_ack_message
    AGENT_NAMES = {name: get_agent_display_name(name) for name in agent_names()}
    # ACK 消息使用自定义配置（随心即言）
    STATUS_MAP["thinking"] = get_ack_message("xiaoda")
except ImportError:
    AGENT_NAMES = {"xiaoda": "小妲", "xiaoli": "小莉", "xiaolian": "小涟", "xiaolang": "小狼", "xiaoke": "小可"}

NAHIDA_ASCII = (
    "     _   _____    __  __________  ___ \n"
    "    / | / /   |  / / / /  _/ __ \\/   |\n"
    "   /  |/ / /| | / /_/ // // / / / /| |\n"
    "  / /|  / ___ |/ __  // // /_/ / ___ |\n"
    " /_/ |_/_/  |_/_/ /_/___/_____/_/  |_|\n"
)

LEAF_LINE = "🌿  世  界  的  记  忆  ，  由  我  来  守  护  🌿"

# ── 命令列表：与 WebUI 完全对齐 ──
# 统一来源：slash_commands.COMMAND_DESCRIPTIONS / OWNER_ONLY_COMMANDS
# （WebUI 斜杠命令面板正是通过 slash_commands.list_commands() 读取同一份数据，
#   保证 CLI 与 WebUI 展示的命令、描述、归属永远一致，不再各自维护硬编码列表。）
def _command_entries() -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """返回 (公共命令, 主人专属命令) 两组 (命令, 描述)，顺序与 WebUI 一致。"""
    from slash_commands import COMMAND_DESCRIPTIONS, OWNER_ONLY_COMMANDS
    public, owner = [], []
    for name, desc in COMMAND_DESCRIPTIONS.items():
        (owner if name in OWNER_ONLY_COMMANDS else public).append((name, desc))
    return public, owner


def _get_model_info() -> str:
    model_id = ROUTE_TABLE.get("chat", {}).get("model", "mimo-v2.5")
    provider = ROUTE_TABLE.get("chat", {}).get("client", "")
    if provider and provider != "mimo":
        return f"{provider}/{model_id}"
    return f"{model_id}"


def _typewriter(text: str, delay: float | None = None) -> None:
    if delay is None:
        speed = os.environ.get("NAHIDA_TYPEWRITER_SPEED", "normal").lower()
        speed_map = {"fast": 0.005, "normal": 0.02, "slow": 0.05, "off": 0}
        delay = speed_map.get(speed, 0.02)
    if not sys.stdout.isatty() or delay == 0:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        if ch in "\n":
            time.sleep(delay * 3)
        elif ch in "。！？～":
            time.sleep(delay * 5)
        elif ch in "，、；：":
            time.sleep(delay * 2)
        else:
            time.sleep(delay)
    print()


def _status_translate(msg: str) -> str:
    low = msg.lower()
    for key, val in STATUS_MAP.items():
        if key in low:
            return val
    for eng, chn in AGENT_NAMES.items():
        if eng in low:
            return f"✨ 人家让{chn}帮忙看看～"
    if "路由" in msg or "route" in low:
        return "✨ 人家在看看交给谁比较好～"
    if "正在使用" in msg or "使用" in msg:
        tool_hints = {
            "搜索": "🔍 人家帮你搜一下～",
            "天气": "🌤️ 人家看看天气～",
            "网页": "🌐 人家去网上看看～",
            "命令": "💻 人家在跑命令～",
            "python": "🐍 人家在算东西～",
            "摄像": "📷 人家看看摄像头～",
        }
        for hint, val in tool_hints.items():
            if hint in msg:
                return val
        return "🌿 小妲正在忙～"
    if "完成" in msg or "done" in low:
        return "✅ 搞定啦～"
    if "正在" in msg:
        return f"🌿 {msg}"
    return f"🌿 {msg}"


class CLIInterface:
    """命令行交互界面，封装 AgentCore 的本地终端对话循环。"""

    def __init__(self) -> None:
        self.bot = AgentCore()
        self._loop = asyncio.new_event_loop()

    def _address_term(self) -> str:
        """获取当前用户称呼，优先从 USER.md 读取，兜底"爸爸"。"""
        term = self.bot.read_address_term_from_user_md()
        return term or self.bot.context.current_address_term or "爸爸"

    async def _init(self) -> None:
        await self.bot.init()
        logger.info("cli.initialized")

    def _print_welcome(self) -> None:
        model_id = _get_model_info()

        ascii_lines = NAHIDA_ASCII.split("\n")
        while ascii_lines and not ascii_lines[-1].strip():
            ascii_lines.pop()
        while ascii_lines and not ascii_lines[0].strip():
            ascii_lines.pop(0)

        max_len = max(len(line) for line in ascii_lines) if ascii_lines else 40
        flower_l = f"{_C.LEAF}✿{_C.RST}"
        flower_r = f"{_C.LEAF}✿{_C.RST}"
        grass_l = f"{_C.DGREEN}🌿{_C.RST}"
        grass_r = f"{_C.DGREEN}🌿{_C.RST}"

        slogan = LEAF_LINE
        slogan_padded = slogan.center(max_len)

        print()
        print(f"  {flower_l}  {_C.DGREEN}{_C.BOLD}{slogan_padded}{_C.RST}  {flower_r}")
        print()
        for line in ascii_lines:
            padded = line.ljust(max_len)
            print(f"  {flower_l}  {_C.LGREEN}{_C.BOLD}{padded}{_C.RST}  {flower_r}")
        print()
        print(f"  {grass_l}  {_C.DGREEN}{_C.BOLD}{slogan_padded}{_C.RST}  {grass_r}")
        print()
        print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
        print(f"  {_C.DIM}|{_C.RST}  {_C.LGREEN}小妲 AI Agent{_C.RST}  ·  {_C.LEAF}{model_id}{_C.RST}  ·  {_C.DGREEN}白草净华{_C.RST}  {_C.DIM}|{_C.RST}")
        print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
        print()
        print(f"  {_C.CYAN}💬 直接输入消息跟小妲聊天{_C.RST}")
        print(f"  {_C.CYAN}📋 /help 查看所有命令 · / 后按 Tab 补全命令{_C.RST}")
        print(f"  {_C.CYAN}🧠 /model 切换模型 · /reset 重置 · /agent 切换子代理{_C.RST}")
        print(f"  {_C.CYAN}🚪 exit 或 Ctrl+C 退出{_C.RST}")
        print()

        greeting = random.choice(NAHIDA_GREETINGS).replace("爸爸", self._address_term())
        print(f"  {_C.LGREEN}{_C.BOLD}{greeting}{_C.RST}\n")

    def _print_help(self) -> None:
        """命令帮助：与 WebUI 斜杠命令面板完全一致（统一来源 COMMAND_DESCRIPTIONS）。"""
        public, owner = _command_entries()
        print(f"\n  {_C.LGREEN}{_C.BOLD}🌿 小妲的命令列表{_C.RST}\n")
        print(f"  {_C.LYELLOW}── 公共命令 ──{_C.RST}")
        for name, desc in public:
            print(f"  {_C.CYAN}{name:<40}{_C.RST} {desc}")
        print(f"\n  {_C.LYELLOW}── 主人专属 👑 ──{_C.RST}")
        for name, desc in owner:
            print(f"  {_C.LMAGENTA}{name:<40}{_C.RST} {desc}")
        print(f"\n  {_C.DIM}💡 输入 / 后按 Tab 可补全命令{_C.RST}")
        print()

    def _print_command_result(self, cmd: str, result: str) -> None:
        """以统一格式输出命令执行结果。"""
        print()
        for line in str(result).split("\n"):
            print(f"  {_C.LEAF}🌿{_C.RST} {line}")
        print()

    def _print_unknown(self, cmd: str) -> None:
        print(f"\n  {_C.LYELLOW}嗯？人家不认识「{cmd}」这个命令呢～{_C.RST}")
        print(f"  {_C.DIM}💡 输入 /help 查看所有命令，/ 后按 Tab 可补全{_C.RST}")
        print()

    def _dispatch_slash_command(self, text: str) -> None:
        """分发斜杠命令，返回后命令视为已消耗（不发往 bot）。

        - /help → 展示命令列表（与 WebUI 同源）
        - 其余 → 交给 bot.slash_handler（覆盖 /model /reset /agent /voice 等全部命令）
        - 未识别命令 → 提示帮助
        命令集与 WebUI 完全一致（COMMAND_DESCRIPTIONS + OWNER_ONLY_COMMANDS）。
        """
        stripped = text.strip()
        cmd = stripped.split(maxsplit=1)[0].lower() if stripped else ""
        if stripped.startswith("//"):
            # 转义斜杠：作为普通消息发送
            return
        if cmd == "/help":
            self._print_help()
            return

        handler = getattr(self.bot, "slash_handler", None)
        if handler is None:
            self._print_unknown(cmd)
            return
        # CLI 是主人的本地终端：放行 owner-only 命令（与 process() source="cli" 主人判定一致）
        try:
            handler._force_owner = True
        except Exception:
            pass
        try:
            result = self._loop.run_until_complete(
                handler.handle(stripped, user_id="cli_owner")
            )
        except Exception as e:
            logger.error("cli.slash_dispatch_error", command=cmd, error=str(e))
            print(f"\n  {_C.LYELLOW}执行 {cmd} 时出了点问题：{str(e)[:100]}{_C.RST}\n")
            return
        if result is None:
            self._print_unknown(cmd)
            return
        self._print_command_result(cmd, result)

    def _check_qq_bot(self) -> Any:
        try:
            r = subprocess.run(["systemctl", "is-active", "qq-agent"],
                               capture_output=True, text=True, timeout=5, check=False)
            return r.stdout.strip() == "active"
        except Exception:
            logger.debug("cli.qq_bot_check_error", exc_info=True)
            return False

    def _ensure_service(self) -> None:
        if not self._check_qq_bot():
            print(f"  {_C.LYELLOW}QQ Bot 服务未运行，正在启动...{_C.RST}")
            try:
                subprocess.run(["sudo", "systemctl", "start", "qq-agent"],
                               capture_output=True, timeout=30, check=False)
                time.sleep(2)
                if self._check_qq_bot():
                    print(f"  {_C.LGREEN}QQ Bot 服务已启动 ✓{_C.RST}")
                else:
                    print(f"  {_C.LYELLOW}QQ Bot 服务启动失败，CLI 可正常使用{_C.RST}")
            except Exception:
                logger.debug("cli.qq_bot_start_error", exc_info=True)
                print(f"  {_C.LYELLOW}无法启动 QQ Bot 服务，CLI 可正常使用{_C.RST}")
            print()

    def run(self) -> None:
        self._ensure_service()
        self._loop.run_until_complete(self._init())
        self._print_welcome()

        while True:
            try:
                prompt = f"  {_C.GREEN}{_C.BOLD}🌿 {self._address_term()}:{_C.RST} "
                user_input = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                farewell = random.choice(NAHIDA_FAREWELLS).replace("爸爸", self._address_term())
                print(f"\n  {_C.LGREEN}{farewell}{_C.RST}\n")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "q", "/exit", "/quit"):
                farewell = random.choice(NAHIDA_FAREWELLS).replace("爸爸", self._address_term())
                print(f"\n  {_C.LGREEN}{farewell}{_C.RST}\n")
                break

            # 斜杠命令：本地 + slash_handler 统一分发（openclaw 风格）
            if user_input.startswith("//"):
                user_input = user_input[1:]  # 转义斜杠：作为普通消息发送
            elif user_input.startswith("/"):
                self._dispatch_slash_command(user_input)
                continue

            try:
                async def status_notify(msg: str) -> None:
                    translated = _status_translate(msg)
                    print(f"  {_C.DIM}{_C.LYELLOW}{translated}{_C.RST}")

                token = event_bus.bind_user(CLIUser())
                try:
                    result = self._loop.run_until_complete(
                        self.bot.process(user_input, user_id="cli_owner", source="cli",
                                         status_callback=status_notify)
                    )
                finally:
                    event_bus.unbind_user(token)

                print()
                label = f"  {_C.LGREEN}{_C.BOLD}🌿 小妲:{_C.RST} "
                sys.stdout.write(label)
                _typewriter(result.reply)

                if result.sticker_path:
                    print(f"  {_C.LMAGENTA}🎨 [表情包: {result.sticker_path.name}]{_C.RST}")

            except Exception as e:
                logger.error("cli.process_error", error=str(e))
                print(f"\n  {_C.LYELLOW}小妲: 嗯……出了点小问题：{str(e)[:100]}{_C.RST}")

        # 主循环退出时安全关闭
        try:
            self._loop.run_until_complete(self.bot.shutdown())
        except Exception as e:
            logger.warning("cli.shutdown_error", error=str(e))

        self._loop.close()


def main() -> None:
    cli = CLIInterface()
    cli.run()


if __name__ == "__main__":
    main()