"""
智能错误处理和自我修复模块
增强 Agent 的错误诊断、自动修复和学习能力
"""

import re
from loguru import logger
from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class ErrorContext:
    error_type: str
    error_message: str
    file_path: str = ""
    line_number: int = 0
    context_code: str = ""
    suggested_fix: str = ""


class SmartErrorHandler:
    """智能错误处理器，记录并解析错误上下文。"""

    def __init__(self, db: Any | None=None, dispatcher: Any | None=None) -> None:
        self._db = db
        self._dispatcher = dispatcher
        self._recent_errors: list[ErrorContext] = []
        self._max_error_history = 10

    def record_error(self, error: Exception, context: str = "") -> ErrorContext:
        """记录错误到历史"""
        error_ctx = self._parse_error(error, context)
        self._recent_errors.append(error_ctx)
        
        # 保持历史记录在合理范围内
        if len(self._recent_errors) > self._max_error_history:
            self._recent_errors = self._recent_errors[-self._max_error_history:]
        
        logger.warning("error_handler.recorded",
                      type=error_ctx.error_type,
                      msg=error_ctx.error_message[:100])
        return error_ctx

    def _parse_error(self, error: Exception, context: str = "") -> ErrorContext:
        """解析错误信息"""
        error_str = str(error)
        error_type = type(error).__name__
        
        # 提取文件路径和行号
        file_match = re.search(r'File "(.+?)", line (\d+)', error_str)
        file_path = file_match.group(1) if file_match else ""
        line_number = int(file_match.group(2)) if file_match else 0
        
        # 对于 AttributeError，提取详细信息
        if error_type == "AttributeError":
            attr_match = re.search(r"'(\w+)' object has no attribute '(\w+)'", error_str)
            if attr_match:
                class_name, missing_attr = attr_match.groups()
                return ErrorContext(
                    error_type=error_type,
                    error_message=error_str,
                    file_path=file_path,
                    line_number=line_number,
                    context_code=context,
                    suggested_fix=self._suggest_attribute_fix(class_name, missing_attr)
                )
        
        # 对于 ImportError
        elif error_type in ("ImportError", "ModuleNotFoundError"):
            module_match = re.search(r"No module named '(\w+)'", error_str)
            if module_match:
                module_name = module_match.group(1)
                return ErrorContext(
                    error_type=error_type,
                    error_message=error_str,
                    suggested_fix=f"请安装缺少的模块: pip install {module_name}"
                )
        
        return ErrorContext(
            error_type=error_type,
            error_message=error_str,
            file_path=file_path,
            line_number=line_number,
            context_code=context
        )

    def _suggest_attribute_fix(self, class_name: str, missing_attr: str) -> str:
        """为 AttributeError 生成修复建议"""
        
        # 常见的 DatabaseManager 属性映射
        db_manager_attrs = {
            "learning": "LearningDB",
            "memory": "MemoryDB",
            "notebook": "NotebookDB",
            "knowledge": "KnowledgeDB",
            "analytics": "AnalyticsDB"
        }
        
        if class_name == "DatabaseManager":
            for attr in db_manager_attrs:
                if missing_attr in dir(__import__(f'db.db_{attr}', fromlist=[attr])):
                    return f"应该通过 self._db.{attr}.{missing_attr}() 调用，而不是 self._db.{missing_attr}()"
        
        return f"检查 {class_name} 类是否定义了 {missing_attr} 方法或属性"

    async def handle_error_with_intelligence(self, error: Exception, 
                                            user_query: str = "",
                                            context: str = "") -> str:
        """智能处理错误，返回友好的错误信息和修复建议"""
        
        error_ctx = self.record_error(error, context)
        
        # 生成用户友好的错误回复
        reply_parts = [
            f"⚠️ 执行时遇到了点小问题：{error_ctx.error_type}",
            f"📝 错误详情：{error_ctx.error_message[:200]}"
        ]
        
        if error_ctx.suggested_fix:
            reply_parts.append(f"\n💡 修复建议：{error_ctx.suggested_fix}")
            
            # 尝试自动修复简单错误
            if error_ctx.error_type == "AttributeError" and "DatabaseManager" in error_ctx.error_message:
                auto_fix_result = await self._attempt_auto_fix(error_ctx)
                if auto_fix_result:
                    reply_parts.append(f"\n✅ 自动修复结果：{auto_fix_result}")
        
        # 如果有最近的错误历史，且用户在询问解决方案
        if self._is_asking_for_solution(user_query):
            reply_parts.append("\n\n🔧 要不要让人家尝试自动修复这个问题？")
        
        # 记录到学习系统
        if self._db:
            try:
                await self._learn_from_error(error_ctx)
            except Exception as e:
                logger.warning("error_handler.learn_failed", error=str(e))
        
        return "\n".join(reply_parts)

    def _is_asking_for_solution(self, query: str) -> bool:
        """判断用户是否在询问解决方案"""
        if not query or not self._recent_errors:
            return False
            
        solution_keywords = ["怎么办", "怎么修", "如何解决", "修复", "fix", "help"]
        return any(kw in query.lower() for kw in solution_keywords)

    async def _attempt_auto_fix(self, error_ctx: ErrorContext) -> str | None:
        """尝试自动修复简单的错误"""
        
        if error_ctx.error_type != "AttributeError":
            return None
            
        if "DatabaseManager" not in error_ctx.error_message:
            return None
            
        # 这里可以集成实际的代码修复逻辑
        # 目前返回建议信息
        return (
            "检测到这是一个数据库访问错误。\n"
            "正确的调用方式应该是通过对应的 DB 子类，例如：\n"
            "• 学习相关: self._db.learning.method()\n"
            "• 记忆相关: self._db.memory.method()\n"
            "• 笔记相关: self._db.notebook.method()\n"
            "\n已经帮你定位到问题了哦～ 🌿"
        )

    async def _learn_from_error(self, error_ctx: ErrorContext) -> None:
        """将错误记录到学习系统，避免重复犯错"""
        if not hasattr(self._db, 'learning'):
            return
            
        pattern_key = f"{error_ctx.error_type}:{error_ctx.error_message[:50]}"
        
        await self._db.learning.insert_learning(
            category="error_pattern",
            priority="high",
            summary=f"{error_ctx.error_type}: {error_ctx.error_message[:100]}",
            details=(
                f"文件: {error_ctx.file_path}\n"
                f"行号: {error_ctx.line_number}\n"
                f"修复建议: {error_ctx.suggested_fix}\n"
                f"上下文: {error_ctx.context_code[:300]}"
            ),
            pattern_key=pattern_key,
            source="error_handler",
            suggested_action=error_ctx.suggested_fix
        )
        
        logger.info("error_handler.learned", pattern=pattern_key)

    async def search_similar_errors(self, error: str, top_k: int = 3) -> list:
        """检索相似错误经验（从 learnings 表，参数化查询）"""
        if not self._db:
            return []
        try:
            # learnings 表中 error_pattern 类别存储错误经验，summary 包含错误摘要
            cursor = await self._db._conn.execute(
                "SELECT * FROM learnings WHERE category='error_pattern' AND summary LIKE ? "
                "ORDER BY recurrence_count DESC LIMIT ?",
                (f"%{error[:100]}%", top_k),
            )
            rows = await cursor.fetchall()
            # 映射字段，供 FailureTrigger._reflect 使用
            result = []
            for r in rows:
                d = dict(r)
                d["correction"] = d.get("suggested_action", "")
                d["error_pattern"] = d.get("summary", "")
                result.append(d)
            return result
        except Exception:
            return []

    async def count_by_error_type(self, error_type: str) -> int:
        """统计同类错误次数（pattern_key 以 error_type 为前缀）"""
        if not self._db:
            return 0
        try:
            cursor = await self._db._conn.execute(
                "SELECT COUNT(*) as cnt FROM learnings "
                "WHERE category='error_pattern' AND pattern_key LIKE ?",
                (f"{error_type}:%",),
            )
            row = await cursor.fetchone()
            return row["cnt"] if row else 0
        except Exception:
            return 0

    async def promote_error_pattern(self, error_type: str, correction: str) -> None:
        """提升错误模式为系统提示规则（status → promoted）"""
        if not self._db:
            return
        try:
            await self._db._conn.execute(
                "UPDATE learnings SET status='promoted' "
                "WHERE category='error_pattern' AND pattern_key LIKE ? AND status != 'promoted'",
                (f"{error_type}:%",),
            )
            await self._db._conn.commit()
        except Exception as e:
            logger.warning(f"smart_error_handler.promote_failed: {e}")

    async def log_error(self, task: str = "", error: str = "",
                        error_type: str = "", correction: str = "",
                        outcome: str = "") -> None:
        """归档错误经验到 learnings 表（供 FailureTrigger 调用）"""
        if not self._db or not hasattr(self._db, 'learning'):
            return
        try:
            pattern_key = f"{error_type}:{error[:50]}" if error_type else f"unknown:{error[:50]}"
            await self._db.learning.insert_learning(
                category="error_pattern",
                priority="high" if outcome == "failure" else "medium",
                summary=f"{error_type}: {error[:100]}" if error_type else error[:100],
                details=f"task: {task[:200]}\noutcome: {outcome}\ncorrection: {correction[:200]}",
                suggested_action=correction[:300],
                source="failure_trigger",
                pattern_key=pattern_key,
            )
        except Exception as e:
            logger.warning(f"smart_error_handler.log_error_failed: {e}")

    def should_delegate_to_specialist(self, error_ctx: ErrorContext | str = None,
                                      context: dict | None = None) -> str | None:
        """判断是否应该将任务委托给专业子代理，返回子代理名称或 None"""

        # 兼容传入字符串的情况
        if isinstance(error_ctx, str):
            error_type = error_ctx
        elif isinstance(error_ctx, ErrorContext):
            error_type = error_ctx.error_type
        else:
            return None

        # 代码相关的错误应该委托给 xiaolang（路由器按内部 name 解析）
        code_error_types = {"SyntaxError", "IndentationError", "TypeError", "AttributeError"}
        if error_type in code_error_types:
            return "xiaolang"

        # 数据/搜索相关错误委托给 xiaolian
        db_error_types = {"OperationalError", "IntegrityError", "DatabaseError"}
        if error_type in db_error_types:
            return "xiaolian"

        # 研究分析相关错误委托给 xiaoke
        research_error_types = {"ValueError", "KeyError", "RuntimeError"}
        if error_type in research_error_types:
            return "xiaoke"

        return None

    async def get_repair_suggestion_from_agent(self, agent_name: str, 
                                             error_ctx: ErrorContext) -> str | None:
        """从专业子代理获取修复建议"""
        
        if not self._dispatcher:
            return None
            
        specialist = self._dispatcher.get_agent(agent_name)
        if not specialist or not specialist.available:
            return None
            
        prompt = f"""请分析以下错误并提供修复建议：

错误类型: {error_ctx.error_type}
错误信息: {error_ctx.error_message}
文件位置: {error_ctx.file_path}:{error_ctx.line_number}
上下文代码: {error_ctx.context_code}

请提供：
1. 错误原因分析
2. 具体的修复代码
3. 如何避免此类错误的建议"""

        try:
            return await specialist.chat(prompt)
        except Exception as e:
            logger.warning("error_handler.agent_consult_failed", 
                          agent=agent_name, error=str(e))
            return None

    def get_recent_error_summary(self) -> str:
        """获取最近错误的摘要，用于上下文理解"""
        if not self._recent_errors:
            return ""
            
        latest = self._recent_errors[-1]
        return (
            f"[最近的错误]\n"
            f"类型: {latest.error_type}\n"
            f"信息: {latest.error_message[:150]}\n"
            f"建议: {latest.suggested_fix or '暂无'}"
        )


# 全局单例实例
_error_handler_instance: SmartErrorHandler | None = None


def get_error_handler(db: Any | None=None, dispatcher: Any | None=None) -> SmartErrorHandler:
    """获取全局错误处理器实例"""
    global _error_handler_instance
    
    if _error_handler_instance is None:
        _error_handler_instance = SmartErrorHandler(db, dispatcher)
    else:
        if db:
            _error_handler_instance._db = db
        if dispatcher:
            _error_handler_instance._dispatcher = dispatcher
            
    return _error_handler_instance