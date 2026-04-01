#!/bin/bash
# AgentSkills 开发环境启动脚本
# 用法: ./deploy/scripts/dev_run.sh [backend|frontend|all]

set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 项目根目录 (deploy/scripts 的上两级)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
FRONTEND_DIR="$PROJECT_ROOT/frontend"

# 打印带颜色的消息
info() { echo -e "${BLUE}[INFO]${NC} $1"; }
success() { echo -e "${GREEN}[SUCCESS]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; }

# Python 运行方式检测：uv > conda(agentskill) > system python
PYTHON_CMD=""
USE_UV=false

detect_python() {
    # 优先使用项目 .venv（跨平台最可靠）
    if [ -f "$PROJECT_ROOT/.venv/bin/python" ]; then
        PYTHON_CMD="$PROJECT_ROOT/.venv/bin/python"
        info "使用项目 .venv 环境"
    elif [ -f "$PROJECT_ROOT/.venv/Scripts/python.exe" ]; then
        PYTHON_CMD="$PROJECT_ROOT/.venv/Scripts/python.exe"
        info "使用项目 .venv 环境 (Windows)"
    elif [ -n "$CONDA_PREFIX" ] && [ "$(basename $CONDA_PREFIX)" = "agentskill" ]; then
        PYTHON_CMD="python"
        info "使用已激活的 conda agentskill 环境"
    elif [ -f "$HOME/miniconda3/envs/agentskill/bin/python" ]; then
        PYTHON_CMD="$HOME/miniconda3/envs/agentskill/bin/python"
        info "使用 conda agentskill 环境"
    elif [ -n "$CONDA_PREFIX" ] && command -v python &> /dev/null; then
        PYTHON_CMD="python"
        info "使用 conda 环境: $(basename $CONDA_PREFIX)"
    elif command -v uv &> /dev/null; then
        USE_UV=true
        PYTHON_CMD="uv run python"
        info "使用 uv 管理 Python 环境"
    elif command -v python &> /dev/null; then
        PYTHON_CMD="python"
        info "使用系统 Python"
    else
        error "未找到 Python 环境，请安装 conda 或 uv"
        exit 1
    fi
}

# 检查依赖
check_dependencies() {
    info "检查依赖..."

    detect_python

    # 检查 Node.js
    if ! command -v node &> /dev/null; then
        error "Node.js 未安装，请先安装 Node.js 16+"
        exit 1
    fi

    # 检查 npm
    if ! command -v npm &> /dev/null; then
        error "npm 未安装"
        exit 1
    fi

    success "依赖检查通过"
}

# 启动后端
start_backend() {
    info "启动后端服务..."
    cd "$PROJECT_ROOT"

    # 清理残留进程
    kill_port 8000

    # 设置 PYTHONPATH
    export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

    # 同步依赖
    info "同步后端依赖..."
    if $USE_UV; then
        uv sync
    fi

    # 检查 .env 文件
    if [ ! -f ".env" ]; then
        warn ".env 文件不存在，请确保已配置环境变量"
        if [ -f ".env.example" ]; then
            info "可以复制 .env.example 作为模板: cp .env.example .env"
        fi
    fi

    success "后端启动于 http://localhost:8000"
    success "API 文档: http://localhost:8000/api/v1/docs"
    $PYTHON_CMD -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000 --reload-dir src
}

# 启动前端
start_frontend() {
    info "启动前端服务..."
    cd "$FRONTEND_DIR"
    
    # 检查是否需要安装依赖
    if [ ! -d "node_modules" ]; then
        info "安装前端依赖..."
        npm install
    fi
    
    success "前端启动于 http://localhost:3000"
    npm run dev
}

# 清理指定端口上的残留进程
kill_port() {
    local port=$1
    local pids
    if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
        # Windows (Git Bash / MSYS2): 使用 netstat + taskkill
        pids=$(netstat -ano 2>/dev/null | grep ":${port} .*LISTENING" | awk '{print $5}' | sort -u)
    else
        # Linux / macOS: 使用 lsof
        pids=$(lsof -ti ":${port}" 2>/dev/null || true)
    fi
    if [ -n "$pids" ]; then
        warn "端口 ${port} 被占用，正在清理残留进程: $pids"
        for pid in $pids; do
            if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
                taskkill //F //PID "$pid" 2>/dev/null || true
            else
                kill -9 "$pid" 2>/dev/null || true
            fi
        done
        sleep 1
    fi
}

# 清理所有子进程（包括 uvicorn --reload 的 worker 子进程）
cleanup() {
    info "正在停止所有服务..."
    # 杀掉整个进程组（包括子进程）
    if [ -n "$BACKEND_PID" ]; then
        # 先尝试杀进程树
        if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
            taskkill //F //T //PID "$BACKEND_PID" 2>/dev/null || true
        else
            kill -- -"$BACKEND_PID" 2>/dev/null || kill "$BACKEND_PID" 2>/dev/null || true
        fi
    fi
    if [ -n "$FRONTEND_PID" ]; then
        if [[ "$OSTYPE" == "msys" || "$OSTYPE" == "cygwin" || "$OSTYPE" == "win32" ]]; then
            taskkill //F //T //PID "$FRONTEND_PID" 2>/dev/null || true
        else
            kill -- -"$FRONTEND_PID" 2>/dev/null || kill "$FRONTEND_PID" 2>/dev/null || true
        fi
    fi
    exit 0
}

# 同时启动前后端
start_all() {
    info "同时启动前后端服务..."

    # 清理残留进程
    kill_port 8000
    kill_port 3000

    # 设置 PYTHONPATH
    export PYTHONPATH="$PROJECT_ROOT:$PYTHONPATH"

    # 启动后端（后台运行）
    cd "$PROJECT_ROOT"
    info "同步后端依赖..."
    if $USE_UV; then
        uv sync -q
    fi

    info "启动后端 (http://localhost:8000)..."
    $PYTHON_CMD -m uvicorn src.api.main:app --reload --host 0.0.0.0 --port 8000 --reload-dir src &
    BACKEND_PID=$!
    
    # 等待后端启动
    sleep 3
    
    # 启动前端（后台运行）
    cd "$FRONTEND_DIR"
    if [ ! -d "node_modules" ]; then
        npm install
    fi
    
    info "启动前端 (http://localhost:3000)..."
    npm run dev &
    FRONTEND_PID=$!
    
    success "========================================"
    success "  AgentSkills 开发服务器已启动"
    success "========================================"
    success "  前端: http://localhost:3000"
    success "  后端: http://localhost:8000"
    success "  文档: http://localhost:8000/api/v1/docs"
    success "========================================"
    info "按 Ctrl+C 停止所有服务"
    
    # 捕获退出信号，清理所有子进程
    trap cleanup SIGINT SIGTERM
    
    # 等待进程
    wait
}

# 显示帮助
show_help() {
    echo "AgentSkills 开发环境启动脚本"
    echo ""
    echo "用法: ./deploy/scripts/dev_run.sh [命令]"
    echo ""
    echo "命令:"
    echo "  backend   只启动后端服务 (端口 8000)"
    echo "  frontend  只启动前端服务 (端口 3000)"
    echo "  all       同时启动前后端 (默认)"
    echo "  help      显示此帮助信息"
    echo ""
    echo "示例:"
    echo "  ./deploy/scripts/dev_run.sh          # 同时启动前后端"
    echo "  ./deploy/scripts/dev_run.sh backend  # 只启动后端"
    echo "  ./deploy/scripts/dev_run.sh frontend # 只启动前端"
}

# 主函数
main() {
    echo ""
    echo "=========================================="
    echo "   AgentSkills 开发环境启动脚本"
    echo "=========================================="
    echo ""
    
    check_dependencies
    
    case "${1:-all}" in
        backend)
            start_backend
            ;;
        frontend)
            start_frontend
            ;;
        all)
            start_all
            ;;
        help|--help|-h)
            show_help
            ;;
        *)
            error "未知命令: $1"
            show_help
            exit 1
            ;;
    esac
}

main "$@"
