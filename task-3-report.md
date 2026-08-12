# Task 3 实施报告

## 实施范围

- 在布局层分离桌面侧栏悬停展开状态与移动侧栏开关状态。
- 增加移动端菜单按钮、遮罩关闭、显式关闭按钮、路由切换关闭和 Escape 关闭路径。
- 为主导航、菜单按钮、Agent 选择和连接状态补充可访问语义与双语文本。
- 将头像加载失败改为按 Agent 名称维护响应式失败集合，保证首字回退可见。
- 将 PromptInput 的键盘事件先转发给 ChatView，并在事件被阻止后跳过默认 Enter 发送。
- 打通斜杠菜单的上下键、Tab、Enter、Escape 键盘闭环，并补充 listbox、option、aria-selected 和 active descendant 语义。
- 增加移动侧栏、层级和动画时长令牌，在 reduced-motion 下移除侧栏和页面 3D/位移动画。

## 变更文件

- `web/frontend/src/components/layout/AppLayout.vue`
- `web/frontend/src/components/layout/SideBar.vue`
- `web/frontend/src/components/layout/TopBar.vue`
- `web/frontend/src/components/chat/PromptInput.vue`
- `web/frontend/src/components/chat/SlashPalette.vue`
- `web/frontend/src/views/ChatView.vue`
- `web/frontend/src/styles/sumeru-tokens.css`
- `web/frontend/src/i18n/zh.ts`
- `web/frontend/src/i18n/en.ts`
- `tests/test_webui_navigation_contracts.py`

## 测试过程

先运行新增契约测试并确认 7 项均因目标能力缺失而失败：

```bash
.venv/bin/python -m pytest tests/test_webui_navigation_contracts.py -q
```

实现后运行：

```bash
.venv/bin/python -m pytest tests/test_webui_navigation_contracts.py tests/test_frontend_runtime_contracts.py -q
```

结果：17 passed。

```bash
cd web/frontend && npx vue-tsc --noEmit
```

结果：退出码 0，无类型错误。

```bash
git diff --check -- web/frontend/src/components/layout/AppLayout.vue web/frontend/src/components/layout/SideBar.vue web/frontend/src/components/layout/TopBar.vue web/frontend/src/components/chat/PromptInput.vue web/frontend/src/components/chat/SlashPalette.vue web/frontend/src/views/ChatView.vue web/frontend/src/styles/sumeru-tokens.css web/frontend/src/i18n/zh.ts web/frontend/src/i18n/en.ts tests/test_webui_navigation_contracts.py
```

结果：退出码 0，无空白错误。

## 边界说明

- 保留 SideBar 现有全部路由和 `router-link-exact-active` 精确高亮规则。
- 未修改无关业务逻辑，未清理工作区中原有的其他变更。
- 未执行 git commit。
