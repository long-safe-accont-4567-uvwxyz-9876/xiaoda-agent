import os
import sys
import time
import random
import asyncio
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

import cli_client
import contextlib

# ── prompt_toolkit 支持（/ 弹出下拉 + 菜单选择）──────────────
# 缺失时优雅回退到 readline 路径，不崩溃（旧安装包兼容）。
try:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import Completer, Completion
    from prompt_toolkit.formatted_text import ANSI
    from prompt_toolkit.history import FileHistory
    _HAS_PROMPT_TOOLKIT = True
except ImportError:
    _HAS_PROMPT_TOOLKIT = False

try:
    from cli_menu import MenuItem, select_from_menu
    _HAS_MENU = True
except ImportError:
    _HAS_MENU = False

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
        from slash_commands import COMMAND_ALIASES, COMMAND_DESCRIPTIONS
        names.extend(COMMAND_DESCRIPTIONS.keys())
        names.extend(COMMAND_ALIASES.keys())
    except ImportError:
        pass
    return sorted(set(names))


_ALL_CMD_NAMES = _all_command_names()  # 与 WebUI 同源（COMMAND_DESCRIPTIONS + 别名）

# 多步命令：无参数时弹出菜单选择，Esc 取消不误发裸命令
_MULTI_STEP_COMMANDS = {"/model", "/agent", "/voice", "/doctor", "/cost", "/cam"}


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


class _SlashCompleter(Completer):
    """斜杠命令补全：/ 输入即弹出命令下拉，命令后跟空格补全参数。

    命令名来源与 WebUI 同源（slash_commands.COMMAND_DESCRIPTIONS + 别名）。
    /model 参数动态从模型发现缓存实时补全，其余命令用 slash_commands 声明式参数。
    """

    def _arg_completions(self, command: str, partial: str) -> list[str]:
        if command == "/model":
            try:
                from model_router import list_discovered_model_ids
                opts = list_discovered_model_ids()
            except Exception:
                opts = []
        else:
            try:
                from slash_commands import get_argument_completions
                opts = get_argument_completions(command, partial)
            except ImportError:
                opts = []
        return [o for o in opts if o.startswith(partial)]

    def get_completions(self, document, complete_event):
        line = document.current_line_before_cursor.lstrip()
        if not line.startswith("/"):
            return
        parts = line.split(maxsplit=1)
        word = document.get_word_before_cursor(WORD=True)
        start = -len(word) if word else 0
        if len(parts) == 1:
            for name in _ALL_CMD_NAMES:
                if word and name.startswith(word) and name != word:
                    yield Completion(name, start_position=start)
        else:
            command = parts[0]
            try:
                from slash_commands import resolve_command
                command = resolve_command(command)
            except ImportError:
                pass
            for cand in self._arg_completions(command, word):
                yield Completion(cand, start_position=start)


try:
    import readline as _rl
    _ALL_CMDS = _all_command_names()

    def _cli_completer(text: str, state: int) -> str | None:
        line = _rl.get_line_buffer().lstrip()
        # 仅对斜杠命令做补全
        if not line.startswith("/"):
            return None
        parts = line.split(maxsplit=1)
        # split(maxsplit=1) 会丢弃尾随空格（如 "/model " → ["/model"]），
        # 需显式判断命令后是否还有内容，否则 "/model <Tab>" 会误入命令名补全分支。
        if len(parts) == 1 and not line[len(parts[0]):]:
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


