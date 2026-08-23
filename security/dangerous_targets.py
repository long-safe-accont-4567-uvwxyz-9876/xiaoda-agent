"""危险目标单一事实源 —— 敏感工具名与危险 shell 命令黑名单的唯一出处。

历史上有三处各自维护的黑名单（2026-08 技术债审查确认）：
1. hooks.py::SecurityPreCheck.matcher —— 内联正则，含从未注册的死条目，
   且缺 profile_set/profile_forget；
2. security/permission_manager.py::_SENSITIVE_TOOLS + _GOAT_DANGEROUS_SHELL_PATTERNS；
3. tools/file_tools_v2.py::BLOCKED_COMMANDS + _DANGEROUS_PATTERNS，
   其中 'dd'/'format' 等子串匹配误伤 `git add`、`--format=json`。

本模块以"并集去死条目"合并三方：每个工具名对照 tools/_builtin_manifest.py
与 @register_tool 装饰器逐一核实，未注册的一律剔除（见各组注释）。
消费方只许 import 本模块的常量，禁止再手抄副本；
tests/test_dangerous_targets_consistency.py 守卫这一约束。

2026-08 二轮收编：tool_engine/tool_guardrails.py 的本地 _DANGEROUS_PATTERNS
（rm -rf /、curl|sh、chmod 777、>/dev/sd[a-z] 四条）确认被本模块
FATAL / BLOCKED_PHRASE / INJECTION 三组覆盖后删除，改为引用编译正则。

分组导出（数据本体只此一份，按消费语义分组）：
- SENSITIVE_TOOLS / SENSITIVE_TOOL_MATCHER —— 敏感工具名集合及其派生正则
- READ_TARGET_TOOLS                        —— 证据门禁"读取标记"类工具
- FATAL_SHELL_PATTERNS / FATAL_SHELL_RE    —— 致命命令（GOAT/BYPASS 防傻层也拦）
- BLOCKED_SHELL_WORDS / BLOCKED_SHELL_PHRASES —— shell_command 词级/短语级黑名单
- BLOCKED_WORD_RES / BLOCKED_PHRASE_RES    —— 上两者编译后的正则（消费方直接用）
- INJECTION_SHELL_PATTERNS / INJECTION_SHELL_RE —— 注入/管道/解释器执行模式
"""
from __future__ import annotations

import re

# ══ 1) 敏感工具名 ═══════════════════════════════════════════════
# 合并决策（并集去死条目，逐条核对过注册表）：
#   保留  shell_command / python_executor / write_file          （真实注册）
#   保留  profile_set / profile_forget                           （真实注册；hooks 侧原本缺失，补齐）
#   改写  agnes_image → agnes_image_generate                     （注册名带 _generate 后缀，旧前缀是死条目式写法）
#   改写  agnes_video → agnes_video_generate                     （同上）
#   删除  execute_code                                           （全仓无此注册名，纯死条目）
#   删除  edit_file                                              （同上；仅 risk_classifier 等处遗留引用，不在本清单范围）
#   删除  create_file                                            （同上）
SENSITIVE_TOOLS: frozenset[str] = frozenset({
    "shell_command",         # tools/file_tools_v2.py
    "python_executor",       # tools/code_tools_v2.py
    "write_file",            # tools/file_tools_v2.py
    "agnes_image_generate",  # tools/agnes_tools.py
    "agnes_video_generate",  # tools/agnes_tools.py
    "profile_set",           # tools/profile_tool.py
    "profile_forget",        # tools/profile_tool.py
})

# SecurityPreCheck.matcher 的派生正则：保持原 re.search 子串语义，
# 由集合自动生成，新增/删减工具只改 SENSITIVE_TOOLS 一处。
SENSITIVE_TOOL_MATCHER: str = "|".join(sorted(SENSITIVE_TOOLS))

# 证据门禁"读取标记"类工具（GateGuardHook 标记已读目标）：
# 原 ("read_file", "cat", "list_dir") 中 cat/list_dir 从未注册（真实名字是
# list_files），改为全部核实过的注册名。
READ_TARGET_TOOLS: frozenset[str] = frozenset({
    "read_file",   # tools/file_tools_v2.py
    "list_files",  # tools/file_tools_v2.py
})

