"""
EntropyGuard · 入口
最小可行后端，部署到阿里云 ECS
[Bug 1 fix] 默认单 worker 防止多进程 state 不一致
"""
import os
import time
import logging
from logging.config import dictConfig

from fastapi import FastAPI
from fastapi.responses import HTMLResponse

from config import Config

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
        "entropyguard": {
            "handlers": ["console", "file"],
            "level": "DEBUG", "propagate": False,
        },
    },
}
dictConfig(LOGGING_CONFIG)

# ========== 创建应用 ==========
app = FastAPI(title="EntropyGuard · 让AI的自主程度看得见、管得住、说得清")

# 注册路由
from routes.api import router as api_router
from routes.ws import setup_websocket

app.include_router(api_router)
setup_websocket(app)

# ========== 前端页面 ==========

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@app.get("/", response_class=HTMLResponse)
async def serve_twin():
    html_path = os.path.join(STATIC_DIR, "twin.html")
    if os.path.exists(html_path):
        with open(html_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    return HTMLResponse("<h1>EntropyGuard</h1><p>twin.html not found. Please place it in static/</p>")
