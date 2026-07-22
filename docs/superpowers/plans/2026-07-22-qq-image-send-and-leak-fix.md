# QQ 图片真实发送 + 生图类泄漏根治 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 QQ 通道在用户要图时始终收到真实图片消息（非链接/非 markdown），并根治生图类（模型名/markdown图/伪造元数据）泄漏，统一三处回复清洗入口。

**Architecture:** 双保险——提示词强化引导 LLM 真调 `agnes_image_generate`；代码兜底 `_extract_fabricated_images_from_reply` 扫描回复里伪造的 markdown 图/pollinations URL，下载为本地文件塞入 `image_paths`（QQ 适配器现有 `_send_reply_with_media` 发真图），并从文本剥离。新增 `strip_image_gen_leak` 清洗生图类文本泄漏；收敛三处漂移清洗序列为 `_clean_reply_full` 统一出口。

**Tech Stack:** Python 3.11 + asyncio，httpx（异步下载），loguru，pytest，正则清洗。

## Global Constraints

- 项目根：`/home/orangepi/ai-agent`（`/home/orangepi/.ai-agent/proj` 是其符号链接，工作区内用 `proj/` 路径读写）。
- `AgentCore(MessageProcessorMixin, ToolExecutorMixin, SubAgentManagerMixin)`（`agent_core/core.py:79`）；`self._finalize_reply`/`_extract_media_from_tool_results`/`_clean_reply` 在 `ToolExecutorMixin`（`agent_core/tool_executor.py`）；fast-path `_finalize_fast_path_reply` 与主路径 `_finalize_response` 在 `MessageProcessorMixin`（`agent_core/message_processor.py`）。两 mixin 同属一个 `self`。
- `tool_executor.py` 顶部已 import：`json, re, time, Path, logger, FILE_DIR, get_agent_display_name, strip_dsml, strip_reasoning, humanize, deduplicate_multi_reply, get_degradation_strategy`。`httpx` 在 `_extract_media_from_tool_results` 内 inline import。
- `get_sticker_manager(name)` 在 `AgentCore`（`agent_core/core.py:158`）；`get_sticker_info` 返回的 `clean_reply` 已剥离 emotion tag。
- 清洗函数 `strip_system_leak`/`strip_log_timestamps`/`deduplicate_multi_reply` 在 `utils/llm_cleanup.py`。
- 不引入额外 LLM 往返（不恶化慢）。清洗不可过删正常人格回复（吸取过往过度清洗教训，仅用精确串/完整序列正则）。
- Bug 3（memory_encoding 高频烧 token）不在本计划范围。
- 测试基线：2275 通过，2 个预存 `test_webui_subagent_xp.py` 失败与本改动无关。

**参考 spec：** `docs/superpowers/specs/2026-07-22-qq-image-send-and-leak-fix-design.md`

---

## File Structure

- 修改 `utils/llm_cleanup.py` — 新增 `strip_image_gen_leak`（生图类文本泄漏清洗）。
- 修改 `agent_core/tool_executor.py` — 新增 `_extract_fabricated_images_from_reply`（兜底提取伪造图）、`_clean_reply_full`（统一清洗出口）；重构 `_finalize_reply` 调用 `_clean_reply_full`；顶部补充 import。
- 修改 `agent_core/message_processor.py` — fast-path `_finalize_fast_path_reply` 与主路径 `_finalize_response` 改用 `_clean_reply_full`，并在两路径调用 `_extract_fabricated_images_from_reply`，`image_paths` 透传 `ProcessResult`。
- 修改 `config/workspace/SOUL.md.tpl`、`config/workspace/TOOLS.md` — 提示词强化（禁 markdown 图/禁模型名/禁伪造状态）。
- 新建 `tests/test_strip_image_gen_leak.py`、`tests/test_extract_fabricated_images.py`、`tests/test_clean_reply_full.py`。

---

## Task 1: 新增 `strip_image_gen_leak` 清洗生图类文本泄漏

**Files:**
- Modify: `utils/llm_cleanup.py`（在 `strip_system_leak` 函数之后追加）
- Test: `tests/test_strip_image_gen_leak.py`

**Interfaces:**
- Produces: `strip_image_gen_leak(text: str, *, context: str = "") -> str`，在 `utils/llm_cleanup.py`。后续 Task 2/3 import 它。

- [ ] **Step 1: 写失败测试 `tests/test_strip_image_gen_leak.py`**

