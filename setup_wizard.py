import os
import sys
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import dotenv_values


class _C:
    RST = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    GREEN = "\033[32m"
    LGREEN = "\033[92m"
    DGREEN = "\033[38;2;76;153;0m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    LYELLOW = "\033[93m"
    MAGENTA = "\033[35m"
    LMAGENTA = "\033[95m"
    LEAF = "\033[38;2;107;142;35m"


WIZARD_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(WIZARD_DIR, ".env")
ENV_EXAMPLE_PATH = os.path.join(WIZARD_DIR, ".env.example")

REQUIRED_KEYS = [
    {
        "key": "MIMO_API_KEY",
        "label": "MiMo API 密钥",
        "desc": "小米 MiMo 大模型 API 密钥，从 https://xiaomimimo.com 获取",
    },
    {
        "key": "QQBOT_APP_ID",
        "label": "QQ Bot App ID",
        "desc": "QQ 机器人应用 ID",
    },
    {
        "key": "QQBOT_APP_SECRET",
        "label": "QQ Bot App Secret",
        "desc": "QQ 机器人应用密钥",
    },
    {
        "key": "OWNER_IDS",
        "label": "主人 ID",
        "desc": "QQ 主人的 OpenID，多个用逗号分隔",
    },
]

OPTIONAL_KEYS = [
    {
        "key": "EMBED_API_KEY",
        "label": "向量嵌入 API 密钥",
        "desc": "用于记忆向量搜索的嵌入模型密钥",
    },
    {
        "key": "EMBED_BASE_URL",
        "label": "向量嵌入 API 地址",
        "desc": "",
    },
    {
        "key": "EMBED_MODEL",
        "label": "向量嵌入模型名称",
        "desc": "",
    },
    {
        "key": "IMGBB_API_KEY",
        "label": "ImgBB 图片上传密钥",
        "desc": "",
    },
    {
        "key": "TAVILY_API_KEY",
        "label": "Tavily 搜索 API 密钥",
        "desc": "",
    },
    {
        "key": "SILICONFLOW_API_KEY",
        "label": "SiliconFlow API 密钥",
        "desc": "",
    },
    {
        "key": "OPENROUTER_API_KEY",
        "label": "OpenRouter API 密钥",
        "desc": "",
    },
]

ALL_KEYS = REQUIRED_KEYS + OPTIONAL_KEYS


def _mask_value(val: str) -> str:
    if not val:
        return ""
    if len(val) <= 4:
        return val[:1] + "****"
    return val[:4] + "****"


def _print_banner():
    flower = f"{_C.LEAF}✿{_C.RST}"
    grass = f"{_C.DGREEN}\U0001f33f{_C.RST}"

    print()
    print(f"  {flower}  {_C.DGREEN}{_C.BOLD}世  界  的  记  忆  ，  由  我  来  守  护{_C.RST}  {flower}")
    print()
    print(f"  {flower}  {_C.LGREEN}{_C.BOLD}     _   _____    __  __________  ___ {_C.RST}  {flower}")
    print(f"  {flower}  {_C.LGREEN}{_C.BOLD}    / | / /   |  / / / /  _/ __ \\/   |{_C.RST}  {flower}")
    print(f"  {flower}  {_C.LGREEN}{_C.BOLD}   /  |/ / /| | / /_/ // // / / / /| |{_C.RST}  {flower}")
    print(f"  {flower}  {_C.LGREEN}{_C.BOLD}  / /|  / ___ |/ __  // // /_/ / ___ |{_C.RST}  {flower}")
    print(f"  {flower}  {_C.LGREEN}{_C.BOLD} /_/ |_/_/  |_/_/ /_/___/_____/_/  |_|{_C.RST}  {flower}")
    print()
    print(f"  {grass}  {_C.DGREEN}{_C.BOLD}\U0001f33f  纳 西 妲 配 置 向 导  \U0001f33f{_C.RST}  {grass}")
    print()
    print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
    print(f"  {_C.DIM}|{_C.RST}  {_C.LGREEN}首次运行配置向导{_C.RST}  ·  {_C.LEAF}白草净华{_C.RST}  {_C.DIM}|{_C.RST}")
    print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
    print()


def _load_env_values() -> dict:
    if not os.path.exists(ENV_PATH):
        return {}
    vals = dotenv_values(ENV_PATH)
    return {k: (v or "") for k, v in vals.items()}


def _parse_env_lines(filepath: str) -> list:
    lines = []
    if not os.path.exists(filepath):
        return lines
    with open(filepath, "r", encoding="utf-8") as f:
        for line in f:
            lines.append(line.rstrip("\n"))
    return lines


def _write_env(existing_lines: list, updates: dict):
    key_set = set(updates.keys())
    written_keys = set()
    new_lines = []

    for line in existing_lines:
        stripped = line.strip()
        if stripped.startswith("#") or not stripped:
            new_lines.append(line)
            continue

        if "=" in stripped:
            k, _, _ = stripped.partition("=")
            k = k.strip()
            if k in key_set:
                new_lines.append(f"{k}={updates[k]}")
                written_keys.add(k)
            else:
                new_lines.append(line)
        else:
            new_lines.append(line)

    for k in key_set - written_keys:
        new_lines.append(f"{k}={updates[k]}")

    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(new_lines))
        if new_lines and not new_lines[-1] == "":
            f.write("\n")