# ══ 2) 致命 shell 命令（GOAT/BYPASS 防傻层也不放行） ═════════════
# 自 permission_manager._GOAT_DANGEROUS_SHELL_PATTERNS 原样迁入（正则，
# 不区分大小写），内容零改动——该组模式本身足够精确，无误伤报告。
FATAL_SHELL_PATTERNS: tuple[str, ...] = (
    # ── Linux/macOS ──
    # 根目录删除
    r'rm\s+(-[a-zA-Z]*\s+)*(--recursive\s+)?(/|/\*|\.\s+)',
    r'rm\s+(-[a-zA-Z]*f[a-zA-Z]*r|rfa?|rf)\s+(/|/\*)',
    r'rm\s+-[a-zA-Z]*\s*/\s*$',
    # 磁盘格式化 / 覆写
    r'mkfs\.',
    r'dd\s+if=.*of=/dev/',
    r'>\s*/dev/sd[a-z]',
    # 叉子炸弹
    # （迁移时修复：原 r':\(\)\{.*\|.*&\}' 要求 &} 紧邻，":(){ :|:& };:"
    #   加一个空格即可绕过；现允许 } 前有空白）
    r':\(\)\{.*\|.*&\s*\}',
    r'fork\s*bomb',
    # 关键系统文件破坏
    r'chmod\s+(-[a-zA-Z]*\s+)?(000|777)\s+/',
    r'chown\s+.*\s+/',
    # init / systemd 杀进程
    r'kill\s+-9\s+1\b',
    r'killall\s+(init|systemd|sshd)',
    r'pkill\s+-(9|SIGKILL)\s+(init|systemd|sshd)',
    # 网络破坏
    r'iptables\s+-F',
    r'ip\s+link\s+set\s+.*down',
    # ── Windows ──
    # 磁盘格式化
    r'format\s+[a-zA-Z]:',
    # 递归删除根目录/系统目录
    r'(del|erase)\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\?\s*$',
    r'(rd|smdir)\s+/[sS]\s+/[qQ]\s+[a-zA-Z]:\\?\s*$',
    # 危险系统命令
    r'rd\s+/[sS]\s+/[qQ]\s+(C:\\|C:\\Windows)',
    r'del\s+/[fF]\s+/[sS]\s+/[qQ]\s+C:\\',
    # 关键进程强杀
    r'taskkill\s+/[fF]\s+/[iI][mM]\s+(csrss|smss|wininit|services)\s*\.exe',
    # 启动配置破坏
    r'bcdedit\s+(/delete|/set)',
    # 磁盘分区操作
    r'diskpart',
    # 关机/重启（强制无延迟）
    r'shutdown\s+(/[sSrR]|/g)\s+.*(/[tT]\s*0)',
    # 注册表破坏
    r'reg\s+(delete|import)\s+HKLM\\SYSTEM',
)

FATAL_SHELL_RE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in FATAL_SHELL_PATTERNS
)

# ══ 3) shell_command 工具级黑名单（tools/file_tools_v2） ═════════
#
# 词级：整词边界匹配。左边界禁止 [单词字符 . - = :]（挡住 --flag=、
# .method()、子串），右边界禁止 [单词字符 -]（挡住 format-patch 这类
# 连字扩展）；路径前缀（/usr/bin/dd）不受左边界限制，仍可命中。
# 匹配不区分大小写（旧版词表区分大小写属漏配，收紧）。
#
# 相对旧 BLOCKED_COMMANDS 的语义变化：
#   dd      子串 → 整词：不再误伤 git add / ldd；dd if=… 照拦
#   format  子串 → 整词：不再误伤 --format=json / git format-patch /
#                  --pretty=format:；裸 format 命令照拦
#   chown/chgrp 子串 → 整词：不再误伤 docker build --chown=…；chown 命令照拦
#   mkfs    吸并 mkfs.ext4/ext3/vfat/ntfs 四个变体（整词右边界允许
#           紧跟 '.'，mkfs.<任意> 全覆盖）
#   wipe    新增显式 wipefs（旧版靠 'wipe' 子串顺带命中，整词后需单列）
#   其余词（fdisk/cfdisk/parted/shutdown/reboot/poweroff/halt/shred）
#   语义不变，仅从子串收紧为整词
BLOCKED_SHELL_WORDS: frozenset[str] = frozenset({
    # 磁盘 / 文件系统破坏
    "dd", "mkfs", "fdisk", "cfdisk", "parted", "format",
    "shred", "wipe", "wipefs",
    # 危险权限修改（任意 chown/chgrp 一律拦，与旧版一致）
    "chown", "chgrp",
    # 系统关停
    "shutdown", "reboot", "poweroff", "halt",
})

_BLOCKED_WORD_TEMPLATE = r"(?<![\w.\-=:]){word}(?![\w-])"