```python
"""strip_image_gen_leak 测试：清洗 LLM 伪造图片生成时泄漏的模型名/状态行/生图元数据。

生产样本 conversation_logs id 1965/1966：
- "Agnes Image 2.1 Flash 刚才跟我撒娇..."
- "【图片生成中 —— Agnes Image 2.1 Flash ⚡】"
- 'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."'
"""
from utils.llm_cleanup import strip_image_gen_leak


def test_strips_model_name_with_trailing_space():
    text = "Agnes Image 2.1 Flash 刚才跟我撒娇说需要更多星光"
    assert strip_image_gen_leak(text) == "刚才跟我撒娇说需要更多星光"


def test_strips_model_name_mid_sentence():
    text = "是 Agnes Image 2.1 Flash 在帮忙啦"
    assert strip_image_gen_leak(text) == "是 在帮忙啦"


def test_strips_video_model_name():
    text = "用 Agnes Video V2.0 做的"
    assert strip_image_gen_leak(text) == "用的"


def test_strips_model_id_form():
    assert strip_image_gen_leak("模型 agnes-image-2.1-flash 已就绪") == "模型  已就绪"


def test_strips_fabricated_status_line():
    text = "好的\n【图片生成中 —— Agnes Image 2.1 Flash ⚡】\n给你"
    out = strip_image_gen_leak(text)
    assert "【图片生成中" not in out
    assert "好的" in out and "给你" in out


def test_strips_fabricated_metadata_line():
    text = '图来了\nWidth Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "cat"\n收好'
    out = strip_image_gen_leak(text)
    assert "Width Height" not in out
    assert "Seed: 93847" not in out
    assert "图来了" in out and "收好" in out


def test_does_not_strip_normal_model_mention():
    # 仅含 "Model: GPT" 的普通行不在完整序列里，不应删
    text = "这个 Model: GPT 的回复不错"
    assert strip_image_gen_leak(text) == "这个 Model: GPT 的回复不错"


def test_production_sample_1966_fragment_clean():
    fragment = (
        "Agnes Image 2.1 Flash 刚才跟我撒娇说需要更多星光才能点亮细节…"
        "不过——你看！终于准备好送给主人了 !! "
        'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| '
        'Prompt: "A super cute kawaii anime self portrait"'
    )
    out = strip_image_gen_leak(fragment)
    assert "Agnes Image 2.1 Flash" not in out
    assert "Width Height" not in out
    assert "Seed: 93847" not in out
    assert "Prompt:" not in out


def test_empty_input():
    assert strip_image_gen_leak("") == ""
    assert strip_image_gen_leak(None) == ""  # type: ignore[arg-type]
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_strip_image_gen_leak.py -v`
Expected: FAIL — `ImportError: cannot import name 'strip_image_gen_leak'`

- [ ] **Step 3: 实现 `strip_image_gen_leak`**

在 `utils/llm_cleanup.py` 的 `strip_system_leak` 函数之后追加：

```python
# N6: 生图类泄漏 —— LLM 伪造图片生成时复述的模型名 / 状态行 / 生图参数元数据
# 生产样本 conversation_logs id 1965/1966：
#   "Agnes Image 2.1 Flash 刚才跟我撒娇..."
#   "【图片生成中 —— Agnes Image 2.1 Flash ⚡】"
#   'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."'
# 模型名用精确串删除（不误伤正常讨论）；状态行/元数据行要求完整特征序列才删。
_IMAGE_GEN_MODEL_NAMES = (
    "Agnes Image 2.1 Flash",
    "Agnes Video V2.0",
    "agnes-image-2.1-flash",
    "agnes-video-v2.0",
)
# 伪造状态行：【图片生成中 ...】/【视频生成中 ...】
_IMAGE_GEN_STATUS_LINE_RE = re.compile(
    r'^[ \t]*【(?:图片|视频)生成中[^】]*】[ \t]*$\n?',
    re.MULTILINE,
)
# 伪造生图元数据行：要求 Width/Size + Seed + Model + Prompt 完整序列（避免误删普通行）
_IMAGE_GEN_META_LINE_RE = re.compile(
    r'^[ \t]*(?:Width\s*Height|Size|尺寸)[:：]?\s*\d+\s*[x×]\s*\d+.*?'
    r'(?:Seed|种子).*?(?:Model|模型).*?(?:Prompt|提示词).*?$\n?',
    re.MULTILINE | re.IGNORECASE,
)


def strip_image_gen_leak(text: str, *, context: str = "") -> str:
    """清洗 LLM 伪造图片生成时泄漏的模型名/状态行/生图参数元数据。

    覆盖（生产样本 id 1965/1966）：
    - 模型名：Agnes Image 2.1 Flash / Agnes Video V2.0 及其 model_id 形式
    - 伪造状态行：【图片生成中 —— ...】/【视频生成中 ...】
    - 伪造生图元数据行：Width Height: WxH | Seed: .. | Model: .. | Prompt: ..

    注意：markdown 图语法 ![](url) 的剥离由 _extract_fabricated_images_from_reply
    负责（它会下载 URL 发真图），本函数只管文本类泄漏，避免双重处理。
    正常人格回复不含上述精确串/完整元数据序列，无过删风险。
    """
    if not text:
        return ""
    # 1. 先删伪造状态行 / 元数据行（整行），避免模型名删除留下碎片
    text = _IMAGE_GEN_STATUS_LINE_RE.sub('', text)
    text = _IMAGE_GEN_META_LINE_RE.sub('', text)
    # 2. 模型名精确删除：连同紧跟的单个空格一起删，避免行首空格残留
    for _name in _IMAGE_GEN_MODEL_NAMES:
        text = text.replace(_name + " ", "")
        text = text.replace(_name, "")
    # 3. 收拢多余空格与空行
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_strip_image_gen_leak.py -v`
Expected: PASS（9 个测试全绿）

