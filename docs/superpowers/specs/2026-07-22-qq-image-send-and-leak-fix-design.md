# QQ 图片真实发送 + 生图类泄漏根治 设计

- 日期：2026-07-22
- 状态：待实现
- 范围：QQ 通道图片发送 bug + 生图类泄漏根治（不含 memory_encoding 性能问题，后者单独 spec）

## 1. 背景与问题

生产数据库 `/media/orangepi/KIOXIA/nahida-data/db/agent.db` 中 `conversation_logs` id 1965/1966（2026-07-22 22:31、22:36，qq_group）暴露三个问题：

1. **图片发成链接**：用户要求生成自画像，助手回复里给出 `![...](https://image.pollinations.ai/...)` 的 markdown 文本 + 裸 URL，用户在 QQ 里只看到链接，没收到真图。
2. **大量泄漏**：同一回复里泄漏了模型名 `Agnes Image 2.1 Flash`、markdown 图语法、伪造生图元数据 `Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."`。
3. **反应慢**：用户跨两轮 5 分钟未收到真图，体感"慢"。`api_usage` 显示后台 `memory_encoding` 任务每 ~13 秒触发一次、每次烧 ~30000 prompt tokens，与聊天请求抢资源。（问题 3 本 spec 不修，单列后续 spec。）

## 2. 根因分析

### 2.1 图片发成链接（Bug 1）

LLM **没有调用** `agnes_image_generate` 工具，而是在人格回复里**凭空伪造**了一个 `image.pollinations.ai` 的 markdown URL（pollinations.ai 是模型训练数据里已知的免费现出图端点）。

证据：`media_tasks` 表仅有 1 条记录（2026-06-12 一只小猫），id 1965/1966 无对应 `media_tasks` 行 → 未发生真实工具调用。

代码层面：
- `tools/agnes_tools.py:125` 工具返回文本 `图片URL: {url}`。
- `agent_core/tool_executor.py:137-221` `_extract_media_from_tool_results` **只扫描真实工具结果**（`tool_results`）里的 `图片URL:`/`图片已保存到:`，下载后塞入 `image_paths`。LLM 未调工具 → 无 tool_results → `image_paths` 为空。
- `qq_bot_adapter.py:1552` `_gather_media_send_tasks` 仅当 `result.image_paths` 非空才发真图；否则整段含 URL 的 markdown 被当纯文本发出 → 用户看到链接。

注：`agnes_image_generate` 工具本身在 `tools/_builtin_manifest.py:442` 已注册，工具路径（调了 agnes）是通的——问题纯粹是 LLM 伪造而非调用。

### 2.2 泄漏未解决（Bug 2）

- `Agnes Image 2.1 Flash` 直接来自系统提示词 `config/workspace/SOUL.md.tpl:221` 与 `TOOLS.md:117`，被 LLM 原样复述。
- 现有清洗 `strip_system_leak`（`utils/llm_cleanup.py:238`）只覆盖 N2-N5（错误详情 / 系统提示词结构块 / 系统指示措辞 / 方括号安全推理），**不覆盖**模型名、markdown 图、伪造生图元数据。
- 三处清洗入口漂移：fast-path（`message_processor.py:881-919`）、主路径 else 分支（`message_processor.py:1199-1210`）**均未调用 `strip_system_leak`**；只有 `_pre_picked_sticker` 命中时走 `_finalize_reply`（`tool_executor.py:374`，其中 `:404-405` 调了 `strip_system_leak`）。因此即便 N2-N5 类泄漏在 fast-path 也可能漏。

## 3. 设计目标

- G1：用户要图时，QQ 侧始终收到**真实图片消息**（非链接、非 markdown 文本）。
- G2：助手回复中**零泄漏**模型名、markdown 图语法、伪造生图元数据；且 N2-N5 类泄漏在所有回复路径一致清洗。
- G3：不引入额外 LLM 往返（不恶化 Bug 3 的慢）。
- G4：清洗不过删正常人格回复（吸取过往过度清洗误删教训）。

## 4. 方案选型

已与用户确认：
- Bug 1 采用**双保险**：提示词强化引导真实工具调用 + 代码兜底拦截伪造 URL。
- Bug 2 采用**全面根治**：扩展清洗器 + 统一三处清洗入口。
- Bug 3（memory_encoding 性能）**单独后续 spec**，不在本 spec 范围。

