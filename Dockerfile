FROM python:3.11-slim-bookworm

# 系统依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libgl1-mesa-glx \
    libglib2.0-0 \
    gcc \
    g++ \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python 依赖（利用 Docker 层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 项目代码
COPY . .

# 数据目录（通过 volume 挂载持久化）
ENV KIOXIA_DATA_DIR=/data
RUN mkdir -p /data/db /data/logs /data/credentials /data/stickers /data/klee-stickers /data/files /data/config

# Web UI 端口
EXPOSE 8080

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/api/v1/health/system')" || exit 1

# 启动命令
CMD ["python", "agent.py", "--web", "8080"]
