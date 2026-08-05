# Task 1 报告: ilink_client AES 加密 + CDN 上传原语

## 状态
DONE_WITH_CONCERNS（详见"关注点"）

## 实现内容
在 `ilink_client.py`（`ILinkClient` 类）新增三个方法，并在文件顶部新增 import：
- **`_aes_ecb_encrypt(data: bytes, key: bytes) -> bytes`** — AES-128-ECB + PKCS7 加密（`@staticmethod`）
- **`_get_upload_url(to_user_id, raw_bytes, aes_key) -> tuple[str, str, bytes]`** — 调用 `POST /ilink/bot/getuploadurl`，返回 `(upload_param, filekey, encrypted)`；请求体含 `media_type=1`、`no_need_thumb=True`、`rawsize`、`rawfilemd5`、`filesize`、`aeskey(hex)`
- **`_upload_to_cdn(upload_param, filekey, encrypted) -> str`** — 上传加密数据到 `ILINK_CDN_URL/upload`，返回 `x-encrypted-param` 响应头；含 HTTP 错误 / 非 200 状态处理

顶部新增：
```python
import hashlib
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives import padding
```

测试文件 `tests/test_ilink_send_media.py` 新建，含 3 个测试（AES 往返、getuploadurl 请求字段、CDN 上传返回头）。

## TDD 证据

### RED（初次运行）
命令：`/home/orangepi/ai-agent/.venv/bin/python -m pytest tests/test_ilink_send_media.py -v`
结果：`3 failed`，全部为 `AttributeError: 'ILinkClient' object has no attribute '_aes_ecb_encrypt'`（及 `_get_upload_url`、`_upload_to_cdn`）。

### GREEN（实现后）
命令：`/home/orangepi/ai-agent/.venv/bin/python -m pytest tests/test_ilink_send_media.py -v`
结果：`3 passed in 0.95s`。

### 回归（既有 ilink 测试）
命令：`/home/orangepi/ai-agent/.venv/bin/python -m pytest tests/test_ilink_send_message.py tests/test_ilink_send_media.py -q`
结果：`12 passed in 0.44s`（9 个既有 send_message + 3 个新增）。

## 变更文件
- `ilink_client.py`（修改，+import +3 方法）
- `tests/test_ilink_send_media.py`（新建，3 测试）

## 提交
- `cc4e902` feat(wechat): ilink_client 新增 AES 加密与 CDN 媒体上传原语
- 仅暂存并提交了上述两个文件，工作区其余无关变更（agent_context.py、prompt_builder.py、tool_engine/*、utils/logging_config.py、web/dist/index.html 等）均未触碰。

## 自审发现 / 关注点
1. **brief 测试断言与实现存在一处不一致（已修正）**：brief 的测试断言 `assert "/uploadurl" in fake.calls[0]["url"]`，但 `_get_upload_url` 按 brief 实现调用 `self._post("/ilink/bot/getuploadurl", ...)`，而 `_post` 会前置 `self._active_base_url`（`https://ilinkai.weixin.qq.com`），最终 URL 为 `.../ilink/bot/getuploadurl`，其中不含 `/uploadurl` 子串（实际是 `/getuploadurl`）。该断言必然失败。由于 `/ilink/bot/getuploadurl` 与代码库既有端点模式（`send_message` 用 `/ilink/bot/sendmessage`）一致，是正确协议路径，故将测试断言修正为 `assert "/ilink/bot/getuploadurl" in fake.calls[0]["url"]`。这是对 brief 的唯一偏离，其余代码均按 brief 逐字实现。
2. **环境注意**：默认 `python`/`python3` 指向 `/home/orangepi/.hermes/venvs/hermes/bin/python`，无 pytest。需用项目虚拟环境 `/home/orangepi/ai-agent/.venv/bin/python` 运行测试。
3. 终端输出在部分情况下未自动回显（tel 6/7 的 `command_run_logs` 为空），改用重定向到文件后读取确认；测试结果本身已从 pytest 输出确认无误。

## 结论
三个原语已实现并通过测试（含既有回归），仅提交了任务指定的两个文件。可衔接 Task 2 的 `send_media_message`。