放弃方案：①「只强制真实工具调用」——LLM 仍伪造时用户仍收链接+泄漏；②「只兜底拦截」——不解决根因，图来自 pollinations 而非 agnes；③「重提示 LLM 再调工具」强制循环——加一轮 LLM 往返，恶化慢，YAGNI。

## 5. 详细设计

### 5.1 总体数据流

LLM 回复生成后、发送 QQ 前，统一经过：

```
LLM 回复 reply
  ├─ [现有] _extract_media_from_tool_results(tool_results, reply)   # 只认真实工具结果
  │        → 真实 agnes 调用：下载 agnes URL → image_paths；从 reply 剥 "图片URL:" 行
  ├─ [新增] _extract_fabricated_images_from_reply(reply)            # 兜底：扫回复文本伪造图
  │        → 伪造 markdown 图 / pollinations URL：下载 → image_paths；从 reply 剥 markdown + 元数据
  └─ [新增] _clean_reply_full(text, style, strip_emotion)           # 统一清洗出口
           strip_dsml → strip_reasoning → strip_system_leak → strip_image_gen_leak(新)
           → strip_log_timestamps → humanize → deduplicate → 名称替换
```

最终 `ProcessResult.image_paths` 非空 → `qq_bot_adapter._gather_media_send_tasks` 走现有 `_send_reply_with_media` 发真图（`msg_type=7` 富媒体）。无需改动 QQ 适配器发送逻辑。

### 5.2 Bug 1 修复：双保险

#### 5.2.A 提示词强化（引导层）

`config/workspace/SOUL.md.tpl` 图片生成小节（约 218-225 行）与 `config/workspace/TOOLS.md`（约 111-117 行）追加约束：

- 「禁止用 markdown 图片语法 `![](...)` 或直接写图片 URL 来'生成'图片，必须调用 `agnes_image_generate` 工具。」
- 「回复中不得出现模型名（如 Agnes Image 2.1 Flash、Agnes Video V2.0）。」
- 「不要在回复中编造'图片生成中…''Width Height…Seed…Model…Prompt…'等生成状态/参数信息。」

此层为引导，不作为可靠性依赖。

#### 5.2.B 代码兜底：`_extract_fabricated_images_from_reply`（可靠性层）

新增于 `agent_core/tool_executor.py`，与 `_extract_media_from_tool_results` 同文件。

**职责**：扫描回复文本，提取伪造图片 URL → 下载 → 追加 `image_paths`，并从文本剥离相关 markdown/URL/元数据。

**匹配规则**（两类图源）：
1. markdown 图：`r'!\[[^\]]*\]\((https?://[^)\s]+)\)'`
2. 裸 pollinations URL：`r'https?://image\.pollinations\.ai/[^\s)\]]+'`

（注：裸 URL 仅匹配 pollinations 域，避免误抓正常链接；markdown 图匹配所有域名，因为人格回复里本就不该出现 markdown 图。）

**下载**：复用 httpx 异步下载，落盘 `FILE_DIR/fabricated_<ts>_<i>.png`（`FILE_DIR` 不可用时回退 `tts_cache/`）。下载超时 30s，`follow_redirects=True`。

**关键**：`image.pollinations.ai/prompt/<文本>` 是按 prompt 现出图的真实端点，下载能拿到真图——伪造也能救成真图发给用户。

**文本剥离**：删除命中的 markdown 图整段、裸 URL；并删除紧随 URL 之后同一逻辑块的伪造元数据行（见 5.3 的元数据正则）。剥离后清理 `\n{3,}` → `\n\n`。

**失败处理**：单个 URL 下载失败 → 剥掉该 markdown/URL，记 `image.fabricated_download_failed`（debug），不阻断；`image_paths` 不追加该项。回复照常发送（无图但文本干净）。

**遥测**：命中伪造图时记 `image.fabricated_url_rescued`（warning，含 url 域名），用于监控提示词强化后的失效频率。若长期高频，再考虑加重强制手段。

**返回**：`(image_paths, cleaned_reply)`，与 `_extract_media_from_tool_results` 返回形态对齐。

