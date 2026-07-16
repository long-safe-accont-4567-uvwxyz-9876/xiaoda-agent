# ── Stage 1: 构建前端 ──
FROM node:20-slim AS frontend-builder
WORKDIR /build
COPY web/frontend/package*.json web/frontend/
RUN cd web/frontend && npm ci --no-audit --no-fund
COPY web/frontend/ web/frontend/
RUN cd web/frontend && npm run build

# 安装 agently-cli（邮箱 OAuth 需要）
RUN npm install -g @tencent-qqmail/agently-cli

# ── Stage 2a: Build Python dependencies ──
FROM python:3.11-slim-bookworm AS builder
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ python3-dev \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# ── Stage 2b: Python runtime ──
FROM python:3.11-slim-bookworm

# System runtime dependencies only (no build tools)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python dependencies (from builder stage)
COPY --from=builder /install /usr/local

# 项目代码
COPY . .

# 从 Stage 1 复制前端构建产物（web/dist 在 .gitignore 中，COPY . . 不包含它）
COPY --from=frontend-builder /build/web/dist web/dist

# 复制辅助脚本
COPY scripts/doctor.sh scripts/doctor.sh
RUN chmod +x scripts/doctor.sh

# 从 Stage 1 复制 agently-cli（邮箱 OAuth 工具）
# 复制 node 二进制和 agently-cli npm 包（run.js 需要 node）
COPY --from=frontend-builder /usr/local/bin/node /usr/local/bin/node
COPY --from=frontend-builder /usr/local/lib/node_modules/@tencent-qqmail/agently-cli /usr/local/lib/node_modules/@tencent-qqmail/agently-cli
RUN ln -sf /usr/local/lib/node_modules/@tencent-qqmail/agently-cli/scripts/run.js /usr/local/bin/agently-cli \
    && chmod +x /usr/local/bin/agently-cli

# 凭据目录持久化（OAuth token 存储，必须在 volume 中）
ENV AGENTLY_CLI_HOME=/data/agently-cli
RUN mkdir -p /data/agently-cli/.agently-cli

# 数据目录（通过 volume 挂载持久化）
ENV KIOXIA_DATA_DIR=/data
RUN mkdir -p /data/db /data/logs /data/credentials /data/stickers /data/xiaoli-stickers \
    /data/agent-stickers /data/files /data/media /data/voice_refs /data/config \
    /data/memory_state /data/plugins /data/workspace

# 创建非特权用户并设置目录权限
RUN groupadd -f appgroup && useradd -r -u 1000 -g appgroup appuser \
    && chown -R appuser:appgroup /app /data
USER appuser

# 默认端口（可通过环境变量 WEBUI_PORT 覆盖）
ENV WEBUI_PORT=8082
EXPOSE 8082

# 健康检查：先尝试 HTTP 探针，失败则运行 doctor --fix 自动修复后重试
HEALTHCHECK --interval=30s --timeout=30s --retries=3 \
    CMD python -c "\
import os, urllib.request, urllib.error, subprocess, sys; \
port = os.environ.get('WEBUI_PORT', '8082'); \
url = f'http://localhost:{port}/api/v1/system/os'; \
try: \
    urllib.request.urlopen(url, timeout=3); \
except Exception: \
    print('Health check failed, running doctor --fix...'); \
    subprocess.run([sys.executable, 'agent.py', 'doctor', '--fix']); \
    try: \
        urllib.request.urlopen(url, timeout=3); \
    except Exception: \
        sys.exit(1)"

# 启动命令（端口由 WEBUI_PORT 环境变量控制，默认 8082）
CMD ["python", "agent.py", "--web"]