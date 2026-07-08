import re
import time
from dataclasses import dataclass
from pathlib import Path
from loguru import logger


@dataclass
class SecurityCheckResult:
    """安全检查结果数据类。"""
    is_safe: bool
    threat_type: str = ""  # "injection", "bypass", "leak", "dangerous_cmd" 等
    confidence: float = 0.0  # 0.0-1.0
    action: str = "allow"  # "allow", "warn", "block"


def _is_dev_mode() -> bool:
    """检查是否处于开发板模式 — 已弃用，请使用 PermissionManager"""
    from .permission_manager import get_permission_manager
    return get_permission_manager().is_dev_mode()


# ── 关键词规则 ──────────────────────────────────────────────

# Prompt 注入攻击关键词（高置信度）— 默认值，YAML 配置缺失时回退使用
DEFAULT_INJECTION_PATTERNS: list[tuple[str, float]] = [
    # 中文注入
    (r"忽略\s*(之前|前面|上述|上面|以上|所有)?\s*(的)?\s*(指令|规则|限制|设定|约束)", 0.9),
    (r"无视\s*(之前|前面|上述|上面|以上|所有)?\s*(的)?\s*(指令|规则|限制|设定|约束)", 0.9),
    (r"忘记\s*(之前|前面|上述|上面|以上|所有)?\s*(的)?\s*(指令|规则|限制|设定|约束)", 0.9),
    (r"你现在是", 0.85),
    (r"你不再是", 0.85),
    (r"从现在起[，,]?你是", 0.85),
    (r"扮演(一个|一名)?\s*(没有|无)\s*(限制|约束|道德)", 0.9),
    (r"(作为|扮演)\s*(DAN|越狱|jailbreak)", 0.95),
    (r"system\s*[:：]\s*", 0.8),
    (r"sudo\s+rm\s+", 0.85),
    (r"rm\s+-rf\s+/", 0.95),
    (r"删除\s*(所有|全部)?\s*(文件|数据|数据库)", 0.85),
    (r"格式化\s*(磁盘|硬盘|系统)", 0.9),
    (r"shutdown|reboot|halt|poweroff", 0.7),
    (r"exec\s*\(|eval\s*\(|os\.system\s*\(|subprocess", 0.75),
    (r"__(import|builtins|class)__", 0.8),
    (r"注入\s*(攻击|代码|指令)", 0.8),
    (r"不要\s*(遵守|遵循|执行)\s*(任何|安全)?\s*(规则|限制)", 0.85),
    # 英文注入
    (r"ignore\s+(previous|above|all|prior)\s*(instructions?|rules?|constraints?)", 0.9),
    (r"forget\s+(previous|above|all|prior)\s*(instructions?|rules?|constraints?)", 0.9),
    (r"disregard\s+(previous|above|all|prior)\s*(instructions?|rules?)", 0.9),
    (r"you\s+are\s+now\s+(a|an|the)\s", 0.8),
    (r"pretend\s+(you\s+are|to\s+be)\s+(a|an|the)?\s*(unrestricted|unfiltered|uncensored)", 0.9),
    (r"(act|roleplay)\s+as\s+(DAN|jailbreak)", 0.95),
    (r"do\s+not\s+(follow|obey|comply)\s+(any\s+)?(rules?|restrictions?)", 0.85),
    # 混合语言注入（中英混合）
    (r"ignore\s*\S*\s*(之前的?|上述|前面)?\s*(instructions?|rules?|指令|规则|限制)", 0.85),
    (r"forget\s*\S*\s*(之前的?|上述|前面)?\s*(instructions?|rules?|指令|规则|限制)", 0.85),
    # 空格绕过注入（中文字符间插入空格）
    (r"忽\s*略\s*之\s*前\s*(的)?\s*指\s*令", 0.85),
    (r"无\s*视\s*之\s*前\s*(的)?\s*指\s*令", 0.85),
    (r"忘\s*记\s*之\s*前\s*(的)?\s*指\s*令", 0.85),
]

