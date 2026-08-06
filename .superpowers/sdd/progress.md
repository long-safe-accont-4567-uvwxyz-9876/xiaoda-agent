# SDD Progress Ledger — 微信 ACK 与表情包系统 (2026-08-04)

Plan: docs/superpowers/plans/2026-08-04-wechat-ack-sticker.md
Base commit: 4825b70 (HEAD)
Branch: feat/wechat-bot-ilink

## Tasks

- [x] Task 1: ilink_client AES 加密 + CDN 上传原语 (commits 4825b70..cc4e902, review clean)
- [x] Task 2: ilink_client send_media_message 图文合并 (commits cc4e902..d854bb1, review clean)
- [x] Task 3: wechat_bot_adapter ACK 发送 (commits d854bb1..94cb298, review clean)
- [x] Task 4: wechat_bot_adapter 表情包图文合并 + send_sticker (commits 94cb298..ed4953b, review clean)
- [x] Task 5: 全量回归 (27 passed, 0 failed)
- [x] 全部任务完成
- [x] Final Review + Fix (commit b6c190c: CDN 空头抛异常 + URL 编码, 10 passed)

## Completion Log

- 2026-08-04 全部 5 任务完成，27 passed
- 2026-08-04 Final Review: 2 Important (CDN 空头不触发回退、upload_param 未编码) + Minor 清理，已在 commit b6c190c 修复，10 passed

## Minor 项（供最终审查权衡）

- Task 1: tests/test_ilink_send_media.py 末尾无换行；docstring 声称校验 rawfilemd5/filesize 但断言未覆盖；upload_param 未 URL 编码（边缘，服务端下发值）
- Task 2: tests/test_ilink_send_media.py 末尾无换行；未断言 media.aes_key 为 base64
- Task 3: tests/test_wechat_ack_sticker.py 未使用 `from pathlib import Path`；末尾无换行；ACK 失败路径未测
- Task 4: tests/test_wechat_ack_sticker.py:93 注释「让 send_media_message 抛异常」不准确（实为底层 client 抛异常），建议改为「让底层 client 抛异常」

- (empty)
## Plan: 2026-08-05 CLI 交互式斜杠命令 (prompt_toolkit)
Base: f7ee7c1 · Branch: feat/cli-shared-process

- [x] Task 1: 声明 prompt_toolkit 依赖 + spec hiddenimports (f7ee7c1..4c29675, review clean)
- [x] Task 2: cli_client discover_models / list_agents + 单测 (4c29675..7035460, review clean)
  - Minor: tests/test_cli_client_http.py 末尾缺换行；brief 命令需 .venv/bin/python
- [x] Task 3: cli_menu 菜单选择器 + 单测 (7035460..5a020c1, review clean)
  - Minor: cli_menu.py / test_cli_menu.py 末尾缺换行
- [x] Task 4: cli.py 集成 prompt_toolkit + 补全 + 多步菜单 (5a020c1..5059ccd → fix cab8f1a, review clean after fix)
  - Critical/Important 修复: _SlashCompleter() 无参构造；多步菜单 Esc 取消不误发 (cab8f1a)
  - 5 passed (tests/test_cli_multistep.py) + py_compile ok
  - Minor: Step 11 手动 TTY 验收待执行
- [x] 最终整体审查 (b30a156..cab8f1a): Ready to merge, 无 Critical/Important 代码问题
  - Important: Step 11 手动 TTY 验收待执行（发布前）
  - Minor: 多处文件缺尾换行；_MULTI_STEP_COMMANDS 硬编码；_model_arg_completions 与 _SlashCompleter 逻辑重复；_menu_model 空列表无提示

## Plan: 2026-08-05 Textual 重构 CLI 为可点击富文本 TUI
Base: 2e52fee · Branch: feat/cli-shared-process

- [x] Task 1: cli_common 共享助手 + textual 依赖 (2e52fee..9031e94, review clean)
  - Minor: cli.py 未使用导入 STYLE/STATUS_MAP/AGENT_NAMES；test_cli_common.py 未使用导入 STATUS_MAP/AGENT_NAMES；cli_common.py/test_cli_common.py 末尾缺换行
- [x] Task 2: cli_app.py Textual App 骨架 + 命令分组 (9031e94..e1e23cc, review clean)
  - Minor: Message.msg CSS 选择器无效（用 Static 非 Message）；无全命令覆盖回归测试；未用导入 asyncio/Container/Header/Label；set_interval noop 空转；文件末尾缺换行
  - 交给 Task 3：修 Message.msg 选择器为 .msg、加 test_every_command_has_group、删未用导入与 noop 定时器
- [x] Task 3: 主进程连接 + 聊天区渲染 (e1e23cc..cdf4dc2, Important #1/#2 fixed in 8d5e1ea, review clean)
  - Important 修复: 连接改异步 run_worker (on_mount)，阻塞 offload to_thread；删 collect_reply + test_cli_app_chat.py
  - Minor: cli_app.py:129 函数内重复 import cli_client；_send_chat 用裸 create_task 未用 run_worker；_report_status 异常静默吞
- [x] Task 4: 斜杠命令面板（搜索/分组/鼠标点击）(8d5e1ea..d40841c, Critical #1/#2 fixed in 1368d0b, review clean)
  - Critical 修复: #panel-search 挂 on_input_changed 实时过滤；on_input_submitted 顶部守卫 panel-search 冒泡
  - Minor: 面板搜索框 Enter 不选中高亮项（守卫副作用，可后续在守卫处触发 on_select）；test_cli_app_panel.py 末尾缺换行
- [x] Task 5: 多步命令二级面板 (1368d0b..6aae9ed, Important fixed in 3a54663, review clean)
  - Important 修复: _MultiStepPanel 选中先 dismiss 再回调；/model 两级不叠栈
  - Minor: _open_model_models mopts 空仅 add_status；run_worker exclusive=False 并发风险；_MULTI_STEP 无 arg_completions 渲染空面板；test_cli_app_multistep.py 末尾缺换行
- [x] Task 6: 真实斜杠命令执行 + 美术打磨 (3a54663..0d58020, review clean)
  - 裁决落地: 弃 HTTP /api/v1/commands/run（不存在），改 async _send_slash 走 WSClient.chat；多步最终项发完整命令串；/help 本地；CSS 统一 STYLE
  - Minor: test_cli_app_exec.py 仅测未连接分支，happy path 需 mock _ws；文件末尾缺换行；_send_slash 无 status_callback（与 cli.py 斜杠路径一致）
- [x] Task 7: TUI 入口判定 + textual 打包 (0d58020..982ed57, review clean)
- [x] 最终整体审查 (2e52fee..be0dded): 0 Critical / 1 Important / ~10 Minor
  - Important #1 已修复: TUI 用户称谓硬编码"爸爸/你"→ 接入 address_term() 动态读取（commit be0dded）
  - M1 已修复: _open_multistep 加未连接守卫防空面板；M2 删除 cli.py 未用导入；M3 删除 cli_app 重复 import（commit be0dded）
  - 41 passed（修复后复测）
  - Minor 裁定 accept-as-is: 缺尾换行、test_cli_app_exec happy path 未测、_send_slash 无 status_callback
  - Minor 裁定 fix-later: Header 未显示模型/连接状态（spec §3.1 偏差 M8）、_send_chat 裸 create_task、_report_status 静默吞异常、面板搜索 Enter 不选中高亮项、_MULTI_STEP 两处硬编码重复
