# nahida-agent Docker 镜像完整性/安全性/功能性/兼容性检查报告

## 1. 完整性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 完整性 | 多阶段构建 | Dockerfile 包含完整两阶段：Stage1 `FROM node:20-slim AS frontend-builder`，Stage2 `FROM python:3.11-slim-bookworm` |
| ✅ | 完整性 | Stage1 前端构建 | 正确执行 `npm ci --no-audit --no-fund` → `npm run build`，并安装 agently-cli |
| ✅ | 完整性 | Stage2 复制前端产物 | `COPY --from=frontend-builder /build/web/dist web/dist`，注释说明了 web/dist 在 .gitignore 中需显式复制 |
| ✅ | 完整性 | requirements.txt 引用 | `COPY requirements.txt .` → `RUN pip install --no-cache-dir -r requirements.txt`，且利用 Docker 层缓存 |
| ✅ | 完整性 | doctor.sh 复制与权限 | `COPY scripts/doctor.sh scripts/doctor.sh` + `RUN chmod +x scripts/doctor.sh` |
| ✅ | 完整性 | agently-cli 复制 | 复制了 `/usr/local/bin/node`、`/usr/local/lib/node_modules/@tencent-qqmail/agently-cli`，并创建符号链接 |
| ✅ | 完整性 | 数据目录创建 | 创建了 /data/db, /data/logs, /data/credentials, /data/stickers, /data/xiaoli-stickers, /data/agent-stickers, /data/files, /data/media, /data/voice_refs, /data/config, /data/memory_state, /data/plugins, /data/workspace，完整覆盖 |
| ⚠️ | 完整性 | docker-compose 配置一致性 | docker-compose.yml 有 `.env` 文件挂载 (`./.env:/app/.env:ro`)，docker-compose.prod.yml 缺少该挂载；prod 版仅通过 `env_file: .env` 注入，未以只读方式挂载到 /app/.env |
| ⚠️ | 完整性 | k8s.yaml 与 Docker 镜像匹配 | k8s.yaml 使用 `xiaoda-agent:latest`（无 registry 前缀），而 docker-compose 使用 `ghcr.io/${GITHUB_OWNER}/xiaoda-agent:latest`；且缺少 KIOXIA_DATA_DIR 和 AGENTLY_CLI_HOME 环境变量 |

## 2. 安全性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 安全性 | 非特权用户运行 | `useradd -r -u 1000 -g appgroup appuser` + `USER appuser`，UID 1000 |
| ⚠️ | 安全性 | .env 文件挂载 | docker-compose.yml 有 `./.env:/app/.env:ro`（只读挂载），但 docker-compose.prod.yml 缺少此挂载，仅靠 `env_file: .env` 注入容器环境变量，代码中 `python-dotenv` 读 /app/.env 将找不到文件 |
| ✅ | 安全性 | HEALTHCHECK 合理性 | HTTP 探针 `/api/v1/system/os`（public_router，无需认证）+ `doctor --fix` 兜底修复，30s 间隔、10s 超时、3 次重试 |
| ✅ | 安全性 | 端口暴露 | 仅 `EXPOSE 8082`（WEBUI_PORT），无多余端口 |
| ✅ | 安全性 | 资源限制 | 所有 docker-compose 文件均设置 `deploy.resources.limits.memory: 1.5G` |
| ✅ | 安全性 | 基础镜像 | `python:3.11-slim-bookworm` 为 Docker Hub 官方 Python 镜像 |
| ✅ | 安全性 | AGENTLY_CLI_HOME 在 volume 中 | `ENV AGENTLY_CLI_HOME=/data/agently-cli`，`/data` 通过 `agent-data` volume 持久化 |
| ✅ | 安全性 | K8s secretRef | k8s.yaml 使用 `envFrom.secretRef.name: xiaoda-agent-secrets`，且 `optional: true` 允许首次部署无 secret |
| ✅ | 安全性 | .dockerignore 排除敏感文件 | 排除了 .env, credentials/, *.key, data/, config/webui_overrides.json 等 |

