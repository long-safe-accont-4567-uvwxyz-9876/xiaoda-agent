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
from model_router import ROUTE_TABLE, MODEL_PREFERENCES

# ── readline 支持 ──────────────────────────────────────────
try:
    import readline
    _HIST_FILE = os.path.expanduser("~/.ai-agent/cli_history")
    _HIST_SIZE = 500
    try:
        readline.read_history_file(_HIST_FILE)
    except FileNotFoundError:
        pass
    readline.set_history_length(_HIST_SIZE)
    import atexit
    atexit.register(lambda: readline.write_history_file(_HIST_FILE))
except ImportError:
    pass  # readline 不可用时静默降级


# ── NO_COLOR 支持 ─────────────────────────────────────────
_NO_COLOR = bool(os.environ.get("NO_COLOR", ""))


class _C:
    RST = "" if _NO_COLOR else "\033[0m"
    BOLD = "" if _NO_COLOR else "\033[1m"
    DIM = "" if _NO_COLOR else "\033[2m"
    ITALIC = "" if _NO_COLOR else "\033[3m"
    GREEN = "" if _NO_COLOR else "\033[32m"
    LGREEN = "" if _NO_COLOR else "\033[92m"
    DGREEN = "" if _NO_COLOR else "\033[38;2;76;153;0m"
    CYAN = "" if _NO_COLOR else "\033[36m"
    YELLOW = "" if _NO_COLOR else "\033[33m"
    LYELLOW = "" if _NO_COLOR else "\033[93m"
    MAGENTA = "" if _NO_COLOR else "\033[35m"
    LMAGENTA = "" if _NO_COLOR else "\033[95m"
    BLUE = "" if _NO_COLOR else "\033[34m"
    LBLUE = "" if _NO_COLOR else "\033[94m"
    WHITE = "" if _NO_COLOR else "\033[97m"
    LEAF = "" if _NO_COLOR else "\033[38;2;107;142;35m"


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
    "thinking": "🌿 纳西妲正在想……",
    "route": "✨ 人家在看看交给谁比较好～",
    "tool": "🌿 纳西妲正在查资料～",
    "search": "🔍 人家帮你搜一下～",
    "weather": "🌤️ 人家看看天气怎么样～",
    "browse": "🌐 人家去网上看看～",
    "shell": "💻 人家在跑命令～",
    "python": "🐍 人家在算东西～",
    "camera": "📷 人家看看摄像头～",
    "nahida_done": "🌿 纳西妲整理好了！",
    "klee_done": "💥 可莉完成啦！",
    "xilian_done": "🌸 昔涟完成啦！",
    "yinlang_done": "🎮 银狼完成啦！",
    "xike_done": "🔮 尼可完成啦！",
    "done": "✅ 搞定啦～",
}

AGENT_NAMES = {
    "nahida": "纳西妲",
    "klee": "可莉",
    "xilian": "昔涟",
    "yinlang": "银狼",
    "nico": "尼可",
    "xike": "尼可",
}

NAHIDA_ASCII = (
    "     _   _____    __  __________  ___ \n"
    "    / | / /   |  / / / /  _/ __ \\/   |\n"
    "   /  |/ / /| | / /_/ // // / / / /| |\n"
    "  / /|  / ___ |/ __  // // /_/ / ___ |\n"
    " /_/ |_/_/  |_/_/ /_/___/_____/_/  |_|\n"
)

LEAF_LINE = "🌿  世  界  的  记  忆  ，  由  我  来  守  护  🌿"

HELP_PUBLIC = [
    ("💰", "/cost [7d]", "查看API消耗"),
    ("📊", "/status", "查看Agent状态"),
    ("🧹", "/forget", "清除短期对话记忆"),
    ("📚", "/learn", "查看学习记录"),
    ("📓", "/note", "查看笔记本"),
    ("🖥️", "/hw", "查看香橙派硬件状态"),
    ("📷", "/cam", "拍照并分析画面"),
    ("⚙️", "/sys", "查看系统运行状态"),
    ("❓", "/help", "显示此帮助"),
]

HELP_OWNER = [
    ("🤖", "/model [mimo|mimo-pro]", "切换模型模式"),
    ("🔄", "/reset", "重置对话上下文"),
    ("🎙️", "/voice [on|off]", "切换语音模式"),
    ("🎭", "/agent [名称]", "切换对话目标Agent"),
]


def _get_model_info() -> str:
    model_id = ROUTE_TABLE.get("chat", {}).get("model", "mimo-v2.5")
    pref = MODEL_PREFERENCES.get("mimo", {}).get("label", "MiMo")
    return f"{model_id}"


def _typewriter(text: str, delay: float | None = None):
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
        return "🌿 纳西妲正在忙～"
    if "完成" in msg or "done" in low:
        return "✅ 搞定啦～"
    if "正在" in msg:
        return f"🌿 {msg}"
    return f"🌿 {msg}"


