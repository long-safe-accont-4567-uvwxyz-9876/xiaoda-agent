"""项目约束注入器：分层加载，按场景注入，控制 token 预算。

约束文件 ~/.ai-agent/project_constraints.md 按 ## 标题分为三层：
  - ## Always          每次必注入 Stable 层（核心硬约束，<300 token）
  - ## Scene: <source> 按 source 动态注入 Volatile 层（场景约束，<500 token）
  - ## RAG             不预注入，向量化检索（经验教训、工程约定细节，0 token）

source 取值：qq_group / qq_c2c / web / cli
"""

from __future__ import annotations

from typing import ClassVar
import re
from pathlib import Path
from loguru import logger

# Token 预算（按字符数估算，中文约 1 字符 = 0.5 token）
_ALWAYS_BUDGET = 600   # ~300 token
_SCENE_BUDGET = 1000   # ~500 token

_DEFAULT_PATH = Path.home() / ".ai-agent" / "project_constraints.md"

# source → Scene 段标题映射
_SCENE_MAP = {
    "qq_group": "Scene: qq_group",
    "qq_c2c": "Scene: qq_group",   # QQ 私聊共用群聊约束
    "web": "Scene: web",
    "cli": "Scene: cli",
}


class ConstraintInjector:
    """单例约束注入器，解析 markdown 分段并按预算截断。"""

    _instance: "ConstraintInjector | None" = None
    _cache: ClassVar[dict[str, str]] = {}        # section_title -> content
    _mtime: float = 0.0
    _path: Path = _DEFAULT_PATH

    @classmethod
    def get_instance(cls, path: Path | None = None) -> "ConstraintInjector":
        if cls._instance is None:
            cls._instance = cls()
        if path:
            cls._instance._path = path
        cls._instance._refresh_if_stale()
        return cls._instance

    def _refresh_if_stale(self) -> None:
        """文件变更时重新加载。"""
        if not self._path.exists():
            return
        mtime = self._path.stat().st_mtime
        if mtime != self._mtime:
            self._load()
            self._mtime = mtime

    def _load(self) -> None:
        """解析 markdown 文件，按 ## 标题分段。"""
        try:
            text = self._path.read_text(encoding="utf-8")
        except Exception as e:
            logger.warning("constraint.load_failed", error=str(e), path=str(self._path))
            return

        self._cache.clear()
        # 按 ## 标题分割（不匹配 ### 等更深层级）
        sections = re.split(r'^## ', text, flags=re.MULTILINE)
        for section in sections:
            if not section.strip():
                continue
            lines = section.split("\n", 1)
            title = lines[0].strip()
            content = lines[1].strip() if len(lines) > 1 else ""
            self._cache[title] = content

        logger.debug("constraint.loaded",
                     sections=list(self._cache.keys()),
                     path=str(self._path))

    def _truncate_to_budget(self, text: str, budget: int) -> str:
        """按字符预算截断，在行边界切分。"""
        if len(text) <= budget:
            return text
        # 在预算附近找最后一个换行
        cut = text.rfind("\n", 0, budget)
        if cut == -1:
            cut = budget
        return text[:cut].rstrip()

    def get_always(self) -> str:
        """获取 Always 层约束（每次必注入）。"""
        content = self._cache.get("Always", "")
        if not content:
            return ""
        return self._truncate_to_budget(content, _ALWAYS_BUDGET)

    def get_scene(self, source: str) -> str:
        """获取场景约束（按 source 选择）。"""
        if not source:
            return ""
        key = _SCENE_MAP.get(source, "")
        if not key:
            return ""
        content = self._cache.get(key, "")
        if not content:
            return ""
        return self._truncate_to_budget(content, _SCENE_BUDGET)

    def build_stable_segment(self) -> str:
        """构建 Stable 层约束段落（Always）。"""
        always = self.get_always()
        if not always:
            return ""
        return f"[项目硬约束]\n{always}"

    def build_volatile_segment(self, source: str = "") -> str:
        """构建 Volatile 层约束段落（Scene）。"""
        scene = self.get_scene(source)
        if not scene:
            return ""
        return f"[场景约束:{source}]\n{scene}"

    def search_rag(self, query: str, top_k: int = 3) -> list[str]:
        """FTS 检索 RAG 层经验教训，按关键词匹配返回相关条目。

        简单实现：用 jieba 分词提取关键词，在 RAG 内容中做子串匹配。
        不依赖向量库，零成本接入现有检索流程。

        Args:
            query: 用户输入或检索 query
            top_k: 返回最多几条

        Returns:
            匹配的经验教训条目列表
        """
        rag_content = self._cache.get("RAG", "")
        if not rag_content:
            return []

        # 提取查询关键词（jieba 分词，去除停用词和短词）
        keywords = self._extract_keywords(query)
        if not keywords:
            return []

        # 按行分割 RAG 内容，每行一条经验教训
        lines = [line.strip().lstrip("- ").strip()
                 for line in rag_content.split("\n")
                 if line.strip().startswith("-")]

        # 关键词匹配评分
        scored: list[tuple[float, str]] = []
        for line in lines:
            if not line or len(line) < 10:
                continue
            score = 0.0
            line_lower = line.lower()
            for kw in keywords:
                if kw.lower() in line_lower:
                    score += 1.0
                    # 标题关键词加权
                    if kw in ("qq", "群聊", "表情", "回复", "截断", "分片"):
                        score += 0.5
            if score > 0:
                scored.append((score, line))

        # 按分数降序，返回 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        return [text for _, text in scored[:top_k]]

    def _extract_keywords(self, text: str) -> list[str]:
        """用 jieba 分词提取关键词，过滤短词和停用词。"""
        try:
            import jieba
            words = jieba.cut_for_search(text)
            stopwords = {"的", "了", "是", "在", "我", "你", "他", "她",
                        "和", "与", "或", "也", "都", "就", "这", "那",
                        "什么", "怎么", "为什么", "哪", "哪个", "哪些",
                        "一个", "一些", "可以", "能", "会", "要", "想",
                        "说", "做", "看", "听", "问", "答", "吧", "呢",
                        "啊", "哦", "嗯", "呀", "吗", "哈", "嘿"}
            keywords = []
            for w in words:
                w_s = w.strip()
                if len(w_s) >= 2 and w_s not in stopwords and not w_s.isdigit():
                    keywords.append(w_s)
            return keywords[:15]  # 最多 15 个关键词
        except ImportError:
            # jieba 不可用时退化为简单分词
            import re
            return [w for w in re.split(r'[\s,，。！？、；：]+', text)
                    if len(w) >= 2][:15]



# 模块级便捷函数（仿 permanent_memory 模式）
def get_constraint_injector(path: Path | None = None) -> ConstraintInjector:
    """获取约束注入器单例（模块级便捷函数）。"""
    return ConstraintInjector.get_instance(path)


def get_stable_constraints() -> str:
    """获取 Stable 层约束（Always），供 prompt_builder 调用。"""
    return get_constraint_injector().build_stable_segment()


def get_scene_constraints(source: str = "") -> str:
    """获取 Volatile 层约束（Scene），供 agent_context 调用。"""
    return get_constraint_injector().build_volatile_segment(source)


def search_constraint_lessons(query: str, top_k: int = 3) -> list[str]:
    """检索 RAG 层经验教训，供 message_processor 调用。"""
    return get_constraint_injector().search_rag(query, top_k)