# 短语级：多 token 组合，token 间空白弹性匹配（\s+），不区分大小写。
# 相对旧版的变化：
#   'rm -rf'/'rm -fr' 两个字面量并入下方 RM_FLAG_PATTERN（组合旗标的
#   全排列正则，且能拦住旧版漏掉的 'rm -Rf' 等）；'-r -f'/'-f -r'
#   分离写法保留为短语。
#   'chmod 777'/'chmod -R 777'/'chmod 000' 三条泛化为一条"chmod ±选项
#   ± 777/000"，额外覆盖旧版漏掉的 'chmod -f 777' 等变体。
BLOCKED_SHELL_PHRASES: tuple[str, ...] = (
    "rm -r -f", "rm -f -r",
    "init 0", "init 6",
    "nc -e", "ncat -e", "socat exec",
)

# rm 组合旗标家族：-rf/-fr/-Rf/-rfx 等（递归+强制任意排列）
_RM_FLAG_PATTERN = r"\brm\s+(?:-{1,2}[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*|-[a-zA-Z]*f[a-zA-Z]*r)\b"
# chmod 宽松权限（允许夹带短选项）：777 / 000
_CHMOD_LOOSE_PATTERN = r"\bchmod\s+(?:-{1,2}[a-zA-Z]+\s+)?(?:777|000)\b"

EXTRA_BLOCKED_PATTERNS: tuple[str, ...] = (_RM_FLAG_PATTERN, _CHMOD_LOOSE_PATTERN)


def _compile_word(word: str) -> re.Pattern[str]:
    return re.compile(
        _BLOCKED_WORD_TEMPLATE.format(word=re.escape(word)), re.IGNORECASE
    )


def _compile_phrase(phrase: str) -> re.Pattern[str]:
    tokens = (re.escape(t) for t in phrase.split(" "))
    return re.compile(r"\b" + r"\s+".join(tokens) + r"\b", re.IGNORECASE)


BLOCKED_WORD_RES: tuple[re.Pattern[str], ...] = tuple(
    _compile_word(w) for w in sorted(BLOCKED_SHELL_WORDS)
)

BLOCKED_PHRASE_RES: tuple[re.Pattern[str], ...] = tuple(
    _compile_phrase(p) for p in BLOCKED_SHELL_PHRASES
) + tuple(re.compile(p, re.IGNORECASE) for p in EXTRA_BLOCKED_PATTERNS)

# ══ 4) 注入 / 管道 / 解释器执行模式 ══════════════════════════════
# 自 file_tools_v2._DANGEROUS_PATTERNS 原样迁入（正则，消费方以
# IGNORECASE 搜索）。该组模式均为结构化写法（管道、反引号、-c/-m/-e），
# 无误伤报告，逐条保留。
INJECTION_SHELL_PATTERNS: tuple[str, ...] = (
    # rm -rf 任意路径（不只是根目录）
    r'rm\s+(-[a-zA-Z]*r[a-zA-Z]*f[a-zA-Z]*\s+|--recursive\s+--force\s+)\S+',
    # fork bomb 变体
    r':\(\)\{\s*:\|:&\s*\}',
    r'\w+\(\)\{\s*\w+\|:\&\s*\}',
    r'fork\s+bomb',
    # 管道到 shell
    r'\|\s*(ba)?sh\b',
    r'\|\s*(ba)?sh\s+-c\b',
    # 命令替换（仅拦截 bash 风格，PowerShell 的 $() 是表达式不是注入）
    r'bash\s+-c\s+.*\$\([^)]*\)',
    r'`[^`]+`',
    # 反向 shell
    r'(nc|ncat|socat)\s+.*(-e|--exec)\s+',
    r'(nc|ncat)\s+.*(-e|--sh-exec)\s+',
    # curl/wget 管道到 shell
    r'(curl|wget)\s+.*\|\s*(ba)?sh',
    # 解释器执行任意代码（-c / -m / -e）
    r'python(?:3)?\s+(-c|-m)',
    r'perl\s+-e',
    r'ruby\s+-e',
    r'node\s+-e',
    # 解码器管道（解码后内容可能被注入执行）
    r'base64\s+-d',
    r'xxd\s+-r',
    # awk system() 调用
    r'awk\s+.*system\s*\(',
    # 危险重定向覆盖
    r'>\s*/dev/sd[a-z]',
    r'>\s*/dev/nand',
    r'>\s*/dev/mmcblk',
    # 内核模块操作
    r'rmmod\s+',
    r'modprobe\s+-r\s+',
    # 覆写关键系统文件
    r'>\s*/etc/passwd',
    r'>\s*/etc/shadow',
    r'>\s*/etc/sudoers',
)

INJECTION_SHELL_RE: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE) for p in INJECTION_SHELL_PATTERNS
)