**调用点**：主路径（`message_processor.py:1145` 之后）与 fast-path（`:881` 区域）均调用，结果并入 `media_image_paths` 与 `reply`。封装为 AgentCore 方法以便两路径复用。

#### 5.2.C 不做"重提示 LLM 再调工具"强制循环

理由：加一轮 LLM 往返直接恶化 Bug 3（慢）；提示词引导 + 代码兜底已覆盖两种情形（调了走真 agnes，没调走兜底），用户两种情况都收到真图。YAGNI。

### 5.3 Bug 2 修复：泄漏全面根治

#### 5.3.A 新增 `strip_image_gen_leak`

新增于 `utils/llm_cleanup.py`（与 `strip_system_leak` 同文件，风格一致）。

**覆盖三类文本泄漏**（markdown 图剥离归 5.2.B 兜底，本函数只管文本类，避免双重处理）：

1. **模型名泄漏**（精确串，不误伤）：
   - `Agnes Image 2.1 Flash`、`Agnes Video V2.0`、`agnes-image-2.1-flash`、`agnes-video-v2.0`
   - 处理：整词删除（含可能的前后装饰符 `⚡` 等），保留句子其余部分；删除后产生的多余空格收拢。

2. **伪造状态行**：
   - `r'【图片生成中[^】]*】'`、`r'【视频生成中[^】]*】'`
   - 整行删除。

3. **伪造生图元数据行**（要求完整序列才删，避免误删正常含 Model/Prompt 的文本）：
   - `r'^[ \t]*(?:Width\s*Height|Size|尺寸)[:：]?\s*\d+\s*[x×]\s*\d+.*?(?:Seed|种子).*(?:Model|模型).*(?:Prompt|提示词).*?$'`（MULTILINE）
   - 整行删除。

末尾 `re.sub(r'\n{3,}', '\n\n', text).strip()`。

**函数签名**：`def strip_image_gen_leak(text: str, *, context: str = "") -> str:`

#### 5.3.B 收敛三处清洗序列为 `_clean_reply_full`

现状：fast-path、主路径 else、`_finalize_reply` 三套近似但漂移的清洗序列，是 N2-N5 在 fast-path 漏的根因。

**新增共享函数** `AgentCore._clean_reply_full(self, text, *, style="xiaoda", strip_emotion=True)`：

```
text = strip_dsml(text)
text = strip_reasoning(text)
text = strip_system_leak(text, context="clean_reply_full")
text = strip_image_gen_leak(text, context="clean_reply_full")
text = strip_log_timestamps(text, context="clean_reply_full")
if strip_emotion:
    text = self.get_sticker_manager(style).strip_emotion_tag(text)
text = humanize(text, style=style)
text = deduplicate_multi_reply(text, context="clean_reply_full")
text = apply_agent_name_replacements(text)
return text
```

**三处改造**（关键：`get_sticker_info` 返回的 `clean_reply` 已剥离 emotion tag，故其后再调 `_clean_reply_full` 须传 `strip_emotion=False` 避免重复剥离；`_finalize_reply` 接收原始 reply，按其自身 `strip_emotion` 形参透传）：
- fast-path（`message_processor.py:880-919`）：`get_sticker_info` 取 `clean_reply`/`sticker_path` 后，用 `_clean_reply_full(clean_reply, strip_emotion=False)` 替换 `:882-895` 的散装清洗。
- 主路径 else（`message_processor.py:1199-1210`）：`get_sticker_info` 后用 `_clean_reply_full(clean_reply, strip_emotion=False)` 替换散装清洗。
- `_finalize_reply`（`tool_executor.py:374-429`）：保留其特有的 JSON 提取、`<instruction>` 标签清理、canary 检测；把中段的 `strip_dsml → strip_reasoning → strip_system_leak → strip_emotion → humanize → deduplicate → 名称替换`（`:398-428`）替换为 `_clean_reply_full(text, strip_emotion=strip_emotion)` 调用（`_finalize_reply` 在其之后还做 `_strip_injected_tool_defs` 与 canary，保留）。

此改造同时消除技术债：三处不再漂移，新增清洗规则只改一处即全路径生效。

### 5.4 调用顺序与不变量

主路径 `_finalize_response`（`message_processor.py:1141+`）：