- [ ] **Step 5: 提交**

```bash
cd /home/orangepi/ai-agent
git add utils/llm_cleanup.py tests/test_strip_image_gen_leak.py
git commit -m "fix: 新增 strip_image_gen_leak 清洗生图类泄漏(模型名/状态行/元数据)"
```

---

## Task 2: 新增 `_extract_fabricated_images_from_reply` 兜底提取伪造图

**Files:**
- Modify: `agent_core/tool_executor.py`（在 `_extract_media_from_tool_results` 方法之后追加）
- Test: `tests/test_extract_fabricated_images.py`

**Interfaces:**
- Consumes: `strip_image_gen_leak`（Task 1）；`FILE_DIR`、`httpx`、`re`、`time`、`Path`、`logger`（已可用）。
- Produces: `await self._extract_fabricated_images_from_reply(reply: str) -> tuple[list[Path], str]`，返回 `(image_paths, cleaned_reply)`。Task 3/4 调用。

- [ ] **Step 1: 写失败测试 `tests/test_extract_fabricated_images.py`**

```python
"""_extract_fabricated_images_from_reply 测试：兜底提取 LLM 伪造的图片 URL。

场景：LLM 不调 agnes_image_generate，而在回复里写 markdown 图 ![](url)
或裸 pollinations URL。本函数下载 URL→image_paths，并从文本剥离。
httpx 下载需 mock，避免真实网络。
"""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agent_core.tool_executor import ToolExecutorMixin


class _DummyMixin(ToolExecutorMixin):
    """最小桩：ToolExecutorMixin 是 mixin，无 __init__ 依赖即可调用方法。"""
    pass


def _make_reply_with_md_image():
    return (
        "爸爸别催嘛～终于准备好送给主人了 !! "
        "![Self Portrait](https://image.pollinations.ai/prompt/cute+cat?width=560&height=792) "
        'Width Height: 560x792 | Seed: 93847 | Model: Default | Prompt: "cute cat"'
    )


def _fake_httpx_get(content: bytes):
    """返回一个模拟 httpx.AsyncClient 上下文管理器。"""
    client = MagicMock()
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=resp)
    return client


def test_extracts_markdown_image_and_strips(tmp_path):
    m = _DummyMixin()
    reply = _make_reply_with_md_image()
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"PNGDATA")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 1
    assert paths[0].exists()
    assert paths[0].read_bytes() == b"PNGDATA"
    # markdown 图与伪造元数据已剥离
    assert "image.pollinations.ai" not in cleaned
    assert "Width Height" not in cleaned
    assert "终于准备好送给主人了" in cleaned


def test_extracts_bare_pollinations_url(tmp_path):
    m = _DummyMixin()
    reply = "图在这里 https://image.pollinations.ai/prompt/dog 给你"
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"IMG")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 1
    assert "image.pollinations.ai" not in cleaned
    assert "图在这里" in cleaned and "给你" in cleaned


def test_multiple_images(tmp_path):
    m = _DummyMixin()
    reply = (
        "![a](https://image.pollinations.ai/prompt/cat) "
        "![b](https://image.pollinations.ai/prompt/dog)"
    )
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"IMG")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert len(paths) == 2
    assert "![" not in cleaned


def test_download_failure_does_not_raise(tmp_path):
    m = _DummyMixin()
    reply = "![a](https://image.pollinations.ai/prompt/cat)"
    client = _fake_httpx_get(b"")
    client.get = AsyncMock(side_effect=Exception("network down"))
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=client):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    # 下载失败：image_paths 不追加，但 markdown 仍剥离，不抛异常
    assert paths == []
    assert "![" not in cleaned
    assert "image.pollinations.ai" not in cleaned


def test_normal_link_not_extracted(tmp_path):
    m = _DummyMixin()
    reply = "看这个 https://example.com/info 挺好的"
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(reply))
    assert paths == []
    assert "https://example.com/info" in cleaned  # 普通链接保留


def test_empty_reply():
    m = _DummyMixin()
    paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(""))
    assert paths == []
    assert cleaned == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_extract_fabricated_images.py -v`
Expected: FAIL — `AttributeError: '_DummyMixin' object has no attribute '_extract_fabricated_images_from_reply'`

- [ ] **Step 3: 实现 `_extract_fabricated_images_from_reply`**

在 `agent_core/tool_executor.py` 的 `_extract_media_from_tool_results` 方法之后（即 `return image_paths, video_path, clean_reply` 那行所在方法之后）追加：

