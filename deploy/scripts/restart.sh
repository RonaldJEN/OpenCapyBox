#!/usr/bin/env bash
set -e

# ============================================
# 使用说明
# ============================================
# bash restart.sh          # 重启全部（默认）
# bash restart.sh backend  # 只重启后端
# bash restart.sh frontend # 只重启前端
# bash restart.sh all      # 重启全部
# ============================================

TARGET="${1:-all}"

# 尝试启用 Docker BuildKit 获得更好的缓存性能（如果可用）
if command -v docker >/dev/null 2>&1 && docker buildx version >/dev/null 2>&1; then
    export DOCKER_BUILDKIT=1
    echo "✓ 使用 Docker BuildKit 加速构建"
else
    unset DOCKER_BUILDKIT
    echo "ℹ 使用传统 Docker 构建方式"
fi

# ============================================
# 后端服务函数
# ============================================
restart_backend() {
    echo ""
    echo "🔧 ============================================"
    echo "   重启后端服务"
    echo "   ============================================"
    
    echo "📦 停止并删除旧的后端容器..."
    docker rm -f agentskills-backend 2>/dev/null || true

    echo "🔨 构建后端镜像..."
    if [ "$DOCKER_BUILDKIT" = "1" ]; then
        docker build \
          --build-arg BUILDKIT_INLINE_CACHE=1 \
          -f Dockerfile.backend \
          -t agentskills-backend:latest \
          .
    else
        docker build \
          -f Dockerfile.backend \
          -t agentskills-backend:latest \
          .
    fi

    echo "🚀 启动后端容器..."
    docker run -d \
      --name agentskills-backend \
      -p 8000:8000 \
      -v /data/backend/data:/app/backend/data \
      -e TZ=Asia/Taipei \
      -e TIMEZONE=Asia/Taipei \
      --restart unless-stopped \
      agentskills-backend:latest
    
    echo "✅ 后端重启完成: http://localhost:8000"
}

# ============================================
# 前端服务函数
# ============================================
restart_frontend() {
    echo ""
    echo "🎨 ============================================"
    echo "   重启前端服务"
    echo "   ============================================"
    
    echo "📦 停止并删除旧的前端容器..."
    docker rm -f agentskills-frontend 2>/dev/null || true

    echo "🔨 构建前端镜像..."
    if [ "$DOCKER_BUILDKIT" = "1" ]; then
        docker build \
          --build-arg BUILDKIT_INLINE_CACHE=1 \
          -f Dockerfile.frontend \
          -t agentskills-frontend:latest \
          .
    else
        docker build \
          -f Dockerfile.frontend \
          -t agentskills-frontend:latest \
          .
    fi

    echo "🚀 启动前端容器..."
    docker run -d \
      --name agentskills-frontend \
      -p 8812:80 \
      --add-host=host.docker.internal:host-gateway \
      --restart unless-stopped \
      agentskills-frontend:latest
    
    echo "✅ 前端重启完成: http://localhost:8812"
}

# ============================================
# 主逻辑
# ============================================
echo "🚀 AgentSkills 服务管理脚本"
echo "   目标: $TARGET"

case "$TARGET" in
    backend|b)
        restart_backend
        ;;
    frontend|f)
        restart_frontend
        ;;
    all|"")
        restart_backend
        restart_frontend
        ;;
    *)
        echo "❌ 未知参数: $TARGET"
        echo ""
        echo "使用方法:"
        echo "  bash restart.sh          # 重启全部"
        echo "  bash restart.sh backend  # 只重启后端 (或 b)"
        echo "  bash restart.sh frontend # 只重启前端 (或 f)"
        echo "  bash restart.sh all      # 重启全部"
        exit 1
        ;;
esac

echo ""
echo "============================================"
echo "✅ 操作完成！"
echo "   前端: http://localhost:8812"
echo "   后端: http://localhost:8000"
echo ""
echo "💡 提示: 只需重启后端时使用 'bash restart.sh backend'"
echo "============================================"