## 3. 功能性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 功能性 | CMD 启动命令 | `CMD ["python", "agent.py", "--web"]`，与代码 `parser.add_argument("--web")` 匹配 |
| ✅ | 功能性 | WEBUI_PORT 环境变量 | Dockerfile `ENV WEBUI_PORT=8082` + `EXPOSE 8082`；代码 `os.getenv("WEBUI_PORT", "8082")` 读取；docker-compose 传递环境变量 |
| ✅ | 功能性 | KIOXIA_DATA_DIR 一致性 | Dockerfile `ENV KIOXIA_DATA_DIR=/data`，代码 `os.getenv("KIOXIA_DATA_DIR")` 读取，volume 挂载 `/data` |
| ✅ | 功能性 | volume 挂载 | 所有 docker-compose 文件均配置 `agent-data:/data` |
| ✅ | 功能性 | HEALTHCHECK URL 匹配 | Dockerfile 探测 `/api/v1/system/os`，代码中 `public_router` 路径 `/system/os` 挂载在 `prefix="/api/v1"` 下，完整路径 `/api/v1/system/os`，完全匹配 |
| ✅ | 功能性 | restart 策略 | 所有 docker-compose 文件设置 `restart: unless-stopped` |
| ✅ | 功能性 | K8s 探针配置 | livenessProbe 和 readinessProbe 均配置，路径 `/api/v1/system/os`，初始延迟、间隔、超时、失败阈值完整 |
| ✅ | 功能性 | CI Docker 构建推送 | 使用 `docker/build-push-action@v5`，推送到 `ghcr.io/${{ github.repository_owner }}/xiaoda-agent`，打 latest 和版本号标签 |
| ⚠️ | 功能性 | K8s 缺少关键环境变量 | k8s.yaml 缺少 KIOXIA_DATA_DIR=/data 和 AGENTLY_CLI_HOME=/data/agently-cli，代码中这两个变量影响数据目录定位，缺失将 fallback 到默认路径（~/.ai-agent/data）导致数据不持久化 |

## 4. 兼容性检查

| 状态 | 类别 | 项目 | 详情 |
|------|------|------|------|
| ✅ | 兼容性 | ffmpeg 与 pilk 兼容性 | `pilk>=0.2.4` 依赖 ffmpeg 进行 SILK 编码，Dockerfile 已安装 `ffmpeg` |
| ⚠️ | 兼容性 | libgl1-mesa-glx / libglib2.0-0 依赖 | opencv (cv2) 在 vision_service.py 和 npu_inference.py 中条件导入，但 requirements.txt 未声明 opencv-python；这两个库是为 opencv 运行时准备的。Pillow 在 slim 镜像中不一定需要 libGL，当前配置为安全冗余但可能带来不必要的镜像体积 |
| ✅ | 兼容性 | docker-compose 版本 3.8 | 兼容 Docker Engine 18.06.0+，主流版本均支持 |
| ✅ | 兼容性 | K8s apiVersion apps/v1 | 兼容 Kubernetes 1.9+，主流版本均支持 |
| ✅ | 兼容性 | Node.js 20-slim | 前端使用 Vite 构建，Node 20 完全兼容 |
| ✅ | 兼容性 | gcc/g++/python3-dev 编译依赖 | pilk、Pillow 等含 C 扩展的包需要编译工具链，安装合理 |
| ⚠️ | 兼容性 | 缺少多架构构建 | CI 仅构建 linux-x86_64 和 windows-x64，Docker 构建未配置多架构（arm64），docker-compose.yml 注释提到 ARM 设备（Orange Pi）但 Docker 镜像无 arm64 版本 |

---

## 总结

**整体评定：⚠️ 有条件通过**（核心构建逻辑正确，但存在若干需修复的问题）

### 需要修复的问题（按优先级排列）

| 优先级 | 问题 | 影响范围 | 建议修复 |
|--------|------|----------|----------|
| 🔴 高 | k8s.yaml 缺少 KIOXIA_DATA_DIR 和 AGENTLY_CLI_HOME 环境变量 | K8s 部署时数据不持久化，凭据丢失 | 在 k8s.yaml 的 env 中添加 `KIOXIA_DATA_DIR: "1"` 和 `AGENTLY_CLI_HOME: "/data/agently-cli"` |
| 🔴 高 | docker-compose.prod.yml 缺少 .env 文件挂载 | 生产环境 python-dotenv 读不到 /app/.env，依赖环境变量的功能可能异常 | 添加 `./.env:/app/.env:ro` 到 volumes |
| 🟡 中 | k8s.yaml 镜像名称无 registry 前缀 | 与 CI 推送的 ghcr.io 镜像不一致，K8s 集群可能拉取失败 | 改为 `ghcr.io/<owner>/xiaoda-agent:latest` 或添加 imagePullPolicy + 说明 |
| 🟡 中 | Docker 构建缺少多架构支持 | ARM 设备（如 Orange Pi）无法使用官方镜像 | 在 CI 中添加 `docker/setup-qemu-action` + `docker/setup-buildx-action`，构建 linux/amd64,linux/arm64 |
| 🟢 低 | opencv 未在 requirements.txt 声明但 libgl1-mesa-glx 已安装 | 条件导入 cv2 会在运行时失败（Docker 中），libgl 增加镜像体积但无实际功能 | 如需 cv2 支持则添加 opencv-python-headless 到 requirements.txt；否则移除 libgl1-mesa-glx 减小镜像体积 |
| 🟢 低 | pywebview 在 Docker requirements 中 | pywebview 用于桌面模式，Docker 环境不需要，增加 pip install 时间和镜像体积 | 考虑拆分为可选依赖或使用条件安装 |