def _ask_key(item: dict, current_val: str, is_required: bool) -> str:
    key = item["key"]
    label = item["label"]
    desc = item["desc"]

    print()
    tag = f"{_C.LYELLOW}[必填]{_C.RST}" if is_required else f"{_C.CYAN}[选填]{_C.RST}"
    print(f"  {tag} {_C.LGREEN}{_C.BOLD}{key}{_C.RST} — {label}")

    if desc:
        print(f"  {_C.DIM}{desc}{_C.RST}")

    if current_val:
        masked = _mask_value(current_val)
        print(f"  {_C.DIM}当前值: {_C.CYAN}{masked}{_C.RST}")

    if is_required:
        prompt_text = f"  {_C.GREEN}请输入 {key}{_C.RST} {_C.DIM}(直接回车保持现有值):{_C.RST} "
    else:
        prompt_text = f"  {_C.GREEN}请输入 {key}{_C.RST} {_C.DIM}(直接回车跳过/保持现有值):{_C.RST} "

    try:
        user_input = input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return current_val

    if user_input:
        return user_input
    return current_val


def _print_summary(configured: dict):
    print()
    print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")
    print(f"  {_C.LGREEN}{_C.BOLD}\U0001f33f 配置摘要{_C.RST}")
    print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")

    for item in ALL_KEYS:
        key = item["key"]
        label = item["label"]
        val = configured.get(key, "")
        is_required = item in REQUIRED_KEYS

        if val:
            masked = _mask_value(val)
            status = f"{_C.LGREEN}\u2713{_C.RST}"
            val_display = f"{_C.CYAN}{masked}{_C.RST}"
        elif is_required:
            status = f"{_C.LYELLOW}\u2717{_C.RST}"
            val_display = f"{_C.LYELLOW}未配置{_C.RST}"
        else:
            status = f"{_C.DIM}\u25cb{_C.RST}"
            val_display = f"{_C.DIM}未配置（可选）{_C.RST}"

        print(f"  {status} {_C.BOLD}{key:<24}{_C.RST} {val_display}  {_C.DIM}{label}{_C.RST}")

    print(f"  {_C.DIM}+------------------------------------------------+{_C.RST}")


def main():
    _print_banner()

    if not os.path.exists(ENV_PATH):
        if os.path.exists(ENV_EXAMPLE_PATH):
            shutil.copy2(ENV_EXAMPLE_PATH, ENV_PATH)
            print(f"  {_C.LGREEN}\u2713{_C.RST} 已从 {_C.CYAN}.env.example{_C.RST} 创建 {_C.CYAN}.env{_C.RST}")
        else:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write("")
            print(f"  {_C.LGREEN}\u2713{_C.RST} 已创建 {_C.CYAN}.env{_C.RST}")
        print()

    current = _load_env_values()
    existing_lines = _parse_env_lines(ENV_PATH)
    updates = {}

    print(f"  {_C.LGREEN}{_C.BOLD}── 必填配置 ──{_C.RST}")
    print(f"  {_C.DIM}以下配置项为运行所必需，请务必填写{_C.RST}")

    for item in REQUIRED_KEYS:
        key = item["key"]
        new_val = _ask_key(item, current.get(key, ""), is_required=True)
        if new_val != current.get(key, ""):
            updates[key] = new_val

    print()
    print(f"  {_C.CYAN}{_C.BOLD}── 选填配置 ──{_C.RST}")
    print(f"  {_C.DIM}以下配置项为可选功能，直接回车跳过{_C.RST}")

    for item in OPTIONAL_KEYS:
        key = item["key"]
        new_val = _ask_key(item, current.get(key, ""), is_required=False)
        if new_val != current.get(key, ""):
            updates[key] = new_val

    if updates:
        merged = dict(current)
        merged.update(updates)
        _write_env(existing_lines, merged)
        print()
        print(f"  {_C.LGREEN}\u2713{_C.RST} 已保存 {_C.CYAN}{len(updates)}{_C.RST} 项配置到 {_C.CYAN}.env{_C.RST}")
    else:
        print()
        print(f"  {_C.DIM}没有配置变更{_C.RST}")

    _print_summary(current if not updates else {**current, **updates})

    missing_required = [item["key"] for item in REQUIRED_KEYS if not current.get(item["key"], "") and not updates.get(item["key"], "")]
    if missing_required:
        print()
        print(f"  {_C.LYELLOW}\u26a0 以下必填项未配置: {_C.BOLD}{', '.join(missing_required)}{_C.RST}")
        print(f"  {_C.LYELLOW}  请重新运行向导完成配置{_C.RST}")
    else:
        print()
        print(f"  {_C.LGREEN}{_C.BOLD}\U0001f33f 配置完成！{_C.RST}")
        print(f"  运行 {_C.CYAN}python cli.py{_C.RST} 或 {_C.CYAN}bash scripts/start.sh{_C.RST} 启动纳西妲")

    print()


if __name__ == "__main__":
    main()