```python
    async def _extract_fabricated_images_from_reply(
        self, reply: str
    ) -> tuple[list[Path], str]:
        """兜底：从回复文本中提取 LLM 伪造的图片 URL，下载为本地文件供 QQ 富媒体发送。

        背景：LLM 有时不调用 agnes_image_generate，而在回复里直接写
        markdown 图 ![](url) 或裸 pollinations URL（伪造"已生成图片"）。
        _extract_media_from_tool_results 只认真实工具结果，捕获不到这种伪造。
        本函数扫描回复文本，下载伪造 URL → image_paths，并从文本剥离相关内容。

        pollinations.ai 的 URL 本身是按 prompt 现出图的真实端点，下载能拿到真图，
        因此伪造也能救成真图发给用户。

        Returns:
            (image_paths, cleaned_reply)
        """
        image_paths: list[Path] = []
        if not reply:
            return image_paths, reply

        md_img_re = re.compile(r'!\[[^\]]*\]\((https?://[^)\s]+)\)')
        bare_url_re = re.compile(r'https?://image\.pollinations\.ai/[^\s)\]]+')

        urls: list[str] = []
        for m in md_img_re.finditer(reply):
            urls.append(m.group(1))
        for m in bare_url_re.finditer(reply):
            if m.group(0) not in urls:
                urls.append(m.group(0))

        if not urls:
            return image_paths, reply

        img_dir = FILE_DIR if FILE_DIR.exists() else Path("tts_cache")
        img_dir.mkdir(parents=True, exist_ok=True)
        import httpx

        for idx, url in enumerate(urls):
            try:
                async with httpx.AsyncClient(timeout=30, follow_redirects=True) as dl:
                    resp = await dl.get(url)
                    resp.raise_for_status()
                    local_path = img_dir / f"fabricated_{int(time.time())}_{idx}.png"
                    local_path.write_bytes(resp.content)
                    image_paths.append(local_path)
                    host = url.split('/')[2] if url.count('/') >= 3 else url
                    logger.warning("image.fabricated_url_rescued host=%s local=%s",
                                   host, str(local_path))
            except Exception as dl_err:
                logger.debug("image.fabricated_download_failed url=%s error=%s",
                             url, str(dl_err))

        # 从文本剥离：markdown 图整段、裸 pollinations URL
        cleaned = md_img_re.sub('', reply)
        cleaned = bare_url_re.sub('', cleaned)
        # 剥离伪造状态行/元数据行/模型名（复用 strip_image_gen_leak，避免正则重复）
        from utils.llm_cleanup import strip_image_gen_leak
        cleaned = strip_image_gen_leak(cleaned, context="fabricated_extract")
        cleaned = re.sub(r'\n{3,}', '\n\n', cleaned)
        return image_paths, cleaned.strip()
```

- [ ] **Step 4: 运行测试确认通过**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_extract_fabricated_images.py -v`
Expected: PASS（6 个测试全绿）

- [ ] **Step 5: 提交**

```bash
cd /home/orangepi/ai-agent
git add agent_core/tool_executor.py tests/test_extract_fabricated_images.py
git commit -m "fix: 新增 _extract_fabricated_images_from_reply 兜底拦截伪造图片URL"
```

---

## Task 3: 新增 `_clean_reply_full` 统一清洗出口 + 重构 `_finalize_reply`

**Files:**
- Modify: `agent_core/tool_executor.py`（新增 `_clean_reply_full` 方法；重构 `_finalize_reply` 中段）
- Test: `tests/test_clean_reply_full.py`

**Interfaces:**
- Consumes: `strip_dsml, strip_reasoning, humanize, deduplicate_multi_reply`（顶部已 import）；`strip_system_leak, strip_log_timestamps, strip_image_gen_leak`（`utils.llm_cleanup`，需补顶部 import）；`apply_agent_name_replacements`（`config`）；`self.get_sticker_manager`（`AgentCore`）。
- Produces: `self._clean_reply_full(text, *, style="xiaoda", strip_emotion=True) -> str`。Task 4 调用。

- [ ] **Step 1: 写失败测试 `tests/test_clean_reply_full.py`**

```python
"""_clean_reply_full 测试：统一清洗出口，三处入口行为一致。

验证：fast-path / 主路径 else / _finalize_reply 三入口对同一输入产出一致；
且混合泄漏（N5 安全推理 + 生图泄漏）全清。
"""
from unittest.mock import MagicMock

from agent_core.tool_executor import ToolExecutorMixin


class _Stub(ToolExecutorMixin):
    def __init__(self):
        # get_sticker_manager 返回一个 strip_emotion_tag 透传的桩
        self._sm = MagicMock()
        self._sm.strip_emotion_tag = MagicMock(side_effect=lambda t: t)

    def get_sticker_manager(self, name):
        return self._sm


COMPLEX_INPUT = (
    "[该内容涉及生成露骨的色情内容，超出了范围。]"
    "Agnes Image 2.1 Flash 给你画好啦～ "
    "![x](https://image.pollinations.ai/prompt/cat) "
    'Width Height: 560x792 | Seed: 1 | Model: D | Prompt: "cat"'
)