class CLIInterface:

    def __init__(self):
        self.bot = AgentCore()
        self._loop = asyncio.new_event_loop()

    async def _init(self):
        await self.bot.init()
        logger.info("cli.initialized")

    def _print_welcome(self):
        model_id = _get_model_info()

        ascii_lines = NAHIDA_ASCII.split("\n")
        while ascii_lines and not ascii_lines[-1].strip():
            ascii_lines.pop()
        while ascii_lines and not ascii_lines[0].strip():
            ascii_lines.pop(0)

        max_len = max(len(l) for l in ascii_lines) if ascii_lines else 40
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
        print(f"  {_C.DIM}|{_C.RST}  {_C.LGREEN}纳西妲 AI Agent{_C.RST}  ·  {_C.LEAF}{model_id}{_C.RST}  ·  {_C.DGREEN}白草净华{_C.RST}  {_C.DIM}|{_C.RST}")
        print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
        print()
        print(f"  {_C.CYAN}💬 直接输入消息跟纳西妲聊天{_C.RST}")
        print(f"  {_C.CYAN}📋 /help 查看所有命令{_C.RST}")
        print(f"  {_C.CYAN}🚪 exit 或 Ctrl+C 退出{_C.RST}")
        print()

        greeting = random.choice(NAHIDA_GREETINGS)
        print(f"  {_C.LGREEN}{_C.BOLD}{greeting}{_C.RST}\n")

    def _print_help(self):
        print(f"\n  {_C.LGREEN}{_C.BOLD}🌿 纳西妲的命令列表{_C.RST}\n")
        print(f"  {_C.LYELLOW}── 公共命令 ──{_C.RST}")
        for emoji, cmd, desc in HELP_PUBLIC:
            print(f"  {emoji} {_C.CYAN}{cmd:<24}{_C.RST} {desc}")
        print(f"\n  {_C.LYELLOW}── 主人专属 ──{_C.RST}")
        for emoji, cmd, desc in HELP_OWNER:
            print(f"  {emoji} {_C.LMAGENTA}{cmd:<24}{_C.RST} {desc}")
        print()

    def _check_qq_bot(self):
        try:
            r = subprocess.run(["systemctl", "is-active", "qq-agent"],
                               capture_output=True, text=True, timeout=5)
            return r.stdout.strip() == "active"
        except Exception:
            return False

    def _ensure_service(self):
        if not self._check_qq_bot():
            print(f"  {_C.LYELLOW}QQ Bot 服务未运行，正在启动...{_C.RST}")
            try:
                subprocess.run(["sudo", "systemctl", "start", "qq-agent"],
                               capture_output=True, timeout=30)
                time.sleep(2)
                if self._check_qq_bot():
                    print(f"  {_C.LGREEN}QQ Bot 服务已启动 ✓{_C.RST}")
                else:
                    print(f"  {_C.LYELLOW}QQ Bot 服务启动失败，CLI 可正常使用{_C.RST}")
            except Exception:
                print(f"  {_C.LYELLOW}无法启动 QQ Bot 服务，CLI 可正常使用{_C.RST}")
            print()

    def run(self):
        self._ensure_service()
        self._loop.run_until_complete(self._init())
        self._print_welcome()

        while True:
            try:
                prompt = f"  {_C.GREEN}{_C.BOLD}🌿 爸爸:{_C.RST} "
                user_input = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                farewell = random.choice(NAHIDA_FAREWELLS)
                print(f"\n  {_C.LGREEN}{farewell}{_C.RST}\n")
                break

            if not user_input:
                continue

            if user_input.lower() in ("exit", "quit", "q"):
                farewell = random.choice(NAHIDA_FAREWELLS)
                print(f"\n  {_C.LGREEN}{farewell}{_C.RST}\n")
                break

            if user_input.strip() == "/help":
                self._print_help()
                continue

            try:
                async def status_notify(msg: str):
                    translated = _status_translate(msg)
                    print(f"  {_C.DIM}{_C.LYELLOW}{translated}{_C.RST}")

                result = self._loop.run_until_complete(
                    self.bot.process(user_input, user_id="cli_owner", source="cli",
                                     status_callback=status_notify)
                )

                print()
                label = f"  {_C.LGREEN}{_C.BOLD}🌿 纳西妲:{_C.RST} "
                sys.stdout.write(label)
                _typewriter(result.reply)

                if result.sticker_path:
                    print(f"  {_C.LMAGENTA}🎨 [表情包: {result.sticker_path.name}]{_C.RST}")

            except Exception as e:
                logger.error("cli.process_error", error=str(e))
                print(f"\n  {_C.LYELLOW}纳西妲: 嗯……出了点小问题：{str(e)[:100]}{_C.RST}")

        # 主循环退出时安全关闭
        try:
            self._loop.run_until_complete(self.bot.shutdown())
        except Exception as e:
            logger.warning("cli.shutdown_error", error=str(e))

        self._loop.close()


def main():
    cli = CLIInterface()
    cli.run()


if __name__ == "__main__":
    main()
