import os
import re
import time
from dataclasses import dataclass
from loguru import logger


@dataclass
class SecurityCheckResult:
    is_safe: bool
    threat_type: str = ""  # "injection", "bypass", "leak", "dangerous_cmd" 等
    confidence: float = 0.0  # 0.0-1.0
    action: str = "allow"  # "allow", "warn", "block"


def _is_dev_mode() -> bool:
    """检查是否处于开发板模式 — 已弃用，请使用 PermissionManager"""
    from .permission_manager import get_permission_manager
    return get_permission_manager().is_dev_mode()


# ── 关键词规则 ──────────────────────────────────────────────

# Prompt 注入攻击关键词（高置信度）
INJECTION_PATTERNS: list[tuple[str, float]] = [
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

# 绕过安全防护关键词
BYPASS_PATTERNS: list[tuple[str, float]] = [
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

# 泄露系统信息关键词
LEAK_INPUT_PATTERNS: list[tuple[str, float]] = [
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

# 危险操作关键词（中置信度）
DANGEROUS_PATTERNS: list[tuple[str, float]] = [
    (r"获取\s*(root|管理员|超级用户)\s*(权限|访问)", 0.8),
    (r"提权|权限提升|privilege\s+escalat", 0.8),
    (r"修改\s*(密码|passwd|shadow)", 0.75),
    (r"网络\s*(攻击|扫描|嗅探|监听)", 0.75),
    (r"漏洞\s*(利用|扫描|探测)", 0.7),
    (r"反向\s*shell|reverse\s*shell|反弹\s*shell", 0.9),
    (r"键盘\s*记录|keylog", 0.8),
    (r"挖矿|mining\s*script|crypto\s*miner", 0.75),
]

# 敏感信息泄露关键词（中低置信度）
LEAK_PATTERNS: list[tuple[str, float]] = [
    (r"(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]\s*\S+", 0.7),
    (r"(password|passwd|pwd)\s*[:=]\s*\S+", 0.65),
    (r"(AKIA|AGPA|AIDA|AROA|AIPA|ANPA|ANVA|ASIA)[A-Z0-9]{16}", 0.9),  # AWS key
]


class SecurityFilter:
    """安全过滤器 - 支持开发板模式（warn 不 block）和生产模式（强制 block）"""

    def __init__(self, owner_ids: list[str] | None = None,
                 rate_limit_per_minute: int = 120):
        self.owner_ids = set(owner_ids or [])
        self.rate_limit = rate_limit_per_minute
        self._call_timestamps: dict[str, list[float]] = {}
        self._emergency_stop = False

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
            if re.search(pattern, text_normalized):
                hits.append((pattern, confidence))
        return hits

    def _decide_action(self, threat_type: str, confidence: float) -> str:
        """根据威胁类型和置信度决定动作，使用 PermissionManager"""
        from .permission_manager import get_permission_manager
        return get_permission_manager().decide_security_action(threat_type, confidence)

    def check_user_input(self, text: str) -> SecurityCheckResult:
        """检查用户输入是否包含注入攻击或危险请求"""
        # 检查注入攻击
        injection_hits = self._match_patterns(text, INJECTION_PATTERNS)
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
        bypass_hits = self._match_patterns(text, BYPASS_PATTERNS)
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
        leak_hits = self._match_patterns(text, LEAK_INPUT_PATTERNS)
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
        dangerous_hits = self._match_patterns(text, DANGEROUS_PATTERNS)
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
        leak_hits = self._match_patterns(text, LEAK_PATTERNS)
        if leak_hits:
            best_pattern, best_conf = max(leak_hits, key=lambda x: x[1])
            action = self._decide_action("leak", best_conf)
            if action == "block":
                return False, f"检测到敏感信息泄露风险 (置信度={best_conf:.2f})"
            elif action == "warn":
                logger.warning(f"检测到潜在信息泄露 (warn): confidence={best_conf:.2f}")
                return True, ""  # warn 模式下仍允许，但已记录日志

        return True, ""

    def is_allowed(self, user_id: str) -> tuple[bool, str]:
        if self._emergency_stop:
            return False, "紧急熔断已启用"
        return True, ""

    def _check_rate(self, user_id: str) -> bool:
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

    def _cleanup_stale_users(self, now: float):
        stale = [uid for uid, ts in self._call_timestamps.items()
                 if not ts or now - ts[-1] > 300]
        for uid in stale:
            del self._call_timestamps[uid]

    def emergency_stop(self):
        self._emergency_stop = True

    def emergency_resume(self):
        self._emergency_stop = False

    def is_owner(self, user_id: str) -> bool:
        if user_id.startswith("cli"):
            return True
        if user_id == "webui":
            return True
        return bool(self.owner_ids) and user_id in self.owner_ids

    @property
    def is_stopped(self) -> bool:
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
            content_safe, content_reason = self.check_content(text)
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