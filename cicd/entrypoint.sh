#!/bin/bash
set -e

echo "========================================="
echo "  OpenCapyBox All-in-One Container"
echo "========================================="

# 环境变量由 K8s envFrom (ConfigMap/Secret) 直接注入，无需 .env 文件

# 初始化数据库（幂等操作，已存在则跳过）
echo "[INFO] 初始化数据库..."
python /app/init_db.py

# 启动后端（后台运行）
echo "[INFO] 启动后端 uvicorn (port 8000)..."
uvicorn src.api.main:app \
    --host 127.0.0.1 \
    --port 8000 \
    --workers "${UVICORN_WORKERS:-2}" \
    --log-level "${LOG_LEVEL:-info}" &

BACKEND_PID=$!

# 等待后端就绪
echo "[INFO] 等待后端就绪..."
for i in $(seq 1 30); do
    if curl -sf http://127.0.0.1:8000/api/v1/docs > /dev/null 2>&1; then
        echo "[INFO] 后端已就绪"
        break
    fi
    sleep 1
done

# 启动 nginx（前台运行，作为主进程）
echo "[INFO] 启动 nginx (port 80)..."
nginx -g 'daemon off;' &

NGINX_PID=$!

# 捕获信号，优雅退出
trap "echo '[INFO] 正在停止...'; kill $BACKEND_PID $NGINX_PID 2>/dev/null; wait; exit 0" SIGTERM SIGINT SIGQUIT

echo "========================================="
echo "  服务已启动"
echo "  前端:  http://0.0.0.0:80"
echo "  后端:  http://127.0.0.1:8000"
echo "========================================="

# 等待任意子进程退出
wait -n $BACKEND_PID $NGINX_PID
EXIT_CODE=$?

echo "[WARN] 子进程退出 (code=$EXIT_CODE)，正在关闭容器..."
kill $BACKEND_PID $NGINX_PID 2>/dev/null
wait
exit $EXIT_CODE
