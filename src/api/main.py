"""FastAPI 主应用"""
import asyncio
import logging
import platform
import sys
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.api.config import get_settings
from src.api.routes import auth, sessions, chat, models
from src.api.routes import cron as cron_routes
from src.api.routes import config as config_routes
from src.api.models.database import init_db
import os

# 配置日志等级（从环境变量 LOG_LEVEL 读取，默认 INFO）
log_level = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, log_level, logging.INFO),
    format="%(levelname)s:     %(name)s - %(message)s",
)

# 修复 Windows 平台特定问题
if platform.system() == "Windows":
    # 1. 修复 asyncio 子进程问题
    # 使用 WindowsSelectorEventLoopPolicy 而不是默认的 WindowsProactorEventLoopPolicy
    # 这样可以支持子进程创建（BashTool 需要）
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # 2. 修复控制台编码问题
    # Windows 默认使用 GBK/CP936，无法输出 Unicode 特殊字符（如数学符号）
    # 设置 UTF-8 编码确保子进程输出能正确处理
    if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
        sys.stderr.reconfigure(encoding='utf-8', errors='replace')

settings = get_settings()

# 创建 FastAPI 应用
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url=f"{settings.api_prefix}/docs",
    redoc_url=f"{settings.api_prefix}/redoc",
)

# CORS 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 启动事件
@app.on_event("startup")
async def startup_event():
    """应用启动时执行"""
    # 初始化数据库
    init_db()
    print(f"✅ 数据库初始化完成")

    # 清理上次进程残留的 running 轮次（服务器重启后 Agent 已不再运行）
    try:
        from src.api.models.round import Round
        from src.api.models.database import SessionLocal
        from src.api.utils.timezone import now_naive

        with SessionLocal() as db:
            stale_count = (
                db.query(Round)
                .filter(Round.status == "running")
                .update({
                    "status": "failed",
                    "completed_at": now_naive(),
                    "final_response": "[系统重启，执行被中断]",
                })
            )
            # 重启后内存中的 _pending_interrupt 已丢失，interrupted 轮次无法恢复
            zombie_count = (
                db.query(Round)
                .filter(Round.status == "interrupted")
                .update({
                    "status": "failed",
                    "completed_at": now_naive(),
                    "interrupt_payload": None,
                    "final_response": "[系统重启，中断问答已失效]",
                })
            )
            db.commit()
            if stale_count:
                print(f"⚠️  已清理 {stale_count} 个残留的 running 轮次（标记为 failed）")
            if zombie_count:
                print(f"⚠️  已清理 {zombie_count} 个残留的 interrupted 轮次（标记为 failed）")
    except Exception as e:
        print(f"⚠️  清理残留轮次失败: {e}")

    # 校驗 Model Registry（啟動時預檢，自動停用 key 缺失的模型）
    try:
        from src.api.model_registry import get_model_registry
        registry = get_model_registry()
        enabled = registry.list_models(enabled_only=True)
        print(f"✅ Model Registry 就緒: {len(enabled)} 個可用模型, 默認: {registry.default_model_id}")
        for m in enabled:
            print(f"   📦 {m.id} ({m.display_name}) — {m.provider}")
    except Exception as e:
        print(f"⚠️  Model Registry 加載失敗: {e}")
        print(f"   將使用 .env 中的 LLM_API_KEY/LLM_API_BASE/LLM_MODEL 作為 fallback")

    print(f"✅ {settings.app_name} v{settings.app_version} 启动成功")

    # 启动 APScheduler 并注册用户 Cron 任务
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from src.api.services.cron_service import register_user_jobs
        from src.api.models.user_sandbox import UserSandbox
        from src.api.models.database import SessionLocal

        scheduler = AsyncIOScheduler()
        with SessionLocal() as db:
            active_users = (
                db.query(UserSandbox.user_id)
                .filter(UserSandbox.status == "active")
                .all()
            )
            total_jobs = 0
            for (uid,) in active_users:
                total_jobs += register_user_jobs(db, uid, scheduler)

        if total_jobs > 0:
            scheduler.start()
            app.state.scheduler = scheduler
            print(f"✅ APScheduler 已启动: {total_jobs} 个 Cron 任务")
        else:
            scheduler.start()
            app.state.scheduler = scheduler
            print(f"ℹ️  无活跃 Cron 任务，APScheduler 已就绪")
    except ImportError:
        print("⚠️  APScheduler 未安装，Cron 自动调度不可用")
    except Exception as e:
        print(f"⚠️  Cron 调度注册失败: {e}")


# 路由
app.include_router(auth.router, prefix=f"{settings.api_prefix}/auth", tags=["认证"])
app.include_router(
    sessions.router, prefix=f"{settings.api_prefix}/sessions", tags=["会话管理"]
)
app.include_router(chat.router, prefix=f"{settings.api_prefix}/chat", tags=["对话"])
app.include_router(models.router, prefix=f"{settings.api_prefix}/models", tags=["模型管理"])
app.include_router(cron_routes.router, prefix=f"{settings.api_prefix}/cron", tags=["定时任务"])
app.include_router(config_routes.router, prefix=f"{settings.api_prefix}/config", tags=["配置管理"])


# 根路径
@app.get("/")
async def root():
    return {
        "message": "OpenCapyBox API",
        "version": settings.app_version,
        "docs": f"{settings.api_prefix}/docs",
    }


# 健康检查
@app.get("/health")
async def health():
    return {"status": "healthy", "version": settings.app_version}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000, reload=settings.debug)