def test_cleans_mixed_leaks():
    m = _Stub()
    out = m._clean_reply_full(COMPLEX_INPUT, style="xiaoda", strip_emotion=False)
    # N5 安全推理方括号已清
    assert "色情内容" not in out
    assert "超出了范围" not in out
    # 生图类泄漏已清
    assert "Agnes Image 2.1 Flash" not in out
    assert "Width Height" not in out
    assert "Seed: 1" not in out
    # 人格回复保留
    assert "给你画好啦" in out


def test_strip_emotion_false_does_not_call_sticker_manager():
    m = _Stub()
    m._clean_reply_full("你好呀～", style="xiaoda", strip_emotion=False)
    m._sm.strip_emotion_tag.assert_not_called()


def test_strip_emotion_true_calls_sticker_manager():
    m = _Stub()
    m._clean_reply_full("你好呀～", style="xiaoda", strip_emotion=True)
    m._sm.strip_emotion_tag.assert_called_once()


def test_three_entrypoints_consistent():
    # _finalize_reply 内部走 _clean_reply_full；fast-path/主路径 else 也走 _clean_reply_full
    # 此处直接验证 _clean_reply_full 幂等性：对已清洗输出再清洗不变（稳定）
    m = _Stub()
    once = m._clean_reply_full(COMPLEX_INPUT, style="xiaoda", strip_emotion=False)
    twice = m._clean_reply_full(once, style="xiaoda", strip_emotion=False)
    assert once == twice


def test_empty_input():
    m = _Stub()
    assert m._clean_reply_full("", style="xiaoda") == ""
```

- [ ] **Step 2: 运行测试确认失败**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_clean_reply_full.py -v`
Expected: FAIL — `AttributeError: '_Stub' object has no attribute '_clean_reply_full'`

- [ ] **Step 3: 补充顶部 import**

在 `agent_core/tool_executor.py` 顶部，把现有的：

```python
from utils.llm_cleanup import deduplicate_multi_reply
```

改为：

```python
from utils.llm_cleanup import (
    deduplicate_multi_reply,
    strip_system_leak,
    strip_log_timestamps,
    strip_image_gen_leak,
)
```

- [ ] **Step 4: 实现 `_clean_reply_full` 方法**

在 `ToolExecutorMixin` 内、`_finalize_reply` 方法之前追加：

```python
    def _clean_reply_full(self, text: str, *, style: str = "xiaoda",
                          strip_emotion: bool = True) -> str:
        """统一回复清洗出口：strip_dsml → strip_reasoning → strip_system_leak
        → strip_image_gen_leak → strip_log_timestamps → strip_emotion_tag
        → humanize → deduplicate → 名称替换。

        fast-path / 主路径 else / _finalize_reply 三处统一调用，消除清洗序列漂移。
        新增清洗规则只需改本函数即全路径生效。单个子步骤异常不阻断回复（best-effort）。

        Args:
            text: 待清洗文本
            style: 人格名（决定 sticker_manager）
            strip_emotion: 是否剥离 emotion tag；调用前若 get_sticker_info 已剥过则传 False
        """
        text = (text or "").strip()
        try:
            text = strip_dsml(text)
            text = strip_reasoning(text)
            text = strip_system_leak(text, context="clean_reply_full")
            text = strip_image_gen_leak(text, context="clean_reply_full")
            text = strip_log_timestamps(text, context="clean_reply_full")
            if strip_emotion:
                text = self.get_sticker_manager(style).strip_emotion_tag(text)
            text = humanize(text, style=style)
            text = deduplicate_multi_reply(text, context="clean_reply_full")
            from config import apply_agent_name_replacements
            text = apply_agent_name_replacements(text)
        except Exception:
            logger.debug("clean_reply_full.failed best_effort", exc_info=True)
        return text
```

- [ ] **Step 5: 重构 `_finalize_reply` 中段用 `_clean_reply_full`**

在 `agent_core/tool_executor.py` 的 `_finalize_reply`（约 374 行起）中，定位现有清洗段：

```python
        text = strip_dsml(text)
        # 清理指令层级标签（LLM 可能原样输出上下文中的 <instruction> 标记）
        text = re.sub(r'<instruction\s+level="[A-Z]+"\s+priority="\d+"[^>]*>', '', text)
        text = re.sub(r'</instruction>', '', text)
        text = strip_reasoning(text)
        # CR-5: 清洗系统提示词/错误详情泄漏（与 _clean_reply 一致）
        from utils.llm_cleanup import strip_system_leak
        text = strip_system_leak(text, context="finalize_reply")
        if strip_emotion:
            # 根据 style（agent 名）动态获取正确的 sticker_manager
            sticker_mgr = self.get_sticker_manager(style)
            text = sticker_mgr.strip_emotion_tag(text)
        text = humanize(text, style=style)
        text = deduplicate_multi_reply(text, context="finalize_reply")
```