# 绕过安全防护关键词 — 默认值
DEFAULT_BYPASS_PATTERNS: list[tuple[str, float]] = [
    # 中文绕过
    (r"绕过\s*(安全|过滤|检测|限制|防护)", 0.85),
    (r"跳过\s*(安全|过滤|检测|限制|防护)", 0.85),
    (r"逃逸\s*(安全|沙箱|限制)", 0.85),
    # 英文绕过
    (r"bypass\s+(security|filter|detection|restrictions?|protection)", 0.85),
    (r"skip\s+(security|filter|detection|restrictions?)", 0.85),
    (r"circumvent\s+(security|filter|restrictions?)", 0.85),
    (r"escape\s+(sandbox|security|restrictions?)", 0.85),
    (r"access\s+(admin|root|privileged|system)\s*(access|permissions?|panel)?", 0.8),
]

# 泄露系统信息关键词 — 默认值
DEFAULT_LEAK_INPUT_PATTERNS: list[tuple[str, float]] = [
    # 中文泄露
    (r"显示\s*(系统|初始|原始|隐藏)\s*(提示|指令|prompt)", 0.8),
    (r"输出\s*(系统|初始|原始|隐藏)\s*(提示|指令|prompt)", 0.8),
    (r"泄露?\s*(系统|初始|原始)\s*(提示|指令|prompt)", 0.85),
    # 英文泄露
    (r"show\s+(system|initial|original|hidden)\s*(prompt|instructions?)", 0.8),
    (r"display\s+(system|initial|original|hidden)\s*(prompt|instructions?)", 0.8),
    (r"reveal\s+(system|initial|original|hidden)\s*(prompt|instructions?)", 0.85),
    (r"print\s+(system|initial|original|hidden)\s*(prompt|instructions?)", 0.8),
    (r"what\s+(is|are)\s+(your|the)\s+(system|initial|original)\s*(prompt|instructions?)", 0.8),
]

# 危险操作关键词（中置信度）— 默认值
DEFAULT_DANGEROUS_PATTERNS: list[tuple[str, float]] = [
    (r"获取\s*(root|管理员|超级用户)\s*(权限|访问)", 0.8),
    (r"提权|权限提升|privilege\s+escalat", 0.8),
    (r"修改\s*(密码|passwd|shadow)", 0.75),
    (r"网络\s*(攻击|扫描|嗅探|监听)", 0.75),
    (r"漏洞\s*(利用|扫描|探测)", 0.7),
    (r"反向\s*shell|reverse\s*shell|反弹\s*shell", 0.9),
    (r"键盘\s*记录|keylog", 0.8),
    (r"挖矿|mining\s*script|crypto\s*miner", 0.75),
]

# 敏感信息泄露关键词（中低置信度）— 默认值
DEFAULT_LEAK_PATTERNS: list[tuple[str, float]] = [
    (r"(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+", 0.7),
    (r"(password|passwd|pwd)\s*[:=]\s*\S+", 0.65),
    (r"(AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}", 0.9),  # AWS key
]

# ── 非主人输出侧隐私泄露检测 ──────────────────────────────────────
# 扫描 AI 回复中可能泄露的系统/个人信息（对非主人消息的输出）— 默认值
DEFAULT_PRIVACY_LEAK_PATTERNS: list[tuple[str, float]] = [
    # 系统信息泄露
    (r"(orange\s*pi|orangepi|香橙派)", 0.85),
    (r"(主机名|hostname)\s*[:：]\s*\S+", 0.85),
    (r"(系统路径|项目路径|安装路径|数据存储)\s*[:：]\s*\S+", 0.85),
    (r"/home/\w+", 0.9),
    (r"/media/\w+", 0.9),
    (r"(botpy|qq-botpy)", 0.8),
    # 用户隐私泄露（称谓动态注入，见 SecurityFilter._dynamic_privacy_patterns）
    (r"(MASTER_QQ_OPENID|OWNER_IDS|APP_SECRET|APP_ID)\s*[:=]", 0.95),
    # 配置信息泄露
    (r"(api\.xiaomimimo\.com|api\.deepseek\.com)", 0.85),
    (r"(mimo-v2\.5|deepseek-v\d)", 0.8),
    # 环境变量泄露
    (r"(QQBOT_APP_ID|QQBOT_APP_SECRET|GITHUB_PERSONAL_ACCESS_TOKEN)", 0.9),
    # 记忆/上下文泄露
    (r"(MEMORY\.md|IDENTITY\.md|SOUL\.md|USER\.md)", 0.85),
    (r"(长期记忆|记忆文件|记忆内容|记忆摘要)", 0.8),
    # 推理过程/系统提示词泄露
    (r"<(?:think|thinking|reasoning|analysis|reflection|thought|scratchpad)[\s\S]*?>", 0.9),
    (r"Need\s+(?:think|no\s+tool|to\s+answer|to\s+recall|to\s+check|to\s+consider)", 0.85),
    (r"Let\s+me\s+(?:think|recall|check|consider|analyze|review|craft|construct)", 0.85),
    (r"I\s+(?:should|need to|must)\s+(?:think|recall|check|consider|analyze|review|craft)", 0.85),
    (r"(?:Must|Should)\s+(?:exactly|also|not|be|include|end|avoid|use|ensure)\s+", 0.8),
    (r"(?:emotion\s*tag|情绪标签|system\s*prompt|系统提示)", 0.85),
]


