# Trae Hooks 自动调用 Skills 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Trae IDE 编程项目创建 hooks 自动建议 skills 的机制

**Architecture:** 基于 Trae hooks 系统，在 SessionStart/UserPromptSubmit/PostToolUse/Stop 四个事件触发时检测项目类型并输出文本建议

**Tech Stack:** Bash shell scripts, JSON configuration, Trae hooks API

## Global Constraints

- 目标平台：macOS/Linux（Windows 需要后续适配）
- 脚本位置：`~/.trae-cn/hooks/` 目录
- 配置文件：`~/.trae-cn/hooks.json`
- 环境变量：`IS_CODE_PROJECT` (1=编程项目, 0=非编程项目)
- 输出格式：纯文本建议，不强制拦截
- **用户提问交互原则**：不要每次提问时都停下来等待用户回答，应使用 AskUserQuestion 工具在对话栏直接提问，提问后继续执行不中断流程

---

## File Structure

```
~/.trae-cn/
├── hooks.json                    # 主配置文件（Task 1）
└── hooks/                        # Hook 脚本目录
    ├── detect_project.sh         # 检测编程项目（Task 2）
    ├── session_start.sh          # SessionStart 处理（Task 3）
    ├── user_prompt_submit.sh     # UserPromptSubmit 处理（Task 4）
    ├── post_tool_use.sh          # PostToolUse 处理（Task 5）
    └── stop_handler.sh           # Stop 处理（Task 6）
```

**文件职责：**
- `hooks.json`: 配置哪些事件触发哪些脚本
- `detect_project.sh`: 提供检测函数，被 session_start.sh 调用
- `session_start.sh`: 会话开始时检测项目类型，注入环境变量
- `user_prompt_submit.sh`: 分析用户消息，输出 skill 建议
- `post_tool_use.sh`: 检测工具调用，输出 skill 建议
- `stop_handler.sh`: 任务完成前输出验证建议

---

## Task 1: 创建 hooks.json 配置文件

**Files:**
- Create: `~/.trae-cn/hooks.json`

**Interfaces:**
- Consumes: 无（第一个任务）
- Produces: 配置文件，定义所有事件的触发逻辑

- [ ] **Step 1: 创建 hooks.json 文件**

```bash
mkdir -p ~/.trae-cn/hooks
cat > ~/.trae-cn/hooks.json << 'EOF'
{
  "version": 1,
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "command": "bash ~/.trae-cn/hooks/session_start.sh",
            "timeout": 10
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "command": "bash ~/.trae-cn/hooks/user_prompt_submit.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Write|Edit",
        "hooks": [
          {
            "command": "bash ~/.trae-cn/hooks/post_tool_use.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "Stop": [
      {
        "loop_limit": 3,
        "hooks": [
          {
            "command": "bash ~/.trae-cn/hooks/stop_handler.sh",
            "timeout": 10
          }
        ]
      }
    ]
  }
}
EOF
```

- [ ] **Step 2: 验证 JSON 格式正确**

Run: `cat ~/.trae-cn/hooks.json | jq .`
Expected: JSON 输出无错误

- [ ] **Step 3: 提交**

```bash
git add ~/.trae-cn/hooks.json
git commit -m "feat: add hooks.json configuration for auto skills suggestion"
```

---

## Task 2: 创建 detect_project.sh 脚本

**Files:**
- Create: `~/.trae-cn/hooks/detect_project.sh`

**Interfaces:**
- Consumes: `$TRAE_PROJECT_DIR` 环境变量（由 Trae 提供）
- Produces: `detect_code_project()` 函数，返回 1（找到代码）或 0（未找到）

- [ ] **Step 1: 创建 detect_project.sh 脚本**

```bash
cat > ~/.trae-cn/hooks/detect_project.sh << 'EOF'
#!/bin/bash
# 检测是否为编程项目（内部函数，返回 0/1）

detect_code_project() {
  local CODE_PATTERNS="*.py *.js *.ts *.go *.java *.rs *.cpp *.c *.h package.json go.mod Cargo.toml pom.xml build.gradle"
  
  for pattern in $CODE_PATTERNS; do
    if find "$TRAE_PROJECT_DIR" -name "$pattern" -type f 2>/dev/null | head -n 1 | grep -q .; then
      return 1  # 找到代码文件
    fi
  done
  
  # 也检查 .git 和 .github 目录
  if [ -d "$TRAE_PROJECT_DIR/.git" ] || [ -d "$TRAE_PROJECT_DIR/.github" ]; then
    return 1
  fi
  
  return 0  # 未找到代码文件
}
EOF
chmod +x ~/.trae-cn/hooks/detect_project.sh
```

- [ ] **Step 2: 验证脚本语法**

Run: `bash -n ~/.trae-cn/hooks/detect_project.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ~/.trae-cn/hooks/detect_project.sh
git commit -m "feat: add detect_project.sh for code project detection"
```

---

## Task 3: 创建 session_start.sh 脚本

**Files:**
- Create: `~/.trae-cn/hooks/session_start.sh`