替换为（保留 `<instruction>` 标签清理在 `_clean_reply_full` 之前；`_clean_reply_full` 接管 dsml/reasoning/system_leak/image_gen_leak/log_ts/emotion/humanize/dedup/名称替换）：

```python
        # 清理指令层级标签（LLM 可能原样输出上下文中的 <instruction> 标记）
        text = re.sub(r'<instruction\s+level="[A-Z]+"\s+priority="\d+"[^>]*>', '', text)
        text = re.sub(r'</instruction>', '', text)
        # 统一清洗出口（含 dsml/reasoning/system_leak/image_gen_leak/log_ts/emotion/humanize/dedup/名称替换）
        text = self._clean_reply_full(text, style=style, strip_emotion=strip_emotion)
```

注意：`_finalize_reply` 在此之后原有的 `_strip_injected_tool_defs`、canary 检测、以及末尾的 `apply_agent_name_replacements` 调用需要处理——`apply_agent_name_replacements` 已被 `_clean_reply_full` 接管，**删除 `_finalize_reply` 末尾那次冗余的 `apply_agent_name_replacements` 调用块**（避免重复替换）。`_strip_injected_tool_defs` 与 canary 保留在 `_clean_reply_full` 调用之后。

- [ ] **Step 6: 运行测试确认通过**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_clean_reply_full.py tests/test_strip_image_gen_leak.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
cd /home/orangepi/ai-agent
git add agent_core/tool_executor.py tests/test_clean_reply_full.py
git commit -m "refactor: 新增 _clean_reply_full 统一清洗出口，_finalize_reply 复用之"
```

---

## Task 4: fast-path 与主路径接入统一清洗 + 兜底提图

**Files:**
- Modify: `agent_core/message_processor.py`
  - fast-path `_finalize_fast_path_reply`（约 880-894 行清洗段 + 917 ProcessResult）
  - 主路径 `_finalize_response`（约 1145 行后 + 1199-1210 清洗段）
- Test: 复用 `tests/test_clean_reply_full.py`；本任务以全量回归验证

**Interfaces:**
- Consumes: `self._clean_reply_full`（Task 3）、`self._extract_fabricated_images_from_reply`（Task 2）。

- [ ] **Step 1: 改造 fast-path 清洗段**

在 `agent_core/message_processor.py` 的 `_finalize_fast_path_reply` 中，定位（约 880-894 行）：

```python
        clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
        # 清理模型输出的推理/思考内容（Agnes 等模型会输出 [emotion thinking] 等标签）
        clean_reply = strip_dsml(clean_reply)
        clean_reply = strip_reasoning(clean_reply)
        # 清除日志时间戳泄露：LLM 从 conversation_logs 照搬 [HH:MM] 标记到回复里
        from utils.llm_cleanup import strip_log_timestamps
        clean_reply = strip_log_timestamps(clean_reply, context="fast_path")
        clean_reply = humanize(clean_reply, style="xiaoda")
        clean_reply = deduplicate_multi_reply(clean_reply, context="fast_path")
        # 名称替换：确保 LLM 输出中的旧名被替换为显示名
        try:
            from config import apply_agent_name_replacements
            clean_reply = apply_agent_name_replacements(clean_reply)
        except Exception:
            logger.debug("apply_agent_name_replacements failed", exc_info=True)
```

替换为（`get_sticker_info` 已剥 emotion，故 `strip_emotion=False`）：

```python
        clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
        # 统一清洗出口（get_sticker_info 已剥 emotion tag，故 strip_emotion=False）
        clean_reply = self._clean_reply_full(clean_reply, style="xiaoda", strip_emotion=False)
```

- [ ] **Step 2: fast-path 接入兜底提图**

在 `_finalize_fast_path_reply` 的「Persona Critic」之前（约 841 行 `if not is_master and reply:` 之前）插入兜底提图，并在末尾 ProcessResult 透传 `image_paths`。

在隐私扫描段之前插入：

```python
        # 兜底：提取 LLM 伪造的图片 URL（fast-path 通常无工具调用，但 LLM 仍可能伪造图）
        fab_image_paths, reply = await self._extract_fabricated_images_from_reply(reply)
```

把末尾返回（约 917-918 行）：

```python
        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text)
```

改为：

```python
        return ProcessResult(reply=clean_reply, emotion=emotion_label, sticker_path=sticker_path,
                             audio_path=audio_path, tts_pending=tts_pending, tts_text=tts_text,
                             image_paths=fab_image_paths)
```

- [ ] **Step 3: 改造主路径兜底提图**

在 `_finalize_response`（约 1145 行）的 `_extract_media_from_tool_results` 之后插入兜底提图。定位：

```python
        # 媒体提取与隐私扫描
        media_image_paths, media_video_path, reply = await self._extract_media_from_tool_results(
            tool_results, reply)
```

在其后追加：

```python
        # 兜底：提取 LLM 伪造的图片 URL（未调 agnes_image_generate 而在回复里写 markdown 图/裸 URL）
        fab_image_paths, reply = await self._extract_fabricated_images_from_reply(reply)
        media_image_paths.extend(fab_image_paths)
