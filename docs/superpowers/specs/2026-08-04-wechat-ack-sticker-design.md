# 微信 ACK 与表情包系统设计

## 背景与目标

微信 Bot 接入已实现文本收发，但缺少 QQ 端已有的两个交互能力，导致用户微信端体验不完整：

1. **ACK（"收到啦，正在想～"）**：QQ 在 `_core.process()` 前立即发送，微信直接进处理流程，无 ACK。
2. **表情包系统**：QQ 把 `ProcessResult.sticker_path`（情感系统已算好）与文字合并成图文消息发送，微信的 `send_sticker` 是空实现（`return False`）。

目标：让微信端行为与 QQ 完全一致。

## 现状调查

### QQ 端已实现（对齐目标）
- **ACK**：处理消息时同步发 `message.reply(get_ack_message('xiaoda'))`。
- **表情包**：`result.sticker_path` 由 agent_core 的 `get_sticker_info(reply, ctx.last_user_emotion)` 计算（平台无关），QQ 用 `_send_reply_with_media` 发图文混合消息。

### 微信端缺口
- `_handle_text_message` 直接 `process()`，无 ACK。
- `send_sticker` 为 TODO 空实现。
- 回复时只读 `result.reply`，未读 `result.sticker_path`。

### iLink 图片发送协议（研究结论）
微信发图片需四步（源自 `@tencent-weixin/openclaw-weixin` 协议）：

1. **getuploadurl**：`POST /ilink/bot/getuploadurl`，请求体含 `filekey`（16字节随机hex）、`media_type=1`（IMAGE）、`to_user_id`、`rawsize`（明文大小）、`rawfilemd5`（明文MD5）、`filesize`（AES加密后大小）、`no_need_thumb=true`（跳过缩略图）、`aeskey`（16字节AES密钥hex）。响应返回 `upload_param`。
2. **AES-128-ECB 加密**：随机16字节key，PKCS7填充，加密大小 `ceil((size+1)/16)*16`。
3. **上传 CDN**：`POST {cdn}/upload?encrypted_query_param={upload_param}&filekey={filekey}`，body 为加密二进制，`Content-Type: application/octet-stream`。保存响应头 `x-encrypted-param`（后续引用）。
4. **sendmessage 引用**：`item_list` 加 `{type:2, image_item:{media:{encrypt_query_param: x-encrypted-param, aes_key: base64(aeskey), encrypt_type:0}}}`。

`item_list` 支持多 item → 文字+图片可一条消息合并发送。

## 设计

### 架构
对齐 QQ，在微信 adapter 补 ACK 与表情包，协议层新增图片上传能力。

### 文件改动

#### 1. `ilink_client.py` — 新增图片发送能力
新增 `send_media_message(to_user_id, context_token, text, image_path)`，内部串起完整流程：
- `get_upload_url()`：`POST /ilink/bot/getuploadurl` 拿 `upload_param`。
- `_aes_encrypt(data, key)`：AES-128-ECB + PKCS7，随机16字节key。
- `_upload_to_cdn()`：`POST {cdn}/upload?...`，保存 `x-encrypted-param`。
- 组装 `item_list:[{type:1,text_item},{type:2,image_item:{media:{...}}}]` 一条消息合并发送。
- 复用现有 `_post`/`_build_headers`。`aeskey` 用 hex 传 server，base64 用于 CDNMedia。

#### 2. `wechat_bot_adapter.py` — 补 ACK + 表情包
- `_handle_text_message`：`process()` 前先发 `get_ack_message('xiaoda')`（与 QQ 一致）。
- 回复处：若 `result.sticker_path` 存在且可读，调 `send_media_message` 发文字+表情包合并；否则纯文本 `send_message`。
- 实现 `send_sticker`（替换空实现）：调 `send_media_message` 只发图。
- 图片上传失败时回退纯文本（不阻塞回复）。

#### 3. 测试
- `ilink_client`：`send_media_message` 全流程 mock 验证（getuploadurl→加密→上传→sendmessage 组装含 image_item）。
- `adapter`：ACK 发送时机、sticker_path 存在时走图文合并、上传失败回退纯文本。

### 错误处理
- 图片上传任一环节失败 → 记录 warning 日志，回退纯文本，不影响用户回复。
- 会话过期（-14）→ 沿用现有 send_message 的 `SessionExpiredError` 处理。

### 数据流
```
用户消息 → _handle_text_message
  → [照发] send_message(get_ack_message('xiaoda'))   ← ACK
  → process() → result
  → [有 sticker_path] send_media_message(text, sticker)   ← 图文合并
  → [无] send_message(text)
```

## 关键决策
- ACK 与 QQ 完全一致：`get_ack_message('xiaoda')`，处理前同步发送。
- 表情包与文字合并发送（一条消息），与 QQ 一致。
- 表情包发送失败回退纯文本，不阻塞回复。

## 验收标准
1. 微信收到消息后，立即收到 ACK 文案。
2. 回复含情感表情包时，微信端收到文字+表情包合并消息。
3. 图片上传失败时，仍收到纯文本回复。
4. 新增测试全部通过。