**Interfaces:**
- Consumes: `$TRAE_ENV_FILE` 环境变量（由 Trae 提供）、`detect_code_project()` 函数（Task 2）
- Produces: 向 `$TRAE_ENV_FILE` 写入 `IS_CODE_PROJECT=1` 或 `IS_CODE_PROJECT=0`，输出上下文建议

- [ ] **Step 1: 创建 session_start.sh 脚本**

```bash
cat > ~/.trae-cn/hooks/session_start.sh << 'EOF'
#!/bin/bash
# SessionStart 处理（主入口）

# 加载检测函数
source ~/.trae-cn/hooks/detect_project.sh

# 检测项目类型
detect_code_project
FOUND_CODE=$?

# 注入环境变量供后续 Hook 使用
if [ $FOUND_CODE -eq 1 ]; then
  echo "export IS_CODE_PROJECT=1" >> "$TRAE_ENV_FILE"
  echo "检测到编程项目，已启用 skills 自动建议功能。"
  echo "建议：可调用 using-superpowers skill 以获得更好的 skills 支持。"
else
  echo "export IS_CODE_PROJECT=0" >> "$TRAE_ENV_FILE"
fi
EOF
chmod +x ~/.trae-cn/hooks/session_start.sh
```

- [ ] **Step 2: 验证脚本语法**

Run: `bash -n ~/.trae-cn/hooks/session_start.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ~/.trae-cn/hooks/session_start.sh
git commit -m "feat: add session_start.sh for project type detection"
```

---

## Task 4: 创建 user_prompt_submit.sh 脚本

**Files:**
- Create: `~/.trae-cn/hooks/user_prompt_submit.sh`

**Interfaces:**
- Consumes: stdin JSON（包含 `prompt` 字段）、`$IS_CODE_PROJECT` 环境变量（Task 3）
- Produces: 纯文本建议，如"建议：检测到功能创建请求..."

- [ ] **Step 1: 创建 user_prompt_submit.sh 脚本**

```bash
cat > ~/.trae-cn/hooks/user_prompt_submit.sh << 'EOF'
#!/bin/bash
# UserPromptSubmit 处理

# 读取 stdin JSON
read -r stdin_json
PROMPT=$(echo "$stdin_json" | jq -r '.prompt')

# 检查是否为编程项目
if [ "$IS_CODE_PROJECT" != "1" ]; then
  exit 0
fi

# 分析用户消息类型
if echo "$PROMPT" | grep -qiE "创建|添加|实现|开发|构建|设计"; then
  echo "建议：检测到功能创建请求，建议先调用 brainstorming skill 进行设计讨论。"
elif echo "$PROMPT" | grep -qiE "修复|bug|错误|问题|调试"; then
  echo "建议：检测到问题修复请求，建议使用 systematic-debugging skill 进行系统性调试。"
elif echo "$PROMPT" | grep -qiE "测试|单元测试|集成测试"; then
  echo "建议：检测到测试相关请求，建议使用 test-driven-development skill。"
elif echo "$PROMPT" | grep -qiE "审查|review|重构"; then
  echo "建议：检测到代码审查/重构请求，建议使用 requesting-code-review skill。"
fi
EOF
chmod +x ~/.trae-cn/hooks/user_prompt_submit.sh
```

- [ ] **Step 2: 验证脚本语法**

Run: `bash -n ~/.trae-cn/hooks/user_prompt_submit.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 验证 jq 已安装**

Run: `which jq`
Expected: 输出 jq 路径（如 `/usr/bin/jq`）

如果未安装，运行：
```bash
sudo apt-get install jq  # Ubuntu/Debian
# 或
brew install jq          # macOS
```

- [ ] **Step 4: 提交**

```bash
git add ~/.trae-cn/hooks/user_prompt_submit.sh
git commit -m "feat: add user_prompt_submit.sh for message type analysis"
```

---

## Task 5: 创建 post_tool_use.sh 脚本

**Files:**
- Create: `~/.trae-cn/hooks/post_tool_use.sh`

**Interfaces:**
- Consumes: stdin JSON（包含 `tool_name`、`tool_input` 字段）、`$IS_CODE_PROJECT` 环境变量（Task 3）
- Produces: 纯文本建议（检测到测试文件修改时）

- [ ] **Step 1: 创建 post_tool_use.sh 脚本**

```bash
cat > ~/.trae-cn/hooks/post_tool_use.sh << 'EOF'
#!/bin/bash
# PostToolUse 处理

# 读取 stdin JSON
read -r stdin_json
TOOL_NAME=$(echo "$stdin_json" | jq -r '.tool_name')
TOOL_INPUT=$(echo "$stdin_json" | jq -r '.tool_input')

# 检查是否为编程项目
if [ "$IS_CODE_PROJECT" != "1" ]; then
  exit 0
fi

