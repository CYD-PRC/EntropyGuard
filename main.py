"""
Entropy Runtime · 入口
最小可行后端，部署到阿里云 ECS
[Bug 1 fix] 默认单 worker 防止多进程 state 不一致
"""
import os
import time
import logging
from logging.config import dictConfig

import os
from pathlib import Path
from dotenv import load_dotenv

# [Security fix] 启动时强制加载 /root/.env 环境变量
# 无论 gunicorn 通过 systemd(EnvironmentFile) 启动还是手动 bash 启动，
# 确保 ENTROPY_RUNTIME_API_KEY 等关键凭证可用
env_path = Path("/root/.env")
if env_path.exists():
    load_dotenv(dotenv_path=env_path, override=True)
    if os.environ.get("ENTROPY_RUNTIME_API_KEY"):
        import logging as _logging
        _logging.getLogger("entropyruntime").info(
            "[Auth] ENTROPY_RUNTIME_API_KEY loaded from /root/.env"
        )

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import Config
from messageboard import MessageBoard, get_messageboard  # Multi-Agent MessageBoard
from routes.messageboard_api import router as mb_router, set_board, setup_messageboard_ws


# ========== 日志配置 ==========
LOGGING_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "format": "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
            "datefmt": "%Y-%m-%d %H:%M:%S",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "default", "level": "INFO",
        },
        "file": {
            "class": "logging.FileHandler",
            "filename": Config.LOG_FILE,
            "formatter": "default", "level": "DEBUG",
        },
    },
    "loggers": {
        "entropyruntime": {
            "handlers": ["console", "file"],
            "level": "DEBUG", "propagate": False,
        },
    },
}
dictConfig(LOGGING_CONFIG)

# ========== 创建应用 ==========
app = FastAPI(title="Entropy Runtime · 让AI的自主程度看得见、管得住、说得清")

# 注册路由
from routes.runtime_api import router as runtime_router
from routes.agent_api import router as agent_router
from routes.ws import setup_websocket

app.include_router(runtime_router)
app.include_router(agent_router)
app.include_router(mb_router)
setup_websocket(app)
setup_messageboard_ws(app)

# ========== 前端页面 ==========

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
from fastapi.staticfiles import StaticFiles

import logging as _logging
_logger = _logging.getLogger("entropyruntime")

@app.on_event("startup")
async def init_messageboard():
    """初始化 MessageBoard，自动从 storage_path 加载持久化数据"""
    try:
        data_file = os.path.join(os.path.dirname(__file__), "messageboard_data.json")
        board = get_messageboard(storage_path=data_file)
        set_board(board)
        _logger.info("[MessageBoard] Initialized (storage: %s, messages: %d)",
                     data_file, len(board.messages))
    except Exception as e:
        _logger.error("[MessageBoard] Init error: %s", e)


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/", response_class=HTMLResponse)
async def serve_twin():
    html_path = os.path.join(STATIC_DIR, "twin.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>Entropy Runtime</h1><p>twin.html not found. Please place it in static/</p>")