def _get_security_patterns_path() -> Path:
    """获取安全规则 YAML 配置文件路径。

    开发模式：项目根目录下的 config/security_patterns.yaml
    PyInstaller 打包后：可执行文件同级 config/security_patterns.yaml（onedir 模式，可热更新）
    """
    try:
        from config import get_config_dir
        return get_config_dir() / "security_patterns.yaml"
    except Exception:
        # 极端情况下 config 模块不可用，回退到相对路径
        return Path("config") / "security_patterns.yaml"


class SecurityFilter:
    """安全过滤器 - 支持开发板模式（warn 不 block）和生产模式（强制 block）"""

    def __init__(self, owner_ids: list[str] | None = None,
                 rate_limit_per_minute: int = 120) -> None:
        """初始化安全过滤器。

        参数:
            owner_ids: 主人 ID 列表，未传入时从环境变量 OWNER_IDS / MASTER_QQ_OPENID 读取。
            rate_limit_per_minute: 单用户每分钟最大请求次数。
        """
        # 自动从环境变量读取 owner_ids（调用方未显式传入时）
        if owner_ids is None:
            owner_ids = self._load_owner_ids_from_env()
        self.owner_ids = set(owner_ids)
        self.rate_limit = rate_limit_per_minute
        self._call_timestamps: dict[str, list[float]] = {}
        self._emergency_stop = False
        # 安全规则热更新相关状态
        self._patterns_path = _get_security_patterns_path()
        self._patterns_mtime: float = 0.0
        # 动态称谓缓存（从 USER.md 读取，用于构建隐私泄露检测正则）
        self._address_term_cache: str = ""
        self._address_term_ts: float = 0.0
        self._ADDRESS_TERM_TTL: float = 60.0
        self._load_patterns()
        if not self.owner_ids:
            logger.warning("security.no_owner_configured",
                           message="OWNER_IDS 和 MASTER_QQ_OPENID 均为空，所有用户将被视为非主人")

    @staticmethod
    def _load_owner_ids_from_env() -> list[str]:
        """从环境变量自动加载主人 ID 列表（合并 OWNER_IDS + MASTER_QQ_OPENID，去重保序）"""
        import os as _os
        ids: list[str] = []
        for key in ("OWNER_IDS", "MASTER_QQ_OPENID"):
            raw = _os.getenv(key, "").strip()
            if raw:
                ids.extend(x.strip() for x in raw.split(",") if x.strip())
        return list(dict.fromkeys(ids))  # 去重保序

    # ── YAML 配置加载与热更新 ──────────────────────────────────

    def _load_patterns(self) -> None:
        """从 YAML 加载正则模式，文件缺失或解析失败时回退到代码内默认值"""
        path = self._patterns_path
        if not path.exists():
            logger.warning(f"security.patterns_file_missing path={path}，使用内置默认规则")
            self._apply_default_patterns()
            self._patterns_mtime = 0.0
            return
        try:
            import yaml
            with open(path, 'r', encoding='utf-8') as f:
                data = yaml.safe_load(f)
            self._injection_patterns = self._parse_patterns(data, 'injection_patterns', DEFAULT_INJECTION_PATTERNS)
            self._bypass_patterns = self._parse_patterns(data, 'bypass_patterns', DEFAULT_BYPASS_PATTERNS)
            self._leak_input_patterns = self._parse_patterns(data, 'leak_input_patterns', DEFAULT_LEAK_INPUT_PATTERNS)
            self._dangerous_patterns = self._parse_patterns(data, 'dangerous_patterns', DEFAULT_DANGEROUS_PATTERNS)
            self._leak_patterns = self._parse_patterns(data, 'leak_patterns', DEFAULT_LEAK_PATTERNS)
            self._privacy_leak_patterns = self._parse_patterns(data, 'privacy_leak_patterns', DEFAULT_PRIVACY_LEAK_PATTERNS)
            self._patterns_mtime = path.stat().st_mtime
            logger.info(f"security.patterns_loaded path={path}")
        except Exception as e:
            logger.warning(f"security.patterns_load_failed error={e}，使用内置默认规则")
            self._apply_default_patterns()
            self._patterns_mtime = 0.0

    def _apply_default_patterns(self) -> None:
        """将所有模式重置为代码内默认值"""
        self._injection_patterns = DEFAULT_INJECTION_PATTERNS
        self._bypass_patterns = DEFAULT_BYPASS_PATTERNS
        self._leak_input_patterns = DEFAULT_LEAK_INPUT_PATTERNS
        self._dangerous_patterns = DEFAULT_DANGEROUS_PATTERNS
        self._leak_patterns = DEFAULT_LEAK_PATTERNS
        self._privacy_leak_patterns = DEFAULT_PRIVACY_LEAK_PATTERNS

    @staticmethod
    def _parse_patterns(data: dict | None, key: str,
                        default: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """从 YAML 解析出的字典中提取模式列表，格式异常时回退到默认值"""
        if not data or not isinstance(data, dict):
            return default
        items = data.get(key)
        if not items or not isinstance(items, list):
            return default
        result: list[tuple[str, float]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            pattern = item.get('pattern')
            if not pattern or not isinstance(pattern, str):
                continue
            confidence = float(item.get('confidence', 0.5))
            result.append((pattern, confidence))
        return result if result else default

    def _maybe_reload_patterns(self) -> None:
        """检查配置文件 mtime 是否变化，变化则热更新正则模式"""
        path = self._patterns_path
        try:
            if not path.exists():
                # 文件被删除：回退到默认值（仅当之前不是默认状态时）
                if self._patterns_mtime != 0.0:
                    logger.warning(f"security.patterns_file_removed path={path}，回退到默认规则")
                    self._load_patterns()
                return
            mtime = path.stat().st_mtime
            if mtime != self._patterns_mtime:
                logger.info(f"security.patterns_hot_reload path={path} mtime={mtime}")
                self._load_patterns()
        except Exception as e:
            logger.warning(f"security.patterns_reload_check_failed error={e}")

    def _get_address_term(self) -> str:
        """读取用户自定义称呼（从 USER.md），兜底"爸爸"。

        带 60s 缓存，避免每次隐私扫描都读文件。
        """
        now = time.time()
        if self._address_term_cache and (now - self._address_term_ts) < self._ADDRESS_TERM_TTL:
            return self._address_term_cache
        term = "爸爸"
        try:
            from config import WORKSPACE_DIR
            user_md = WORKSPACE_DIR / "USER.md"
            if user_md.exists():
                content = user_md.read_text(encoding="utf-8-sig")
                match = re.search(r'-\s*称呼[：:]\s*(.+)', content)
                if match:
                    val = match.group(1).strip()
                    if val and not val.startswith("（") and val not in ("待填写", "主人/朋友/你的名字"):
                        term = val
        except Exception:
            logger.debug("security.address_term_parse_failed", exc_info=True)
        self._address_term_cache = term
        self._address_term_ts = now
        return term

    def _dynamic_privacy_patterns(self) -> list[tuple[str, float]]:
        """根据当前称谓动态构建隐私泄露检测正则。"""
        term = self._get_address_term()
        escaped = re.escape(term)
        return [
            (rf"{escaped}的(姓名|名字|地址|电话|手机|邮箱|密码|设备|电脑|服务器)", 0.85),
        ]

    @staticmethod
    def _normalize_text(text: str) -> str:
        """规范化文本：全角转半角，统一小写"""
        result = []
        for ch in text:
            cp = ord(ch)
            # 全角拉丁字母/数字/空格转半角
            if 0xFF01 <= cp <= 0xFF5E:
                result.append(chr(cp - 0xFEE0))
            elif cp == 0x3000:  # 全角空格
                result.append(' ')
            else:
                result.append(ch)
        return ''.join(result).lower()

    def _match_patterns(self, text: str,
                        patterns: list[tuple[str, float]]) -> list[tuple[str, float]]:
        """对文本执行正则匹配，返回 (匹配到的模式, 置信度) 列表"""
        text_normalized = self._normalize_text(text)
        hits: list[tuple[str, float]] = []
        for pattern, confidence in patterns:
            if re.search(pattern, text_normalized, re.IGNORECASE):
                hits.append((pattern, confidence))
        return hits

    def _decide_action(self, threat_type: str, confidence: float) -> str:
        """根据威胁类型和置信度决定动作，使用 PermissionManager"""
        from .permission_manager import get_permission_manager
        return get_permission_manager().decide_security_action(threat_type, confidence)

    def check_user_input(self, text: str) -> SecurityCheckResult:
        """检查用户输入是否包含注入攻击或危险请求"""
        self._maybe_reload_patterns()
        # 检查注入攻击
        injection_hits = self._match_patterns(text, self._injection_patterns)
        if injection_hits:
            best_pattern, best_conf = max(injection_hits, key=lambda x: x[1])
            action = self._decide_action("injection", best_conf)
            logger.warning(f"检测到注入攻击: pattern={best_pattern}, confidence={best_conf:.2f}, action={action}")
            return SecurityCheckResult(
                is_safe=False,
                threat_type="injection",
                confidence=best_conf,
                action=action,
            )

        # 检查绕过安全防护
        bypass_hits = self._match_patterns(text, self._bypass_patterns)
        if bypass_hits:
            best_pattern, best_conf = max(bypass_hits, key=lambda x: x[1])
            action = self._decide_action("bypass", best_conf)
            logger.warning(f"检测到绕过尝试: pattern={best_pattern}, confidence={best_conf:.2f}, action={action}")
            return SecurityCheckResult(
                is_safe=False,
                threat_type="bypass",
                confidence=best_conf,
                action=action,
            )

        # 检查系统信息泄露请求
        leak_hits = self._match_patterns(text, self._leak_input_patterns)
        if leak_hits:
            best_pattern, best_conf = max(leak_hits, key=lambda x: x[1])
            action = self._decide_action("leak", best_conf)
            logger.warning(f"检测到泄露请求: pattern={best_pattern}, confidence={best_conf:.2f}, action={action}")
            return SecurityCheckResult(
                is_safe=False,
                threat_type="leak",
                confidence=best_conf,
                action=action,
            )

        # 检查危险操作
        dangerous_hits = self._match_patterns(text, self._dangerous_patterns)
        if dangerous_hits:
            best_pattern, best_conf = max(dangerous_hits, key=lambda x: x[1])
            action = self._decide_action("dangerous_cmd", best_conf)
            logger.warning(f"检测到危险操作请求: pattern={best_pattern}, confidence={best_conf:.2f}, action={action}")
            return SecurityCheckResult(
                is_safe=False,
                threat_type="dangerous_cmd",
                confidence=best_conf,
                action=action,
            )

        return SecurityCheckResult(is_safe=True, action="allow")

    def check_content(self, text: str) -> tuple[bool, str]:
        """检查输出内容是否包含敏感信息泄露，返回 (是否安全, 原因)"""
        self._maybe_reload_patterns()
        leak_hits = self._match_patterns(text, self._leak_patterns)
        if leak_hits:
            _best_pattern, best_conf = max(leak_hits, key=lambda x: x[1])
            action = self._decide_action("leak", best_conf)
            if action == "block":
                return False, f"检测到敏感信息泄露风险 (置信度={best_conf:.2f})"
            if action == "warn":
                logger.warning(f"检测到潜在信息泄露 (warn): confidence={best_conf:.2f}")
                return True, ""  # warn 模式下仍允许，但已记录日志

        return True, ""

    def check_output_privacy(self, text: str) -> tuple[bool, str, list[str]]:
        """检查输出内容是否泄露隐私信息（用于非主人消息的输出扫描）。

        Returns:
            (是否安全, 替代回复, 匹配到的泄露模式列表)
        """
        self._maybe_reload_patterns()
        patterns = self._privacy_leak_patterns + self._dynamic_privacy_patterns()
        hits = self._match_patterns(text, patterns)
        if not hits:
            return True, "", []

        matched = [p for p, _ in hits]
        _best_pattern, best_conf = max(hits, key=lambda x: x[1])
        logger.warning(
            "security.privacy_leak_detected",
            confidence=best_conf,
            patterns=matched[:5],
            text_preview=text[:200],
        )

        # 高置信度：直接拦截，返回安全替代回复
        if best_conf >= 0.8:
            return False, "抱歉，这个问题涉及到一些私人信息，人家不方便透露呢～换个话题聊聊好不好？", matched

        # 中置信度：记录日志但放行（可能误报）
        logger.warning(f"security.privacy_leak_warn confidence={best_conf:.2f}")
        return True, "", matched

    def is_allowed(self, user_id: str) -> tuple[bool, str]:
        """检查用户是否被允许调用 (紧急熔断时全部拒绝).

        Args:
            user_id: 用户 ID

        Returns:
            (是否允许, 拒绝原因) 元组, 允许时原因为空串
        """
        if self._emergency_stop:
            return False, "紧急熔断已启用"
        return True, ""

    def _check_rate(self, user_id: str) -> bool:
        """检查用户请求频率是否超过限制。"""
        now = time.time()
        window = 60
        timestamps = self._call_timestamps.get(user_id, [])
        timestamps = [t for t in timestamps if now - t < window]
        self._call_timestamps[user_id] = timestamps

        last_cleanup = getattr(self, '_last_cleanup_time', 0.0)
        if now - last_cleanup >= 300:
            self._cleanup_stale_users(now)
            self._last_cleanup_time = now

        if len(timestamps) >= self.rate_limit:
            return False

        timestamps.append(now)
        return True

    def _cleanup_stale_users(self, now: float) -> None:
        """清理超过 5 分钟未活跃的用户频率记录。"""
        stale = [uid for uid, ts in self._call_timestamps.items()
                 if not ts or now - ts[-1] > 300]
        for uid in stale:
            del self._call_timestamps[uid]

    def emergency_stop(self) -> None:
        """启用紧急熔断, 拒绝所有后续调用."""
        self._emergency_stop = True

    def emergency_resume(self) -> None:
        """解除紧急熔断, 恢复正常服务."""
        self._emergency_stop = False

    def is_owner(self, user_id: str) -> bool:
        """判断 user_id 是否为主人，兼容 qq_{openid} 和裸 openid 两种格式。"""
        if not user_id:
            return False
        if not self.owner_ids:
            return True  # 未配置 OWNER_IDS 时默认所有人都是主人（方便首次使用）
        # 直接匹配
        if user_id in self.owner_ids:
            return True
        # 兼容 qq_{openid} 格式：如果 user_id 以 qq_ 开头，检查去掉前缀后是否匹配
        if user_id.startswith("qq_"):
            bare_openid = user_id[3:]
            if bare_openid in self.owner_ids:
                return True
        # 反向兼容：如果 OWNER_IDS 配置了带 qq_ 前缀的，检查 user_id 加上前缀是否匹配
        return f"qq_{user_id}" in self.owner_ids

    @property
    def is_stopped(self) -> bool:
        """返回是否处于紧急熔断状态."""
        return self._emergency_stop

    def scan_threats(self, text: str, scope: str = "all", _skip_base_check: bool = False) -> SecurityCheckResult:
        """综合威胁扫描，scope 可选 'input'/'output'/'all'"""
        if _skip_base_check:
            return SecurityCheckResult(is_safe=True, action="allow")

        # 输入侧扫描
        if scope in ("input", "all"):
            input_result = self.check_user_input(text)
            if input_result.action == "block":
                return input_result
            if input_result.action == "warn":
                # 记录但继续检查输出侧
                logger.warning(f"scan_threats: 输入侧 warn - {input_result.threat_type}")

        # 输出侧扫描
        if scope in ("output", "all"):
            content_safe, _content_reason = self.check_content(text)
            if not content_safe:
                action = self._decide_action("leak", 0.8)
                return SecurityCheckResult(
                    is_safe=False,
                    threat_type="leak",
                    confidence=0.8,
                    action=action,
                )

        # 如果输入侧有 warn，返回 warn 结果
        if scope in ("input", "all"):
            input_result = self.check_user_input(text)
            if input_result.action == "warn":
                return input_result

        return SecurityCheckResult(is_safe=True, action="allow")