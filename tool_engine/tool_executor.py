from typing import Any, ClassVar
import asyncio
import json
import time
import inspect
from loguru import logger

from .tool_registry import get_tool, ToolResult, resolve_tool_func
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

    def __init__(self, db: Any | None=None) -> None:
        self.db = db
        self._call_counts: dict[str, list[float]] = {}
        self._global_timeout: float = self.TOOL_TIMEOUTS["default"]
        self._failure_streaks: dict[str, int] = {}
        self._failure_first_time: dict[str, float] = {}

    def _is_retryable_error(self, error: str) -> bool:
        """检查错误是否为瞬时错误，值得重试."""
        error_lower = (error or "").lower()
        return any(keyword in error_lower for keyword in self.RETRYABLE_ERRORS)

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
                # 生成 request_id 供前端弹窗匹配
                import uuid as _uuid
                req_id = _uuid.uuid4().hex[:16]
                logger.info("tool_executor.needs_confirmation",
                            tool=tool_name, command=cmd[:200], request_id=req_id)
                return ToolResult.fail(f"__NEEDS_CONFIRMATION__:{req_id}:{cmd}")
            logger.warning("tool_executor.workspace_blocked", tool=tool_name, reason=ws_err)
            return ToolResult.fail(ws_err)

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
        except Exception as _e:
            logger.debug(f"tool_executor.learning_feedback_failed: {_e}")

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
            path = arguments.get("path") or arguments.get("file_path") or arguments.get("dir") or ""
            if path:
                allowed, reason = check_path_allowed(path, sandbox)
                if not allowed:
                    return f"路径不被允许：{reason}"

        # 子进程工具：strict 模式下限制危险命令
        if tool_name in self._SHELL_TOOLS:
            cmd = arguments.get("command") or arguments.get("code") or ""
            if cmd and sandbox.network.block_private_ips:
                # 阻止明显的危险命令
                dangerous = ("rm -rf /", "mkfs", "dd if=", ":(){ :|:& };:", "wget.*|.*sh",
                             "curl.*|.*sh", "chmod 777 /", "chown root")
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
        from security.permission_manager import get_permission_manager
        pm = get_permission_manager()

        # 文件工具：路径必须在 cwd 内
        if tool_name in self._WORKSPACE_FILE_TOOLS:
            path = arguments.get("path") or arguments.get("file_path") or arguments.get("dir") or ""
            allowed, reason = pm.is_path_allowed(path)
            if not allowed:
                return reason
            return None

        # shell 工具：命令必须在白名单（黑名单始终生效）
        if tool_name in self._WORKSPACE_SHELL_TOOLS:
            if not pm.is_cwd_authorized():
                return "未授权工作目录，请先在聊天框上方选择并授权工作目录"
            cmd = arguments.get("command") or arguments.get("code") or ""
            allowed, reason, needs_conf = pm.is_command_allowed(cmd)
            if allowed:
                return None
            if needs_conf:
                # 返回特殊标记，由 execute 转换为 ToolResult
                return f"__NEEDS_CONFIRMATION__:{cmd}"
            return reason

        # 非文件/shell 工具不受 workspace 约束
        return None

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
                result = await asyncio.wait_for(
                    asyncio.to_thread(func, **call_args) if call_args else asyncio.to_thread(func),
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
        except Exception as e:
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
        except Exception as e:
            logger.warning("tool_executor.audit_log_failed", error=str(e))