def _get_model_info(token: str = "") -> str:
    """返回当前聊天模型显示名，从主进程远程读取（与 WebUI 单一数据源同步）。

    旧实现直接读本地 ROUTE_TABLE（.env 默认 mimo-v2.5），不反映 WebUI 切换后的
    模型。CLI 不再自建 AgentCore，改为远程读取 POST /models/chat-model 的当前值，
    天然与 WebUI 共享同一份模型状态。
    """
    if token:
        return cli_client.get_chat_model_label(token)
    return "mimo-v2.5"


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
    """命令行交互界面：作为主进程（nahida-web）的客户端，共享同一 AgentCore。

    不再自建 AgentCore，而是通过 HTTP（token 认证 + 斜杠命令）与 WebSocket
    （对话）连接主进程，复用其已初始化的 AgentCore —— 与 Web UI / QQ / 微信
    同一进程，记忆、模型、上下文天然共享。
    """

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._token = ""
        self._ws: cli_client.WSClient | None = None

    def _address_term(self) -> str:
        """获取当前用户称呼。

        从 USER.md 的"- 称呼：xxx"动态读取（可在设置页修改）；未设置时兜底"朋友"
        （与设置页占位符"留空则默认「朋友」"一致），不硬编码某种称呼。
        """
        try:
            from agent_core.core import AgentCore
            term = AgentCore.read_address_term_from_user_md()
            if term:
                return term
        except ImportError:
            pass
        return "朋友"

    def _init_prompt_session(self) -> None:
        """初始化 prompt_toolkit 会话（含历史、自动建议、斜杠补全）。"""
        if not _HAS_PROMPT_TOOLKIT:
            self._session = None
            return
        hist_path = os.path.expanduser("~/.ai-agent/cli_history")
        self._session = PromptSession(
            history=FileHistory(hist_path),
            auto_suggest=AutoSuggestFromHistory(),
            completer=_SlashCompleter(),
            complete_while_typing=True,
        )

    def _menu_fixed(self, cmd: str, choices: list[str]) -> str | None:
        """固定参数多步命令：从 choices 单选，返回完整命令。"""
        items = [MenuItem(label=v, value=f"{cmd} {v}") for v in choices]
        return select_from_menu(f"{cmd} · 选择参数", items)

    def _menu_model(self) -> str | None:
        """/model 多步：先选 provider，再选该 provider 下模型。失败返回 None。"""
        providers = cli_client.discover_models(self._token)
        if not providers:
            print(f"\n  {_C.LYELLOW}模型选项加载失败，请手动输入 /model provider/模型{_C.RST}")
            return None
        p_items = []
        for p in providers:
            pid = p.get("provider") or p.get("id") or ""
            if not pid:
                continue
            models = p.get("models") or []
            p_items.append(MenuItem(
                label=str(p.get("label") or pid),
                value=pid,
                description=f"{len(models)} 个模型",
            ))
        pid = select_from_menu("/model · 选择模型提供方", p_items)
        if pid is None:
            return None
        provider = next(
            (p for p in providers if (p.get("provider") or p.get("id")) == pid), None)
        models = (provider or {}).get("models") or []
        m_items = [
            MenuItem(
                label=str(m.get("display_name") or m.get("id") or ""),
                value=f"{pid}/{m['id']}",
            )
            for m in models if m.get("id")
        ]
        picked = select_from_menu(f"/model · {pid} 选择模型", m_items)
        if picked is None:
            return None
        return f"/model {picked}"

    def _menu_agent(self) -> str | None:
        """/agent 多步：从代理列表单选。失败返回 None。"""
        agents = cli_client.list_agents(self._token)
        if not agents:
            print(f"\n  {_C.LYELLOW}代理列表加载失败，请手动输入 /agent 名称{_C.RST}")
            return None
        items = [
            MenuItem(label=str(a.get("display_name") or a.get("name") or ""),
                     value=str(a["name"]))
            for a in agents if a.get("name")
        ]
        picked = select_from_menu("/agent · 选择子代理", items)
        if picked is None:
            return None
        return f"/agent {picked}"

    def _try_expand_multistep(self, cmd: str, arg: str) -> str | None:
        """多步命令无参数时弹菜单，返回完整命令；否则返回 None（不展开）。"""
        if arg.strip() or not _HAS_MENU:
            return None
        if cmd == "/model":
            return self._menu_model()
        if cmd == "/agent":
            return self._menu_agent()
        if cmd == "/voice":
            return self._menu_fixed("/voice", ["on", "off"])
        if cmd == "/doctor":
            return self._menu_fixed("/doctor", ["json", "fix"])
        if cmd == "/cost":
            return self._menu_fixed("/cost", ["7d"])
        if cmd == "/cam":
            return self._menu_fixed("/cam", ["snap"])
        return None

    # ── 主进程连接 ────────────────────────────────────────────
    def _connect_main_process(self) -> bool:
        """确保主进程可用并建立连接。失败时打印原因并返回 False（不闪退）。"""
        def status(msg: str) -> None:
            print(f"  {_C.DIM}{_C.LYELLOW}{msg}{_C.RST}")

        if not cli_client.ensure_main_process(on_status=status):
            print()
            print(f"  {_C.LYELLOW}主进程不可用，CLI 无法连接。请检查 nahida-web 服务状态。{_C.RST}")
            return False

        # 获取 token。优先用 .env 的 WEBUI_PASSWORD（cli.py 顶部已 load_dotenv），
        # 避免每次启动都手动输入密码；未配置密码时直接签发，显式传入覆盖。
        pwd = os.getenv("WEBUI_PASSWORD", "") or ""
        try:
            self._token = cli_client.fetch_token(password=pwd)
        except RuntimeError as e:
            if any(tag in str(e) for tag in ("401", "403", "422")):
                try:
                    import getpass
                    pwd = getpass.getpass("请输入 WebUI 访问密码: ")
                except Exception:
                    pwd = ""
                try:
                    self._token = cli_client.fetch_token(password=pwd)
                except Exception as e2:
                    print(f"\n  {_C.LYELLOW}认证失败: {str(e2)[:100]}{_C.RST}")
                    return False
            else:
                print(f"\n  {_C.LYELLOW}获取主进程 token 失败: {str(e)[:100]}{_C.RST}")
                return False

        self._ws = cli_client.WSClient(self._token)
        try:
            self._loop.run_until_complete(self._ws.connect())
        except Exception as e:
            print(f"\n  {_C.LYELLOW}连接主进程失败: {str(e)[:100]}{_C.RST}")
            return False
        return True

    def _print_welcome(self) -> None:
        model_id = _get_model_info(self._token)

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
        """分发斜杠命令：/help 本地展示，其余交给主进程共享 AgentCore 处理。

        多步命令（/model /agent /voice /doctor /cost /cam）在无参数时先弹菜单选择，
        拼接完整命令后发送；有参数则直接发送。主进程 core.process() 内部识别并
        执行命令，故 CLI 无需本地 AgentCore。
        """
        stripped = text.strip()
        if stripped.startswith("//"):
            return  # 转义斜杠：作为普通消息发送
        cmd, _, arg = stripped.partition(" ")
        cmd_l = cmd.lower()
        if cmd_l == "/help":
            self._print_help()
            return
        # 多步命令：无参数时弹出菜单选择
        if not arg.strip():
            expanded = self._try_expand_multistep(cmd_l, arg)
            if expanded is not None:
                stripped = expanded
                cmd = stripped.split(maxsplit=1)[0].lower()
            elif _HAS_MENU and cmd_l in _MULTI_STEP_COMMANDS:
                # 多步命令：用户 Esc 取消或选项加载失败（已打印提示），不误发裸命令
                return
        if self._ws is None:
            self._print_unknown(cmd)
            return
        try:
            result = self._loop.run_until_complete(self._ws.chat(stripped))
        except Exception as e:
            logger.error("cli.slash_dispatch_error", command=cmd, error=str(e))
            print(f"\n  {_C.LYELLOW}执行 {cmd} 时出了点问题：{str(e)[:100]}{_C.RST}\n")
            return
        if result is None or not str(result).strip():
            self._print_unknown(cmd)
            return
        self._print_command_result(cmd, result)

    def _send_message(self, user_input: str) -> None:
        """发送普通消息到主进程，并显示最终回复。"""
        if self._ws is None:
            print(f"\n  {_C.LYELLOW}小妲: 尚未连接主进程，请重新启动 CLI。{_C.RST}")
            return

        async def status_notify(msg: str) -> None:
            translated = _status_translate(msg)
            print(f"  {_C.DIM}{_C.LYELLOW}{translated}{_C.RST}")

        try:
            reply = self._loop.run_until_complete(
                self._ws.chat(user_input, status_callback=status_notify)
            )
        except Exception as e:
            logger.error("cli.chat_error", error=str(e))
            print(f"\n  {_C.LYELLOW}小妲: 嗯……出了点小问题：{str(e)[:100]}{_C.RST}")
            return
        print()
        label = f"  {_C.LGREEN}{_C.BOLD}🌿 小妲:{_C.RST} "
        sys.stdout.write(label)
        _typewriter(reply)

    def run(self) -> None:
        if not self._connect_main_process():
            return
        self._print_welcome()
        self._init_prompt_session()

        while True:
            try:
                prompt = f"  {_C.GREEN}{_C.BOLD}🌿 {self._address_term()}:{_C.RST} "
                if self._session is not None:
                    user_input = self._session.prompt(message=ANSI(prompt)).strip()
                else:
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

            # 转义斜杠：作为普通消息发送
            if user_input.startswith("//"):
                user_input = user_input[1:]
            elif user_input.startswith("/"):
                self._dispatch_slash_command(user_input)
                continue

            self._send_message(user_input)

        # 主循环退出时关闭 WebSocket 连接
        if self._ws is not None:
            try:
                self._loop.run_until_complete(self._ws.close())
            except Exception as e:
                logger.warning("cli.ws_close_error", error=str(e))
        self._loop.close()


def main() -> None:
    cli = CLIInterface()
    cli.run()


if __name__ == "__main__":
    main()