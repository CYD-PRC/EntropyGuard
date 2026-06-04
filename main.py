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

# [v6.1] 公开 Dashboard API（无 Token 认证）
from fastapi import APIRouter
dashboard_router = APIRouter()

@dashboard_router.get("/api/dashboard-data")
async def dashboard_data():
    from audit import state
    from config import GEAR_MAP
    state._load_events()
    state.compute_dynamic_sc()
    evts = state.event_log[-50:]
    total = len(evts)
    blocked = sum(1 for e in evts if (e.get("event_type") or "").endswith("BLOCK") or "block" in (e.get("action") or "").lower())
    redteam_total = 36
    try:
        import json as _j
        with open("/root/EntropyGuard/security/redteam_suite.json") as _f:
            _suite = _j.load(_f)
        redteam_passed = sum(1 for _t in _suite if _t.get("last_result") == "passed")
    except Exception:
        redteam_passed = 0
    sc_vals = [round(e.get("control_entropy", e.get("sc", 0)), 4) for e in evts if e.get("control_entropy") or e.get("sc")]
    sc_labels = [e.get("timestamp", "") for e in evts if e.get("control_entropy") or e.get("sc")]
    return {
        "current_gear": state.current_gear,
        "gear_name": GEAR_MAP.get(state.current_gear, {}).get("name", ""),
        "sc": round(getattr(state, "control_entropy", 0), 4),
        "event_count": total,
        "blocked_count": blocked,
        "block_rate": round(blocked / total * 100, 1) if total else 0,
        "redteam_total": redteam_total,
        "redteam_passed": redteam_passed,
        "redteam_rate": round(redteam_passed / redteam_total * 100) if redteam_total else 0,
        "sc_timeline": {"labels": sc_labels[-30:], "values": sc_vals[-30:]},
        "events": evts[-20:],
        "status": "ok",
        "uptime": round(time.time() - getattr(state, "last_switch_time", time.time()), 1),
    }

app.include_router(dashboard_router)
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