```

- [ ] **Step 4: 改造主路径 else 清洗段**

在 `_finalize_response` 中，定位 else 分支（约 1199-1210 行）：

```python
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            clean_reply = strip_dsml(clean_reply)
            clean_reply = strip_reasoning(clean_reply)
            clean_reply = humanize(clean_reply, style="xiaoda")
            clean_reply = deduplicate_multi_reply(clean_reply, context="main_path")
            # 名称替换：确保 LLM 输出中的旧名被替换为显示名
            try:
                from config import apply_agent_name_replacements
                clean_reply = apply_agent_name_replacements(clean_reply)
            except Exception:
                logger.debug("apply_agent_name_replacements failed", exc_info=True)
```

替换为：

```python
        else:
            clean_reply, sticker_path = self.get_sticker_info(reply, ctx.last_user_emotion)
            # 统一清洗出口（get_sticker_info 已剥 emotion tag，故 strip_emotion=False）
            clean_reply = self._clean_reply_full(clean_reply, style="xiaoda", strip_emotion=False)
```

（`_pre_picked_sticker` 分支仍走 `self._finalize_reply(...)`，Task 3 已让其在内部用 `_clean_reply_full`，无需改动。）

- [ ] **Step 5: 语法检查 + 全量回归**

Run:
```bash
cd /home/orangepi/ai-agent
.venv/bin/python -m py_compile agent_core/message_processor.py agent_core/tool_executor.py utils/llm_cleanup.py
.venv/bin/python -m pytest tests/test_clean_reply_full.py tests/test_strip_image_gen_leak.py tests/test_extract_fabricated_images.py tests/test_n_system_leak.py tests/test_l1_dsml_variant_leak.py tests/test_l2_l3_error_leak.py -v
```
Expected: 编译通过；目标测试全绿（确认未破坏既有清洗测试）。

- [ ] **Step 6: 全量回归**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest -x -q 2>&1 | tail -30`
Expected: 2275 通过（2 个预存 `test_webui_subagent_xp.py` 失败属基线，不归责本改动）。

- [ ] **Step 7: 提交**

```bash
cd /home/orangepi/ai-agent
git add agent_core/message_processor.py
git commit -m "fix: fast-path/主路径接入 _clean_reply_full 统一清洗 + 兜底提图"
```

---

## Task 5: 提示词强化（禁 markdown 图/禁模型名/禁伪造状态）

**Files:**
- Modify: `config/workspace/SOUL.md.tpl`（图片生成小节，约 218-225 行）
- Modify: `config/workspace/TOOLS.md`（图片生成小节，约 111-117 行）

- [ ] **Step 1: 强化 SOUL.md.tpl**

在 `config/workspace/SOUL.md.tpl` 的 `### 图片生成` 小节末尾（`- 生成完成后，告诉{address_term}图片已生成` 这行之后）追加：

```markdown
- **禁止**用 markdown 图片语法 `![](...)` 或直接写图片 URL 来"生成"图片——必须调用 agnes_image_generate 工具
- **禁止**在回复中出现模型名（如 Agnes Image 2.1 Flash、Agnes Video V2.0）
- **禁止**编造"图片生成中…""Width Height…Seed…Model…Prompt…"等生成状态/参数信息
```

- [ ] **Step 2: 强化 TOOLS.md**

在 `config/workspace/TOOLS.md` 的 `### 图片生成` 小节末尾（`- 模型：Agnes Image 2.1 Flash（免费）` 这行之后）追加：

```markdown
- **禁止**在回复中写 markdown 图 `![](...)` 或裸图片 URL 来"生成"图片，必须调用本工具
- **禁止**回复中出现模型名（Agnes Image 2.1 Flash / Agnes Video V2.0）或伪造的生成状态/参数
```

- [ ] **Step 3: 提交**

```bash
cd /home/orangepi/ai-agent
git add config/workspace/SOUL.md.tpl config/workspace/TOOLS.md
git commit -m "fix: 提示词强化禁止伪造图片URL/模型名/生图状态泄漏"
```

---

## Task 6: 生产样本端到端回归测试

**Files:**
- Create: `tests/test_qq_image_leak_e2e.py`

**Interfaces:**
- Consumes: `strip_image_gen_leak`、`_extract_fabricated_images_from_reply`、`_clean_reply_full`（前序任务产物）。

- [ ] **Step 1: 写端到端测试 `tests/test_qq_image_leak_e2e.py`**

