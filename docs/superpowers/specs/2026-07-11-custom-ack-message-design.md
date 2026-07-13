# 自定义 ACK 消息设计

## 目标

允许用户在 WebUI Agent 管理面板中自定义"收到消息"的 ACK 提示文本，支持多句随机，全渠道生效（QQ / CLI / WebUI）。

## 数据模型

在 agent JSON（如 `config/agents/xiaoda.json`）中新增可选字段：

```json
{
  "ack_messages": ["小妲收到啦，正在想～🌿", "收到～让人家想想哦🌱"]
}
```

- 类型：`string[]`，可选
- 为空/不存在 → 使用默认 `f"{display_name}收到啦，正在想～🌿"`
- 非空 → 随机选一条

## 核心 API

在 `emotion/emoji_config.py` 新增：

```python
def get_ack_message(agent_name: str) -> str:
    """获取 agent 的 ACK 消息。

    1. 从 agent JSON 读取 ack_messages 列表
    2. 为空 → 返回默认 f"{display_name}收到啦，正在想～🌿"
    3. 非空 → 随机选一条，经 apply_agent_name_replacements 处理
    """
```

## 名称替换策略

- ACK 文本经 `apply_agent_name_replacements` 处理
- 包含 agent 原名（如"纳西妲"）或 agent key（如"xiaoda"）→ 替换为当前 display_name
- 不包含任何 agent 名称标识 → 原样输出（静默降级）

## 渠道接入

| 渠道 | 当前代码 | 修改后 |
|------|---------|--------|
| QQ 私聊 | `qq_bot_adapter.py:572` 硬编码 | `get_ack_message('xiaoda')` |
| QQ 群聊 | `qq_bot_adapter.py:654` 硬编码 | `get_ack_message('xiaoda')` |
| CLI | `cli.py:115` STATUS_MAP thinking | `get_ack_message('xiaoda')` |
| CLI Client | `cli_client.py:86,136` STAGE_TEXT | `get_ack_message('xiaoda')` |

`agent_dispatcher.py:363` 的 `get_status_msg("thinking")` 保持不变（这是 LLM 思考状态，不是 ACK）。

## WebUI

在 `AgentsView.vue` 的 `n-tabs` 中，`personality` Tab 之后新增：

```vue
<n-tab-pane name="ack" tab="随心即言" v-if="isMain">
  <!-- NDynamicTags: 每条 ACK 作为一个 tag -->
  <!-- 实时预览 -->
</n-tab-pane>
```

- 使用 `NDynamicTags`（已引入）
- 保存时与其他字段一起通过 `PUT /agents/{name}` 提交
- 保存后即时生效

## 测试

`test_ack_message.py`：
- 未配置 → 返回默认
- 配置1条 → 返回该条
- 配置多条 → 随机返回其中一条
- 名称替换 → 原名被替换为 display_name
- 无名称 → 原样输出
