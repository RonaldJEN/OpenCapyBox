# ============================================================
# OpenCapyBox All-in-One Dockerfile
# 前后端合并部署，适合公司内部 K8s 流水线
# 
# 用法:
#   docker build -f cicd/Dockerfile -t opencapybox:latest .
#
# 本地测试:
#   docker run -d \
#     -e API_KEY=xxx -e DB_URL=xxx \
#     -v /your/data/path:/app/data \
#     -p 80:80 \
#     opencapybox:latest
#
# K8s 部署:
#   环境变量通过 ConfigMap/Secret envFrom 注入
#   数据目录挂载 PVC 到 /app/data
# ============================================================

# ========================
# Stage 1: 构建前端
# ========================
FROM node:20-alpine AS frontend-builder

WORKDIR /build
RUN npm config set registry https://registry.npmmirror.com

COPY frontend/package*.json ./
RUN npm ci

COPY frontend/ ./
RUN npm run build

# ========================
# Stage 2: 最终镜像
# ========================
FROM python:3.11-slim

WORKDIR /app

# ---------- 系统依赖 ----------
RUN apt-get update && apt-get install -y --no-install-recommends \
    nginx \
    curl \
    git \
    bash \
    build-essential \
    pandoc \
    libreoffice \
    poppler-utils \
    # Playwright 浏览器依赖
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

# ---------- Node.js (用于 agent 工具链) ----------
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# ---------- 镜像源配置 (加速公司内部构建) ----------
RUN pip config set global.index-url https://pypi.tuna.tsinghua.edu.cn/simple \
    && pip install uv
ENV UV_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
RUN npm config set registry https://registry.npmmirror.com

# ---------- Python 依赖 ----------
COPY pyproject.toml uv.lock requirements.txt ./
RUN mkdir -p src && touch src/__init__.py \
    && uv pip install --system . \
    && uv pip install --system -r requirements.txt

# ---------- npm 全局工具 (agent sandbox 需要) ----------
RUN npm install -g \
    pptxgenjs playwright sharp react react-dom react-icons \
    pdf-lib pdfjs-dist docx @modelcontextprotocol/sdk \
    zod axios fs-extra lodash cheerio marked \
    && npx playwright install chromium

# ---------- 后端代码 ----------
COPY src/ ./src/
COPY models.yaml ./models.yaml
COPY init_db.py ./init_db.py

# ---------- 前端产物 ----------
COPY --from=frontend-builder /build/dist /app/frontend/dist

# ---------- nginx 配置 ----------
COPY cicd/nginx.conf /etc/nginx/conf.d/default.conf
RUN rm -f /etc/nginx/sites-enabled/default

# ---------- 入口脚本 ----------
COPY cicd/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

# ---------- 数据目录 (用于挂载) ----------
RUN mkdir -p /app/data/database /app/data/workspaces /app/data/shared_env

# ---------- 环境变量 ----------
ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

# 只暴露 80，nginx 统一入口
EXPOSE 80

ENTRYPOINT ["/app/entrypoint.sh"]