# 检测测试文件修改
if [ "$TOOL_NAME" = "Write" ] || [ "$TOOL_NAME" = "Edit" ]; then
  FILE_PATH=$(echo "$TOOL_INPUT" | jq -r '.file_path // .path')
  if echo "$FILE_PATH" | grep -qE "test_|_test\.|\.test\.|spec_|_spec\."; then
    echo "建议：检测到测试文件修改，建议遵循 TDD 流程，先运行测试验证。"
  fi
fi
EOF
chmod +x ~/.trae-cn/hooks/post_tool_use.sh
```

- [ ] **Step 2: 验证脚本语法**

Run: `bash -n ~/.trae-cn/hooks/post_tool_use.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ~/.trae-cn/hooks/post_tool_use.sh
git commit -m "feat: add post_tool_use.sh for test file detection"
```

---

## Task 6: 创建 stop_handler.sh 脚本

**Files:**
- Create: `~/.trae-cn/hooks/stop_handler.sh`

**Interfaces:**
- Consumes: stdin JSON（包含 `last_assistant_message` 字段）、`$IS_CODE_PROJECT` 环境变量（Task 3）
- Produces: 纯文本建议（建议验证完成质量）

- [ ] **Step 1: 创建 stop_handler.sh 脚本**

```bash
cat > ~/.trae-cn/hooks/stop_handler.sh << 'EOF'
#!/bin/bash
# Stop 处理

# 读取 stdin JSON
read -r stdin_json
LAST_MESSAGE=$(echo "$stdin_json" | jq -r '.last_assistant_message')

# 检查是否为编程项目
if [ "$IS_CODE_PROJECT" != "1" ]; then
  exit 0
fi

# 建议验证流程
echo "建议：任务完成前，建议调用 verification-before-completion skill 验证测试通过、代码质量等。"
EOF
chmod +x ~/.trae-cn/hooks/stop_handler.sh
```

- [ ] **Step 2: 验证脚本语法**

Run: `bash -n ~/.trae-cn/hooks/stop_handler.sh`
Expected: 无输出（语法正确）

- [ ] **Step 3: 提交**

```bash
git add ~/.trae-cn/hooks/stop_handler.sh
git commit -m "feat: add stop_handler.sh for completion verification suggestion"
```

---

## Task 7: 集成测试

**Files:**
- 无新文件（测试现有配置）

**Interfaces:**
- Consumes: 所有已创建的 hooks（Task 1-6）
- Produces: 验证 hooks 在编程项目中正常触发

- [ ] **Step 1: 手动触发 SessionStart 测试**

模拟 Trae 环境：
```bash
export TRAE_PROJECT_DIR="/tmp/test-project"
export TRAE_ENV_FILE="/tmp/test-env"
mkdir -p "$TRAE_PROJECT_DIR"
touch "$TRAE_PROJECT_DIR/test.py"
bash ~/.trae-cn/hooks/session_start.sh
cat "$TRAE_ENV_FILE"
```

Expected: 输出包含 "检测到编程项目"，`IS_CODE_PROJECT=1`

- [ ] **Step 2: 清理测试环境**

```bash
rm -rf "$TRAE_PROJECT_DIR" "$TRAE_ENV_FILE"
unset TRAE_PROJECT_DIR TRAE_ENV_FILE
```

- [ ] **Step 3: 验证所有脚本可执行**

Run: `ls -la ~/.trae-cn/hooks/`
Expected: 所有 `.sh` 文件有 `-rwxr-xr-x` 权限

- [ ] **Step 4: 最终提交**

```bash
git add -A
git commit -m "test: verify hooks integration in code project"
```

---

## Self-Review

**1. Spec coverage:**
- ✅ SessionStart 事件检测项目类型（Task 3）
- ✅ UserPromptSubmit 事件分析消息（Task 4）
- ✅ PostToolUse 事件检测工具（Task 5）
- ✅ Stop 事件建议验证（Task 6）
- ✅ 配置文件创建（Task 1）
- ✅ 检测函数封装（Task 2）
- ✅ 集成测试（Task 7）

**2. Placeholder scan:**
- ✅ 无 "TBD"、"TODO"、"implement later"
- ✅ 所有代码块完整
- ✅ 所有命令具体可执行

**3. Type consistency:**
- ✅ 所有脚本使用 `$TRAE_PROJECT_DIR`（Trae 提供的环境变量）
- ✅ 所有脚本使用 `$TRAE_ENV_FILE`（Trae 提供的环境变量）
- ✅ 所有脚本检查 `$IS_CODE_PROJECT`（SessionStart 注入）
- ✅ stdin JSON 字段名与 Trae 文档一致（`prompt`、`tool_name`、`tool_input`、`last_assistant_message`）

---

Plan complete and saved to `/home/orangepi/ai-agent/docs/superpowers/plans/2026-07-12-trae-hooks-auto-skills-implementation.md`.

**Two execution options:**

**1. Subagent-Driven (recommended)** - 我为每个任务派发独立的子代理，任务间进行 review，快速迭代

**2. Inline Execution** - 在当前会话中使用 executing-plans 执行，批量执行并设置检查点

选择哪种方式？