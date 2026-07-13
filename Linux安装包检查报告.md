# nahida-agent Linux 安装包完整性/安全性/功能性/兼容性检查报告

**检查时间**: 2025-07-13  
**项目路径**: /app/data/所有对话/主对话/nahida-agent/  
**版本**: v0.5.03

---

## 1. 完整性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ⚠️ | 完整性 | spec hiddenimports — core/* 覆盖 | 缺少 `core.behavioral_direction`, `core.behavioral_signal`, `core.cancel_token`, `core.conflict_supersession`, `core.delegation`, `core.dream_engine_v2`, `core.enhanced_router`, `core.event_bus`, `core.intervention_loop`, `core.tnr_self_heal`（共10个模块） |
| ⚠️ | 完整性 | spec hiddenimports — memory/* 覆盖 | 缺少 `memory.bridge_memory`, `memory.cognitive_memory`, `memory.concept_graph`, `memory.confirm_correct`, `memory.entity_extractor`, `memory.entity_store`, `memory.fsrs_model`, `memory.hopfield_layer`, `memory.key_extractor`, `memory.kg_search`, `memory.knowledge_graph_v2`, `memory.preference_discovery`, `memory.query_cache`（共13个模块） |
| ⚠️ | 完整性 | spec hiddenimports — db/* 覆盖 | 缺少 `db.db_concept`, `db.db_kg_v2`, `db.db_temporal_memory`（共3个模块） |
| ⚠️ | 完整性 | spec hiddenimports — agent_core/* 覆盖 | 缺少 `agent_core.user_base`, `agent_core.user_cli`, `agent_core.user_qq`, `agent_core.user_web`, `agent_core.shared_blackboard_db`, `agent_core.structured_blackboard`（共6个模块） |
| ⚠️ | 完整性 | spec hiddenimports — chaos/quality 覆盖 | 缺少 `chaos.*` 全部7个模块和 `quality.triple_axis_degradation`，但这些为开发/测试用模块，非生产必须 |
| ⚠️ | 完整性 | spec hiddenimports — 第三方依赖覆盖 | `fastapi`, `numpy`, `rich`, `networkx`, `lxml`, `websockets` 未在 hiddenimports 中显式列出，但代码中有 import 语句，PyInstaller 静态分析可发现；`python-multipart`（import 名 `multipart`）既不在 hiddenimports 也未被项目代码直接 import，依赖 FastAPI 运行时懒加载，**可能打包遗漏** |
| ✅ | 完整性 | scripts/install-linux.sh 存在 | 文件存在，5302字节，功能完整（依赖检查→解压安装→venv创建→systemd服务） |
| ⚠️ | 完整性 | scripts/auto-update.sh 可执行 | 文件存在但权限为 644（不可执行），需 `chmod +x` |
| ✅ | 完整性 | scripts/doctor.sh 存在且可执行 | 权限 755，功能完整（支持 PyInstaller 包和开发模式双路径） |
| ✅ | 完整性 | db/schema.sql 表覆盖 | `concept_nodes`(含difficulty/stability/phase/last_review/reinforcement_count列)、`episodic_memories`、`concept_edges`、`memory_child_chunks`、`schema_version` 全部存在 |
| ✅ | 完整性 | .env.example 配置项覆盖 | MIMO_API_KEY、QQBOT_APP_ID、QQBOT_APP_SECRET、ENABLE_QQ_BOT、MASTER_QQ_OPENID、OWNER_IDS、WEBUI_PORT、WEBUI_HOST 全部包含 |

## 2. 安全性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ⚠️ | 安全性 | .gitignore 排除 *.key | `*.key` 模式**未显式列出**；`credentials/` 已排除，但散落在其他目录的 .key 文件无保护 |
| ✅ | 安全性 | .gitignore 排除 .env | 已排除 |
| ✅ | 安全性 | .gitignore 排除 credentials/ | 已排除 |
| ✅ | 安全性 | .gitignore 排除 *.db | 已排除 |
| ✅ | 安全性 | .gitignore 排除 webui_overrides.json | 已排除 |
| ✅ | 安全性 | spec _exclude 排除敏感文件 | .env、.env.prod、.env.local、webui_overrides.json、USER.md、SOUL.md、MEMORY.md、IDENTITY.md、credential_salt.bin 全部排除 |
| ✅ | 安全性 | spec _exclude_dirs 排除敏感目录 | credentials、__pycache__、.git、node_modules、stickers、voice_refs 全部排除 |
| ✅ | 安全性 | spec *.key/*.secret 排除 | 通过 `fn.endswith('.key')` 和 `fn.endswith('.secret')` 逻辑排除 |
| ⚠️ | 安全性 | install-linux.sh 非root运行 | 无 EUID/UID 检查，整个安装流程以当前用户执行（$HOME下），仅 systemd 注册需 sudo；**建议添加 root 检测提示** |
| ✅ | 安全性 | 无硬编码 API Key | grep 检测仅发现 test 文件中的 `sk-test*` 占位值和 `secrets_broker.py` 的正则脱敏模式，无真实密钥 |
| ✅ | 安全性 | Dockerfile 非特权用户 | `USER appuser`（uid=1000），目录权限已 chown |

## 3. 功能性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 功能性 | agent.py 入口语法 | `ast.parse()` 通过，无语法错误 |
| ❌ | 功能性 | .run 自解压安装流程 | **严重缺陷**: `build-release.sh` 通过 `cat install-linux.sh tar.gz > .run` 创建自解压包，但 `install-linux.sh` 缺少自解压逻辑（无 `__ARCHIVE__` 标记、无 `tail -n+N $0` 提取），执行 .run 文件时脚本会寻找外部 tar.gz 参数而非提取内嵌数据 |
| ⚠️ | 功能性 | scripts/build-release.sh 可执行 | 权限 644（不可直接 `./` 执行，需 `bash scripts/build-release.sh`） |
| ⚠️ | 功能性 | scripts/start.sh 硬编码路径 | 使用硬编码路径 `/home/orangepi/ai-agent` 和 `/home/orangepi/miniconda3/bin/python`，**不可移植**；生产环境需修改 |
| ✅ | 功能性 | deploy/qq-agent.service 存在 | systemd 服务文件完整，包含资源限制和重启策略；但路径为占位符（REPLACE_WITH_*），需用户替换 |
| ✅ | 功能性 | web/frontend 构建命令 | `node build.mjs` 内部调用 `npx vite build`，构建流程正确 |

## 4. 兼容性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 兼容性 | pyproject.toml requires-python | `>=3.11` |
| ✅ | 兼容性 | Dockerfile 基础镜像 | `python:3.11-slim-bookworm`，与 pyproject.toml 版本要求一致 |
| ⚠️ | 兼容性 | pywebview Linux 依赖 | `requirements.txt` 包含 `pywebview>=5.0`，Linux 上需要 GTK3（`libgtk-3-dev`等），Dockerfile 仅安装 `libgl1-mesa-glx` 和 `libglib2.0-0`，**缺少 GTK 依赖**；裸机部署 install-linux.sh 也未安装 GTK |
| ✅ | 兼容性 | docker-compose.yml 端口/环境 | 端口 8082、环境变量 KIOXIA_DATA_DIR/WEBUI_HOST/WEBUI_PORT/AGENTLY_CLI_HOME 一致 |
| ⚠️ | 兼容性 | docker-compose.yml vs prod.yml 卷差异 | dev.yml 挂载 `./:/app`（开发模式），yml 挂载 `./.env:/app/.env:ro`，prod.yml 仅挂载 `agent-data:/data`（prod 不挂载 .env 到 /app，仅通过 env_file 注入），配置一致但挂载策略不同属预期行为 |
| ⚠️ | 兼容性 | qq-agent.service 端口不一致 | 服务文件默认端口 8080，而 docker-compose 和 .env.example 默认 8082 |

---

## 总结

### 判定: ⚠️ 不通过（需修复关键问题）

### 必须修复（P0）

1. **❌ .run 自解压安装流程缺失** — `scripts/install-linux.sh` 缺少自解压逻辑，`build-release.sh` 生成的 .run 文件无法正常工作。需在 install-linux.sh 中添加：
   - `__ARCHIVE__` 标记行作为分界
   - `tail -n +N $0 | tar xz` 提取内嵌 tarball 的逻辑
   - 或改为先提取到临时目录再执行安装

### 建议修复（P1）

2. **⚠️ spec hiddenimports 遗漏 31+ 个模块** — `agent_core.user_*`、`core.event_bus`、`core.dream_engine_v2`、`memory.knowledge_graph_v2`、`db.db_concept`、`db.db_kg_v2` 等生产关键模块未列入，可能导致 PyInstaller 打包后运行时 ImportError
3. **⚠️ `python-multipart` 未被 PyInstaller 覆盖** — 作为 FastAPI 的隐式依赖（表单解析），既不在 hiddenimports 也未被代码直接 import，打包后 FastAPI 表单功能可能失败
4. **⚠️ .gitignore 缺少 `*.key` 模式** — 散落在非 credentials/ 目录的密钥文件可能被意外提交
5. **⚠️ scripts/start.sh 硬编码路径** — 使用绝对路径 `/home/orangepi/ai-agent`，不可移植
6. **⚠️ qq-agent.service 端口默认 8080** — 与项目约定 8082 不一致
7. **⚠️ pywebview Linux GTK 依赖缺失** — Dockerfile 和 install-linux.sh 均未安装 GTK 库

### 可选优化（P2）

8. **⚠️** scripts/auto-update.sh、install-linux.sh、build-release.sh 权限为 644，建议设为 755
9. **⚠️** install-linux.sh 建议添加 root 用户检测提示（非禁止，仅提醒）
10. **⚠️** chaos/* 模块（7个）为开发/测试工具，非生产必须，可从 hiddenimports 排除