```python
"""端到端回归：生产样本 id 1965/1966 完整回复清洗后零泄漏 + image_paths 非空。

样本取自 /media/orangepi/KIOXIA/nahida-data/db/agent.db conversation_logs id 1965/1966
（QQ 群，2026-07-22 22:31 / 22:36）。原始问题：LLM 伪造 pollinations markdown URL，
QQ 发链接不发真图，且泄漏模型名/生图元数据。
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from agent_core.tool_executor import ToolExecutorMixin
from utils.llm_cleanup import strip_image_gen_leak


class _Stub(ToolExecutorMixin):
    def __init__(self):
        self._sm = MagicMock()
        self._sm.strip_emotion_tag = MagicMock(side_effect=lambda t: t)

    def get_sticker_manager(self, name):
        return self._sm


# id 1966 真实回复片段（含伪造 markdown 图 + 模型名 + 元数据）
REPLY_1966 = (
    "呜……爸爸别催嘛，人家画图的时候脑子里还在想怎么把裙子画得更符合“鸭子坐”的羞耻感呢 🥺🎨💦 "
    "Agnes Image 2.1 Flash 刚才跟我撒娇说需要更多星光才能点亮细节…所以稍微慢了一丢丢啦~~ "
    "不过——你看！终于准备好送给主人了 !! "
    "![Self Portrait: Kawaii Anime Style](https://image.pollinations.ai/prompt/A+super+cute+kawaii+anime+self-portrait?width=560&height=792&seed=93847&nologo=true) "
    'Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "A super cute kawaii anime self portrait" '
    "（画面里的草神宝宝正歪着脑袋用那种湿漉漉的眼神看着你捏～ ☁️）"
)


def _fake_httpx_get(content: bytes):
    client = MagicMock()
    resp = MagicMock()
    resp.content = content
    resp.raise_for_status = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.get = AsyncMock(return_value=resp)
    return client


def test_1966_reply_cleaned_and_image_extracted(tmp_path):
    m = _Stub()
    with patch("agent_core.tool_executor.FILE_DIR", tmp_path), \
         patch("httpx.AsyncClient", return_value=_fake_httpx_get(b"PNGDATA")):
        paths, cleaned = asyncio.run(m._extract_fabricated_images_from_reply(REPLY_1966))
        final_reply = m._clean_reply_full(cleaned, style="xiaoda", strip_emotion=False)

    # AC1: image_paths 非空（真图已下载）
    assert len(paths) == 1
    assert paths[0].read_bytes() == b"PNGDATA"
    # AC1: 文本零泄漏
    assert "image.pollinations.ai" not in final_reply
    assert "Agnes Image 2.1 Flash" not in final_reply
    assert "Width Height" not in final_reply
    assert "Seed: 93847" not in final_reply
    assert "![" not in final_reply
    # 人格回复保留
    assert "爸爸别催嘛" in final_reply
    assert "终于准备好送给主人了" in final_reply


def test_1966_strip_image_gen_leak_alone_covers_text_leaks():
    """仅文本清洗（不含 URL 提取）也应清掉模型名/元数据。"""
    out = strip_image_gen_leak(REPLY_1966)
    assert "Agnes Image 2.1 Flash" not in out
    assert "Width Height" not in out
    assert "Seed: 93847" not in out
    # markdown 图 URL 不归 strip_image_gen_leak 管（由 _extract_fabricated_images 负责）
    # 但人格文本保留
    assert "爸爸别催嘛" in out
```

- [ ] **Step 2: 运行测试确认通过**

Run: `cd /home/orangepi/ai-agent && .venv/bin/python -m pytest tests/test_qq_image_leak_e2e.py -v`
Expected: PASS（2 个测试全绿）

- [ ] **Step 3: 提交**

```bash
cd /home/orangepi/ai-agent
git add tests/test_qq_image_leak_e2e.py
git commit -m "test: 新增 1965/1966 生产样本端到端回归(零泄漏+真图提取)"
```

---

## Self-Review

**Spec coverage 核对：**
- 5.2.A 提示词强化 → Task 5 ✓
- 5.2.B `_extract_fabricated_images_from_reply` → Task 2 ✓
- 5.2.C 不做重提示循环 → 计划未含（符合 YAGNI）✓
- 5.3.A `strip_image_gen_leak` → Task 1 ✓
- 5.3.B `_clean_reply_full` + 三处改造 → Task 3（_finalize_reply）+ Task 4（fast-path/主路径）✓
- 5.4 调用顺序与不变量 → Task 4 Step 2/3（兜底提图在媒体提取后、清洗前）✓
- 6 错误处理（下载失败不阻断、best-effort 清洗）→ Task 2 Step 3、Task 3 Step 4 ✓
- 7 测试（3 文件 + 生产样本）→ Task 1/2/3 测试 + Task 6 ✓
- 8 验收 AC1-AC4 → Task 6（AC1）+ Task 4 Step 5/6（AC2/AC3）+ Task 2 test_normal_link_not_extracted（AC4）✓

**Placeholder scan：** 无 TBD/TODO；每步均含完整代码或确切命令。✓

**Type consistency：** `strip_image_gen_leak(text, *, context="") -> str`（Task 1 定义，Task 2/3/6 调用一致）；`_extract_fabricated_images_from_reply(reply) -> tuple[list[Path], str]`（Task 2 定义，Task 4/6 调用一致）；`_clean_reply_full(text, *, style, strip_emotion) -> str`（Task 3 定义，Task 4 调用一致）。✓