1. `media_image_paths, media_video_path, reply = _extract_media_from_tool_results(tool_results, reply)`（现有）
2. `fab_paths, reply = _extract_fabricated_images_from_reply(reply)`（新）；`media_image_paths += fab_paths`
3. 隐私扫描、人格 critic、记忆写入（现有）
4. 情绪标签（现有）
5. `clean_reply, sticker_path = get_sticker_info(reply, ...)`；`clean_reply = _clean_reply_full(clean_reply, strip_emotion=False)`（新统一出口；`get_sticker_info` 已剥 emotion，故 False）
6. 语音构建、`ProcessResult(image_paths=media_image_paths, ...)`（现有）

fast-path 对齐同样顺序（无 tool_results 时步骤 1 跳过）。

**不变量**：发送前 `reply` 文本不含 `![...]()`、不含 `image.pollinations.ai`、不含模型名/伪造元数据；`image_paths` 含所有可下载图。

## 6. 错误处理

- 兜底下载失败：剥 markdown + debug 日志，回复照发（不崩）。
- 兜底下载成功但 QQ 发图失败：复用 `qq_bot_adapter._send_reply_with_media:901-907` 现有降级（降为纯文本）。
- 清洗过删保护：`strip_reasoning` 已有 `cleaned_len < original_len*0.3` 告警；`strip_image_gen_leak` 仅删精确串/整行元数据，正常人格回复不含这些，无过删风险。
- `_clean_reply_full` 任一子步骤异常：try/except 包裹单个步骤，失败跳过该步保留原文本，记 debug，不阻断回复（与现有散装清洗的容错策略一致）。

## 7. 测试

新增 3 个测试文件：

- `tests/test_strip_image_gen_leak.py`：
  - 模型名 `Agnes Image 2.1 Flash` / `agnes-image-2.1-flash` 删除；正常句"这个模型叫 XX"中"模型"不被误删。
  - `【图片生成中 —— Agnes Image 2.1 Flash ⚡】` 整行删除。
  - `Width Height: 560x792 | Seed: 93847 | Model: Default | Quality.default| Prompt: "..."` 整行删除；仅含 "Model: GPT" 的普通行不删。
  - 生产样本 1965/1966 片段清洗后零命中。

- `tests/test_extract_fabricated_images.py`：
  - markdown 图 `![alt](https://image.pollinations.ai/prompt/cat)` → 提取 1 URL、文本剥离、`image_paths` 含 1 项（httpx 下载 mock）。
  - 裸 pollinations URL 提取。
  - 多图场景。
  - 下载失败 → 剥 markdown、`image_paths` 不追加、不抛异常。
  - 正常含 `https://example.com` 链接的回复不被误抓（非 pollinations 非 markdown 图）。

- `tests/test_clean_reply_full.py`：
  - 三处入口（fast-path / 主路径 else / `_finalize_reply`）清洗后行为一致（同一输入同输出）。
  - 含 N2/N5 泄漏 + 生图泄漏的混合样本 → 全清。
  - 1965/1966 完整回复端到端：输出 0 个 `pollinations`/`Agnes Image`/`Width Height`，`image_paths` 非空。

## 8. 验收标准

- AC1：用 id 1965/1966 真实回复跑 `_clean_reply_full` + `_extract_fabricated_images_from_reply`，输出文本零 `pollinations`/`Agnes Image`/`Width Height`/`Seed`/markdown 图；`image_paths` 非空。
- AC2：新增测试全绿；全量回归测试通过（基线 2275 通过，2 个预存 `test_webui_subagent_xp.py` 失败不归责本改动）。
- AC3：fast-path / 主路径 else / `_finalize_reply` 三入口对同一输入产出一致。
- AC4：生产样本中正常含 URL 的回复（如 `https://example.com` 普通链接）不被误删、不误抓为图。

## 9. 不在范围

- memory_encoding 后台任务高频烧 token（Bug 3）→ 单独 spec 处理（加最小间隔/批量合并/降级更便宜模型）。
- agnes 工具本身的 API 稳定性、`media_tasks` 持久化补全（1965/1966 未写 media_tasks 是因未调工具，根因解决后自然恢复）。
- QQ 适配器发送侧改造（现有 `_send_reply_with_media` 已满足，无需改动）。
