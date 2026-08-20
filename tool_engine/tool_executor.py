from typing import Any, ClassVar
import asyncio
import json
import time
import inspect
from concurrent.futures import ThreadPoolExecutor
from loguru import logger

from .tool_registry import get_tool, ToolResult, resolve_tool_func
from .approver import (
    Approver,
    DefaultApprover,
    SessionApprover,
    ApprovalRequest,
    ApprovalDecision,
    ApprovalOutcome,
)
from utils.metrics import metrics

# 敏感参数关键词，匹配到的参数值会被屏蔽
_SENSITIVE_KEYS = {'key', 'token', 'password', 'secret', 'api_key', 'credential'}


def _filter_sensitive_args(arguments: dict) -> dict:
    """过滤敏感参数值，保留参数名，值替换为 ***REDACTED***"""
    filtered = {}
    for k, v in arguments.items():
        if any(s in k.lower() for s in _SENSITIVE_KEYS):
            filtered[k] = '***REDACTED***'
        else:
            filtered[k] = v
    return filtered


class ToolExecutor:
    """工具执行器，按名称调度工具并应用超时控制。"""

    # 按工具名自定义超时（秒），default 为全局默认
    TOOL_TIMEOUTS: ClassVar[dict[str, float]] = {
        "agnes_video_generate": 240,
        "document_reader": 120,
        "web_browse": 30,           # 网页渲染较慢
        "multi_search": 25,         # 多引擎并发搜索
        "web_search": 15,           # 单次网络搜索
        "wolfram_query": 20,        # 知识计算引擎
        "python_executor": 30,      # 代码执行可能较慢
        "shell_command": 20,        # Shell 命令执行
        "delegate_task": 60,        # 子代理委托
        # N9 修复：recall 短超时 15s 兜底（recall 函数内部已有 10s 自身超时）。
        # 原 60s 兜底在生产中卡死事件循环 60s 才触发，用户体验灾难。
        # 15s 留 5s buffer 给格式化阶段，作为 recall 内部 10s 超时失效时的最后防线。
        "recall": 15,
        "default": 60.0,
    }

    # ── S3: 重试与循环检测配置 ──
    RETRYABLE_ERRORS: ClassVar[set[str]] = {"timeout", "connection", "temporal", "transient",
                                  "timeouterror", "connectionerror", "apierror",
                                  "ratelimit", "503", "502", "429"}
    MAX_RETRIES: int = 2
    RETRY_BASE_DELAY: float = 0.5
    RETRY_MAX_DELAY: float = 5.0
    FAILURE_STREAK_THRESHOLD: int = 5
    FAILURE_STREAK_RESET_SECONDS: int = 300  # 5 分钟后半开恢复
    # 命令确认等待超时（秒）：用户未在期限内确认则按"拒绝"处理
    CMD_CONFIRM_TIMEOUT: float = 60.0

    def __init__(self, db: Any | None=None, approver: Approver | None=None,
                 cmd_confirm_timeout: float | None = None,
                 decision_provider: Any | None = None) -> None:
        self.db = db
        self._approver = approver or DefaultApprover()
        self._call_counts: dict[str, list[float]] = {}
        self._global_timeout: float = self.TOOL_TIMEOUTS["default"]
        self._failure_streaks: dict[str, int] = {}
        self._failure_first_time: dict[str, float] = {}
        # 命令确认参数：decision_provider(request_id)->str|None 可注入（测试用），
        # 默认走 web.routers.workspace.get_pending_cmd_decision（前端确认结果）。
        self._cmd_confirm_timeout = cmd_confirm_timeout if cmd_confirm_timeout is not None \
            else self.CMD_CONFIRM_TIMEOUT
        self._decision_provider = decision_provider
        # 专用线程池：同步工具在线程中执行。与全局默认 executor 隔离，
        # 避免后台任务（记忆编码/本能提取等）的同步阻塞调用占满默认线程池，
        # 导致工具执行在其后排队数十秒（真实案例：计算器 88s 延迟）。
        self._tool_threadpool: ThreadPoolExecutor = ThreadPoolExecutor(
            max_workers=8, thread_name_prefix="tool-exec")

    def close(self) -> None:
        """释放专用线程池（进程退出时调用，避免线程泄漏）。"""
        self._tool_threadpool.shutdown(wait=False, cancel_futures=False)

    @property
    def approver(self) -> Approver:
        """获取当前审批器实例。"""
        return self._approver

    def _is_retryable_error(self, error: str) -> bool:
        """检查错误是否为瞬时错误，值得重试."""
        error_lower = (error or "").lower()
        return any(keyword in error_lower for keyword in self.RETRYABLE_ERRORS)

    async def _get_cmd_decision(self, request_id: str) -> str | None:
        """查询命令确认决策（None 表示尚未决策）。

        默认从 web.routers.workspace 的前端确认结果队列读取；
        测试可注入 decision_provider 覆盖。
        """
        if self._decision_provider is not None:
            return self._decision_provider(request_id)
        from web.routers.workspace import get_pending_cmd_decision
        return get_pending_cmd_decision(request_id)

    async def _await_cmd_confirmation(self, cmd: str, tool_name: str) -> str:
        """暂停-确认-继续：推送确认卡片并等待用户决策。

        返回 "allow"/"allow_once"（放行）| "deny"（拒绝）| "timeout"（超时未决策）。
        命令交由调用方决定执行或拒绝，不再以"先失败"方式返回给 LLM。

        身份路由：从当前异步上下文读取发起会话，确认请求只推送给该会话对应
        连接（无会话时回退广播），并把发起会话绑定到决策记录，由 confirm_cmd
        校验回传身份，防止无关客户端替他人决定命令放行/拒绝。
        """
        import uuid as _uuid
        req_id = _uuid.uuid4().hex[:16]
        try:
            from web.ws_hub import current_session_id
            session_id = current_session_id()
        except (ImportError, RuntimeError):
            session_id = ""
        # 登记发起会话到决策记录（confirm_cmd 身份校验依据）
        try:
            from web.routers.workspace import register_cmd_decision_scope
            register_cmd_decision_scope(req_id, session_id)
        except (ImportError, RuntimeError, ValueError) as _e:
            logger.debug("tool_executor.cmd_scope_register_failed", error=str(_e))
        logger.info("tool_executor.needs_confirmation",
                    tool=tool_name, command=cmd[:200], request_id=req_id,
                    session_id=session_id)
        # 推送 WS 消息到前端，触发命令确认问答卡片（非阻塞）
        payload = {
            "type": "cmd_confirm_request",
            "request_id": req_id,
            "command": cmd[:500],
            "tool": tool_name,
            "session_id": session_id,
        }
        try:
            from web.ws_hub import manager as _ws_manager
            if session_id:
                await _ws_manager.send_to_session(session_id, payload)
            else:
                await _ws_manager.broadcast(payload)
        except (RuntimeError, OSError, ConnectionError) as _e:
            logger.warning("tool_executor.cmd_confirm_push_failed", error=str(_e))
        # 异步等待用户决策（最多 CMD_CONFIRM_TIMEOUT 秒），期间不阻塞事件循环
        deadline = time.time() + self._cmd_confirm_timeout
        while time.time() < deadline:
            try:
                decision = await self._get_cmd_decision(req_id)
            except (RuntimeError, KeyError, ValueError) as _e:
                logger.warning("tool_executor.cmd_decision_lookup_failed", error=str(_e))
                decision = None
            if decision is not None:
                logger.info("tool_executor.cmd_decision_received",
                            request_id=req_id, decision=decision)
                return decision
            await asyncio.sleep(0.1)
        # 超时：清理决策记录，防止内存泄漏与迟到的越权决策
        try:
            from web.routers.workspace import discard_cmd_decision_scope
            discard_cmd_decision_scope(req_id)
        except (ImportError, RuntimeError, ValueError) as _e:
            logger.debug("tool_executor.cmd_scope_discard_failed", error=str(_e))
        logger.warning("tool_executor.cmd_confirm_timeout", tool=tool_name)
        return "timeout"

    async def execute(self, tool_name: str, arguments: dict,
                      user_id: str = "", safe_mode: bool = False) -> ToolResult:
        tool = get_tool(tool_name)
        if not tool:
            logger.warning("tool_executor.not_found", tool=tool_name)
            return ToolResult.fail(f"还没有学会「{tool_name}」这个技能呢……")

        if tool.get("enabled") is False:
            logger.warning("tool_executor.disabled", tool=tool_name)
            return ToolResult.fail(f"「{tool_name}」已被管理员全局停用了呢～")

        # S3: 循环检测 — 连续失败次数过多则短路（5 分钟后半开恢复）
        if self._failure_streaks.get(tool_name, 0) >= self.FAILURE_STREAK_THRESHOLD:
            first_time = self._failure_first_time.get(tool_name)
            if first_time is not None and time.time() - first_time > self.FAILURE_STREAK_RESET_SECONDS:
                # 半开恢复：重置计数，允许重试
                self._failure_streaks[tool_name] = 0
                self._failure_first_time.pop(tool_name, None)
                logger.info("tool_executor.failure_streak_reset", tool=tool_name)
            else:
                logger.warning("tool_executor.failure_streak_blocked", tool=tool_name,
                               streak=self._failure_streaks.get(tool_name, 0))
                return ToolResult.fail(f"工具「{tool_name}」连续失败次数过多，已暂时停用")

        if not self._check_rate_limit(tool_name, tool):
            logger.warning("tool_executor.rate_limited", tool=tool_name)
            return ToolResult.fail("刚才已经帮你查过了呢……等一会儿再看好不好？")

        # 沙箱安全检查：网络/文件/子进程工具执行前强制校验
        sandbox_err = self._enforce_sandbox(tool_name, arguments)
        if sandbox_err:
            logger.warning("tool_executor.sandbox_blocked", tool=tool_name, reason=sandbox_err)
            return ToolResult.fail(f"安全沙箱阻止了此操作：{sandbox_err}")

        # 工作目录边界检查（叠加层）：文件工具检查路径 ∈ cwd，shell 工具检查命令 ∈ 白名单
        # 独立于 PermissionMode，用户通过 webui 显式授权后才允许访问工作目录
        ws_err = self._enforce_workspace_boundary(tool_name, arguments)
        if ws_err:
            if ws_err.startswith("__NEEDS_CONFIRMATION__:"):
                cmd = ws_err[len("__NEEDS_CONFIRMATION__:"):]
                # 暂停-确认-继续：不直接失败，推送确认卡片并等待用户决策。
                # 确认后继续执行并返回真实结果给 LLM；拒绝/超时则作为用户决策失败返回。
                decision = await self._await_cmd_confirmation(cmd, tool_name)
                if decision == "deny":
                    logger.info("tool_executor.cmd_denied", command=cmd[:200])
                    return ToolResult.fail(
                        f"命令已被用户拒绝执行：{cmd[:200]}", user_decision=True)
                if decision == "timeout":
                    logger.warning("tool_executor.cmd_timeout_denied", command=cmd[:200])
                    return ToolResult.fail(
                        f"命令确认超时（{int(self._cmd_confirm_timeout)}秒），未执行：{cmd[:200]}",
                        user_decision=True)
                # Fail-closed：只放行明确的 allow/allow_once，其余未知值一律拒绝，
                # 避免畸形决策（如 "error"）意外授权命令执行。
                if decision not in ("allow", "allow_once"):
                    logger.warning("tool_executor.cmd_invalid_decision",
                                   decision=decision, command=cmd[:200])
                    return ToolResult.fail(
                        f"命令确认结果无效，未执行：{cmd[:200]}",
                        user_decision=True)
                # decision in ("allow", "allow_once") → 放行，继续执行
            else:
                logger.warning("tool_executor.workspace_blocked", tool=tool_name, reason=ws_err)
                return ToolResult.fail(ws_err)

        from security.permission_manager import get_permission_manager
        permission_allowed, permission_reason = get_permission_manager().check_tool_permission(
            tool_name, arguments
        )
        logger.bind(
            tool=tool_name,
            user_id=user_id,
            argument_keys=sorted(str(key) for key in arguments),
            argument_count=len(arguments),
            allowed=permission_allowed,
            has_reason=bool(permission_reason),
        ).info("tool.permission_checked")
        if not permission_allowed:
            logger.info("tool_executor.permission_mode_blocked", tool=tool_name, reason=permission_reason)
            return ToolResult.fail(permission_reason, user_decision=True)

        # ── Approver 审批检查（借鉴 OpenWorker TurnEngine 的 out-of-band 审批）──
        # 在沙箱和工作目录检查通过后、实际执行前检查审批器。
        # 不传 approver 时 DefaultApprover 直接返回 ONCE（放行），不影响原有逻辑。
        approval_req = ApprovalRequest(
            tool_name=tool_name,
            arguments=arguments,
            risk_level=self._get_tool_risk_level(tool_name),
            user_id=user_id,
        )
        try:
            approval_decision = await self._approver.approve(approval_req)
        except (RuntimeError, ValueError, OSError) as e:
            logger.warning("tool_executor.approver_error", tool=tool_name, error=str(e))
            # Fail-closed：高风险工具（EXECUTE 权限，如 shell_command）approver 异常时拒绝，
            # 避免审批器故障导致危险操作放行；低/中风险工具放行以保证可用性。
            if approval_req.risk_level == "high":
                approval_decision = ApprovalDecision(
                    outcome=ApprovalOutcome.DENY,
                    reason=f"approver error on high-risk tool, failing closed: {e}",
                )
            else:
                approval_decision = ApprovalDecision(
                    outcome=ApprovalOutcome.ONCE,
                    reason=f"approver error, defaulting to ONCE: {e}",
                )

        if approval_decision.outcome == ApprovalOutcome.DENY:
            logger.info("tool_executor.approval_denied",
                        tool=tool_name, reason=approval_decision.reason)
            denial_result = ToolResult.fail(
                f"操作「{tool_name}」已被拒绝执行：{approval_decision.reason}"
            )
            # 拒绝的操作也写审计日志，保证安全溯源链完整
            if self.db:
                await self._write_audit_log(tool_name, arguments, denial_result, user_id)
            return denial_result

        _start = time.time()
        # S3: 重试机制 — 瞬时错误自动重试 + 指数退避
        attempt = 0
        while True:
            result = await self._execute_with_timeout(tool, arguments)
            if result.success or attempt >= self.MAX_RETRIES or not self._is_retryable_error(result.error):
                break
            delay = min(self.RETRY_BASE_DELAY * (2 ** attempt), self.RETRY_MAX_DELAY)
            logger.warning("tool_executor.retry", tool=tool_name, attempt=attempt + 1,
                           max_retries=self.MAX_RETRIES, delay=round(delay, 2),
                           error=result.error[:200])
            await asyncio.sleep(delay)
            attempt += 1
        duration = time.time() - _start
        metrics.observe(f"tool_execute.{tool_name}.duration", duration)
        if result.success:
            metrics.inc(f"tool_execute.{tool_name}.success")
            self._failure_streaks[tool_name] = 0
            self._failure_first_time.pop(tool_name, None)
        else:
            metrics.inc(f"tool_execute.{tool_name}.failure")
            new_streak = self._failure_streaks.get(tool_name, 0) + 1
            self._failure_streaks[tool_name] = new_streak
            if new_streak == 1:
                self._failure_first_time[tool_name] = time.time()
            if new_streak == self.FAILURE_STREAK_THRESHOLD:
                logger.warning("tool_executor.failure_streak_threshold", tool=tool_name,
                               streak=new_streak)
        metrics.maybe_report()
        # 结构化日志：工具执行结果
        logger.info("tool.execute", event="tool_execute", tool=tool_name,
                    duration_ms=int(duration * 1000), user_id=user_id,
                    success=result.success)

        # A4: 工具执行结果 → 学习反馈闭环 (失败不阻塞主流程)
        try:
            from core.learning_feedback import record_tool_outcome
            record_tool_outcome(
                tool_name=tool_name,
                arguments=arguments,
                success=result.success,
                error=result.error or "",
                duration=duration,
            )
        except (RuntimeError, OSError, ValueError) as _e:
            logger.debug("tool_executor.learning_feedback_failed: {}", _e)

        if self.db:
            await self._write_audit_log(tool_name, arguments, result, user_id)

        return result

    def _check_rate_limit(self, tool_name: str, tool: dict) -> bool:
        max_freq = tool.get("max_frequency", 6000)
        if max_freq == 0:
            return True

        now = time.time()
        window = 10
        timestamps = self._call_counts.get(tool_name, [])
        timestamps = [t for t in timestamps if now - t < window]
        self._call_counts[tool_name] = timestamps

        if len(timestamps) >= max_freq:
            return False
        timestamps.append(now)
        return True

    def _get_tool_risk_level(self, tool_name: str) -> str:
        """根据工具权限级别返回风险等级字符串（供 Approver 使用）。"""
        tool = get_tool(tool_name)
        if not tool:
            return "low"
        from .tool_registry import ToolPermission
        perm = tool.get("permission", ToolPermission.READ_ONLY)
        if perm == ToolPermission.EXECUTE:
            return "high"
        if perm == ToolPermission.READ_WRITE:
            return "medium"
        return "low"

    # ── 沙箱安全检查 ─────────────────────────────────────────────
    # 需要检查 URL 的网络工具
    _NETWORK_TOOLS: ClassVar[set[str]] = {"web_browse", "web_search", "multi_search", "web_browse_enhanced"}
    # 需要检查路径的文件工具
    _FILE_TOOLS: ClassVar[set[str]] = {"read_file", "write_file", "list_files", "search_files", "document_reader"}
    # 需要检查命令的子进程工具
    _SHELL_TOOLS: ClassVar[set[str]] = {"shell_command", "python_executor"}
    # 允许的无害子进程命令前缀（即使沙箱 strict 也放行）
    _SAFE_SHELL_PREFIXES: ClassVar[tuple[str, ...]] = ("python3 -c", "python -c", "echo", "date", "whoami", "pwd", "ls", "cat")

    # ── 工作目录边界检查（叠加层，独立于 _enforce_sandbox） ──────
    # 受 workspace 授权约束的文件工具（路径必须 ∈ cwd）
    _WORKSPACE_FILE_TOOLS: ClassVar[set[str]] = {
        "read_file", "write_file", "edit_file", "create_file",
        "list_files", "search_files", "delete_file", "document_reader",
    }
    # 受 workspace 授权约束的 shell 工具（命令必须在白名单/不在黑名单）
    _WORKSPACE_SHELL_TOOLS: ClassVar[set[str]] = {"shell_command", "python_executor"}

    @staticmethod
    def _extract_file_path(tool_name: str, arguments: dict) -> str:
        """提取文件类工具的目标路径，兼容不同工具的参数命名。

        - 常规工具：path / file_path / dir
        - write_file：input_str 为 "路径|||内容" 格式，取 ||| 前的路径
        - search_files：pattern 为搜索模式，取目录部分
        """
        raw = (arguments.get("path") or arguments.get("file_path")
               or arguments.get("dir") or "")
        if raw:
            return raw
        if tool_name == "write_file":
            input_str = arguments.get("input_str") or ""
            return (input_str.split("|||", 1)[0] if "|||" in input_str else "").strip()
        if tool_name == "search_files":
            pattern = arguments.get("pattern") or ""
            import os as _os
            return _os.path.dirname(pattern) or "."
        return ""

    def _enforce_sandbox(self, tool_name: str, arguments: dict) -> str | None:
        """工具执行前沙箱检查。返回 None 表示放行，返回字符串为拒绝原因。"""
        from security.sandbox_config import check_domain_allowed, check_path_allowed, get_default_sandbox
        sandbox = get_default_sandbox()

        # 网络工具：检查 URL 参数
        if tool_name in self._NETWORK_TOOLS:
            url = arguments.get("url") or arguments.get("query") or ""
            if url and url.startswith(("http://", "https://")):
                allowed, reason = check_domain_allowed(url, sandbox)
                if not allowed:
                    return f"域名不被允许：{reason}"

        # 文件工具：检查路径参数
        if tool_name in self._FILE_TOOLS:
            path = self._extract_file_path(tool_name, arguments)
            if path:
                allowed, reason = check_path_allowed(path, sandbox)
                if not allowed:
                    return f"路径不被允许：{reason}"

        # 子进程工具：strict 模式下限制危险命令
        if tool_name in self._SHELL_TOOLS:
            cmd = arguments.get("command") or arguments.get("code") or ""
            if cmd and sandbox.network.block_private_ips:
                # 阻止明显的危险命令
                # 注意：wget/curl 的检测必须匹配"下载后管道给 shell"（如 `wget URL | sh`），
                # 不能用 "wget.*|.*sh" —— 该正则会被拆成 `.*sh`，误匹配任何含 "sh" 的命令
                # （真实案例：`echo shell_ok` 被误伤）。用 `\|\s*(sh|bash)` 限定管道符
                # 后必须跟 shell 解释器，而非任意含 "sh" 的词。
                dangerous = ("rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:",
                             r"wget.*\|\s*(sh|bash)\b", r"curl.*\|\s*(sh|bash)\b",
                             "chmod 777 /", "chown root")
                import re as _re
                for d in dangerous:
                    if _re.search(d, cmd):
                        return f"命令包含危险操作：{d}"

        return None

    def _enforce_workspace_boundary(self, tool_name: str, arguments: dict) -> str | None:
        """工作目录边界检查（叠加层，独立于 PermissionMode）。

        在 _enforce_sandbox 之后执行，对文件工具检查路径 ∈ cwd，
        对 shell 工具检查命令 ∈ 白名单/不在黑名单。

        返回 None 表示放行；返回字符串为拒绝原因；
        返回 "__NEEDS_CONFIRMATION__:<command>" 表示需用户确认（由 execute 转换）。
        """
        from security.permission_manager import get_permission_manager, AuditEntry
        from datetime import datetime as _dt
        pm = get_permission_manager()

        # 文件工具：路径必须在 cwd 内
        if tool_name in self._WORKSPACE_FILE_TOOLS:
            path = self._extract_file_path(tool_name, arguments)
            allowed, reason = pm.is_path_allowed(path)
            # 写入审计缓冲：cwd 始终记录当前工作目录（含未授权情况，便于追溯）
            action = self._classify_file_action(tool_name)
            pm.add_audit_entry(AuditEntry(
                timestamp=_dt.now().isoformat(timespec="seconds"),
                action=action,
                target=path or "(空)",
                cwd=pm.cwd,
                allowed=allowed,
                reason=reason if not allowed else "",
            ))
            if not allowed:
                return reason
            return None

        # shell 工具：命令必须在白名单（黑名单始终生效）
        if tool_name in self._WORKSPACE_SHELL_TOOLS:
            # 高权限模式（GOAT/BYPASS/AUTO）跳过"未授权工作目录"墙：
            # 随心模式是最高权限，绝大多数命令可直接执行，无需先授权目录。
            # is_command_allowed 内部对高权限模式已放行非黑名单命令。
            from security.permission_manager import AUTO_APPROVE_MODES
            _high_perm = pm.mode in AUTO_APPROVE_MODES
            if not _high_perm and not pm.is_cwd_authorized():
                pm.add_audit_entry(AuditEntry(
                    timestamp=_dt.now().isoformat(timespec="seconds"),
                    action="exec",
                    target=(arguments.get("command") or arguments.get("code") or "")[:200],
                    cwd=pm.cwd,  # CodeRabbit #8：与其他 shell 分支保持一致，便于追溯
                    allowed=False,
                    reason="未授权工作目录",
                ))
                return "未授权工作目录，请先在聊天框上方选择并授权工作目录"
            cmd = arguments.get("command") or arguments.get("code") or ""
            allowed, reason, needs_conf = pm.is_command_allowed(cmd)
            if allowed:
                pm.add_audit_entry(AuditEntry(
                    timestamp=_dt.now().isoformat(timespec="seconds"),
                    action="exec",
                    target=cmd[:200],
                    cwd=pm.cwd,
                    allowed=True,
                    reason="",
                ))
                return None
            if needs_conf:
                # 写审计：需用户确认（pending）
                pm.add_audit_entry(AuditEntry(
                    timestamp=_dt.now().isoformat(timespec="seconds"),
                    action="exec",
                    target=cmd[:200],
                    cwd=pm.cwd,
                    allowed=False,
                    reason="等待用户确认",
                ))
                # 返回特殊标记，由 execute 转换为 ToolResult
                return f"__NEEDS_CONFIRMATION__:{cmd}"
            pm.add_audit_entry(AuditEntry(
                timestamp=_dt.now().isoformat(timespec="seconds"),
                action="exec",
                target=cmd[:200],
                cwd=pm.cwd,
                allowed=False,
                reason=reason,
            ))
            return reason

        # 非文件/shell 工具不受 workspace 约束
        return None

    @staticmethod
    def _classify_file_action(tool_name: str) -> str:
        """根据文件工具名推断动作类型（read/write/delete）"""
        if tool_name in ("delete_file",):
            return "delete"
        if tool_name in ("write_file", "edit_file", "create_file"):
            return "write"
        return "read"

    async def _execute_with_timeout(self, tool: dict, arguments: dict) -> ToolResult:
        func, lazy_err = resolve_tool_func(tool)
        if func is None:
            return ToolResult.fail(lazy_err or f"工具「{tool.get('name')}」实现未加载")
        tool_name = tool["name"]
        timeout = self.TOOL_TIMEOUTS.get(tool_name, self._global_timeout)

        try:
            _sig = inspect.signature(func)
            call_args = dict(arguments)

            # 必填参数校验：LLM 可能漏传必填参数
            missing = []
            for pname, param in _sig.parameters.items():
                if param.default is inspect.Parameter.empty and pname not in call_args:
                    missing.append(pname)
            if missing:
                logger.warning("tool_executor.missing_params",
                               tool=tool_name, missing=missing)
                return ToolResult.fail(
                    f"调用「{tool_name}」时缺少必填参数: {', '.join(missing)}"
                )

            if asyncio.iscoroutinefunction(func):
                result = await asyncio.wait_for(
                    func(**call_args) if call_args else func(),
                    timeout=timeout,
                )
            else:
                # 同步工具在专用线程池中执行，避免与后台任务争用默认线程池
                # run_in_executor 只接受位置参数，用 functools.partial 绑定关键字参数
                from functools import partial
                _fn = partial(func, **call_args) if call_args else func
                result = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(
                        self._tool_threadpool, _fn),
                    timeout=timeout,
                )

            if isinstance(result, ToolResult):
                return result
            return ToolResult.ok(result)
        except TimeoutError:
            logger.error("tool_executor.timeout", tool=tool_name, timeout=timeout)
            metrics.inc(f"tool.timeout.{tool_name}")
            # N9 修复（2026-07-25 17:48-17:51 生产事故根因）：
            # 原错误字符串含 "[timeout]" 字样，被 _is_retryable_error 中的
            # "timeout" 关键词匹配，触发自动重试（MAX_RETRIES=2），导致单次
            # 工具调用最坏阻塞 60s × 3 = 180s。
            # 工具执行超时通常意味着底层资源耗尽（事件循环阻塞、SQLite WAL 锁
            # 竞争、KG auto_link 同步计算等），重试只会加剧问题、浪费用户
            # 时间。改为中文表述避免匹配英文 "timeout" 关键词，让超时不重试。
            # 网络瞬时超时（httpx.ConnectTimeout 等）的错误字符串不同（含
            # "ConnectTimeout" / "connection"），仍可被识别为可重试。
            return ToolResult.fail(
                f"工具「{tool_name}」执行超时（{timeout}s），请稍后再试"
            )
        except (RuntimeError, OSError, ValueError, TimeoutError, KeyError) as e:
            logger.error("tool_executor.error", tool=tool_name, error=str(e))
            error_type = type(e).__name__
            return ToolResult.fail(f"出了一点小问题……等会儿再试试好不好？ [{error_type}]")

    async def _write_audit_log(self, tool_name: str, arguments: dict,
                               result: ToolResult, user_id: str) -> None:
        try:
            # 安全加固：过滤敏感参数值
            safe_args = _filter_sensitive_args(arguments)
            await self.db.insert_audit_log(
                event_type="tool_call",
                user_id=user_id,
                detail=json.dumps({
                    "tool": tool_name,
                    "arguments": safe_args,
                    "success": result.success,
                    "error": result.error[:200] if result.error else "",
                }, ensure_ascii=False),
            )
        except (OSError, RuntimeError, ValueError) as e:
            logger.warning("tool_executor.audit_log_failed", error=str(e))