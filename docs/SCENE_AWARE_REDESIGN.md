# 场景感知系统提示词重构方案

## 问题诊断

### 当前架构的 3 个核心问题

1. **每轮重建 system prompt** — `build_scene_aware_prompt()` 在每次 `build_messages()` 时被调用，
   重新拼接整个 system prompt 字符串。即使模块内容已缓存（按 mtime），拼接结果未缓存。
   后果：**KV Cache / Prompt Caching 完全失效**（前缀每轮变化 = 缓存每轮 miss）。

2. **硬匹配场景选择** — `_classify_scene()` 取第一个命中的关键词返回单一场景。
   后果：多意图输入（"好累啊，帮我查下天气"）被错误分类。

3. **无场景稳定性** — 每条消息独立分类，可能在 greeting/task/emotional 间频繁切换。
   后果：50 轮对话可能产生 20+ 次场景切换 = 20+ 次 KV Cache 失效。

### 性能影响估算

- System prompt ~2000 tokens
- 每次缓存 miss = 重新 Prefill 2000 tokens ≈ 200-400ms 额外延迟
- 50 轮对话 × 20 次切换 = 4-8 秒累积浪费
- 加上 token 计费成本（缓存 token 价格 ~1/10）

## 解决方案：三层优化

### Layer 1: 加权多场景检测（替代硬匹配）

```python
def _classify_scene_blended(user_input: str) -> dict[str, float]:
    """返回 {scene: weight}，权重之和=1.0"""
    # 检测多个场景信号，按命中数加权
    scores = {}
    for scene, keywords in _SCENE_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in clean)
        if hits > 0:
            scores[scene] = hits
    total = sum(scores.values())
    return {s: w / total for s, w in scores.items()} if total > 0 else {"default": 1.0}
```

示例：
- "好累啊，帮我查下天气" → {emotional: 0.5, tool: 0.5}
- 混合优先级 = 0.5 × emotional列 + 0.5 × tool列

### Layer 2: 场景签名缓存（避免重复拼接）

```python
# 缓存结构：{签名元组: 拼接后的 prompt 字符串}
_scene_prompt_cache: dict[tuple, str] = {}

def build_scene_aware_prompt(user_input, address_term):
    modules = _load_cached_modules(address_term)  # 已有 mtime 缓存
    weights = _classify_scene_blended(user_input)

    # 计算混合优先级 → 排序 → 生成签名
    scores = {name: sum(w * _MODULE_SCENE_PRIORITY[name].get(s, 0) for s, w in weights.items())
              for name in modules}
    sig = tuple(sorted(scores, key=scores.get))

    # 命中缓存 → 直接返回（零开销，KV Cache 命中）
    if sig in _scene_prompt_cache:
        return _scene_prompt_cache[sig]

    # 未命中 → 拼接并缓存
    prompt = "\n\n---\n\n".join(modules[n] for n in sig if modules[n])
    _scene_prompt_cache[sig] = prompt
    return prompt
```

关键：签名是**排序后的模块名元组**。不同输入只要产生相同排序 → 共享缓存。

### Layer 3: 场景粘性（最小化切换）

```python
_current_scene_sig: tuple = ()
_scene_switch_threshold: float = 0.6  # 新场景主导权重需 >60% 才切换

def build_scene_aware_prompt(user_input, address_term):
    weights = _classify_scene_blended(user_input)
    new_sig = _compute_sig(weights, modules)

    # 场景粘性：新场景的主导权重需超过阈值才切换
    max_weight = max(weights.values()) if weights else 0
    if _current_scene_sig and max_weight < _scene_switch_threshold:
        # 权重不够强 → 保持当前场景
        new_sig = _current_scene_sig
    else:
        _current_scene_sig = new_sig

    # 从缓存取或构建
    ...
```

## 为什么不能在历史中"调换顺序"

1. **API 层面**：messages[0] 永远是 system prompt。API 按顺序处理消息，KV Cache 基于前缀匹配。
   改了 messages[0] = 新前缀 = 缓存 miss。无法绕过。

2. **正确性**：如果 system prompt 顺序变了但历史中的旧 system prompt 没变，LLM 会看到矛盾信息。

3. **正确做法**：最小化 messages[0] 的变化次数。场景粘性 + 签名缓存 = 只在真正需要时才变。

## 预期收益

| 指标 | 当前 | 优化后 | 改善 |
|------|------|--------|------|
| 场景切换次数（50轮） | ~20次 | ~3-5次 | -75% |
| KV Cache 命中率 | ~0% | ~90% | +90% |
| 每轮 system prompt 开销 | 2000 token prefill | 0（缓存命中） | -100% |
| 场景检测延迟 | ~0.01ms | ~0.02ms | +0.01ms（可忽略）|
| 场景检测准确率 | ~70% | ~85% | +15% |

## 实现步骤

1. 替换 `_classify_scene()` → `_classify_scene_blended()` (加权混合)
2. 添加 `_scene_prompt_cache` 和 `_current_scene_sig` (签名缓存+粘性)
3. 重写 `build_scene_aware_prompt()` (三层整合)
4. 添加缓存命中率日志 (可观测性)
5. 冒烟测试 × 3 轮 + 基准评测对比
