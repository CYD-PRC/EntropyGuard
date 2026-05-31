"""
Entropy Runtime · 配置管理
集中管理所有配置参数，支持环境变量覆盖
"""
import os


class Config:
    # 物理常数
    BOLTZMANN = float(os.environ.get("EG_BOLTZMANN", "1.380649e-23"))
    TEMPERATURE = int(os.environ.get("EG_TEMPERATURE", "300"))

    # 系统路径
    DATA_DIR = os.environ.get("EG_DATA_DIR", "/root/EntropyGuard")
    EVENTS_FILE = os.path.join(DATA_DIR, "events.json")
    LOG_FILE = os.path.join(DATA_DIR, "app.log")
    MCP_CLIENT = os.environ.get("EG_MCP_CLIENT", "/root/EntropyGuard/mcp_client.py")

    # 安全限制
    MAX_TOOL_ROUNDS_GEAR3 = int(os.environ.get("EG_MAX_TOOL_ROUNDS_G3", "50"))
    MAX_TOOL_ROUNDS_GEAR4 = int(os.environ.get("EG_MAX_TOOL_ROUNDS_G4", "80"))
    MAX_TOOL_ROUNDS_DEFAULT = int(os.environ.get("EG_MAX_TOOL_ROUNDS_DEF", "5"))
    SHELL_TIMEOUT = int(os.environ.get("EG_SHELL_TIMEOUT", "30"))
    MEMORY_LIMIT_KB = int(os.environ.get("EG_MEMORY_LIMIT_KB", "524288"))
    CPU_TIME_LIMIT = int(os.environ.get("EG_CPU_TIME_LIMIT", "30"))

    # 空闲惩罚
    IDLE_THRESHOLD_MIN = int(os.environ.get("EG_IDLE_THRESHOLD_MIN", "30"))
    IDLE_PENALTY_PER_MIN = float(os.environ.get("EG_IDLE_PENALTY", "0.01"))

    # 升级冷却
    UPGRADE_COOLDOWN_SEC = int(os.environ.get("EG_UPGRADE_COOLDOWN", "120"))

    # 输出截断
    MAX_STDOUT_BYTES = int(os.environ.get("EG_MAX_STDOUT", "5000"))
    MAX_STDERR_BYTES = int(os.environ.get("EG_MAX_STDERR", "2000"))

    # EMBRACE 回复长度限制
    EMBRACE_MAX_REPLY_CHARS = int(os.environ.get("EG_EMBRACE_MAX_CHARS", "2000"))

    # 熵增系数
    TOOL_ENTROPY_INCREASE = float(os.environ.get("EG_TOOL_ENTROPY", "0.03"))
    VIOLATION_ENTROPY_INCREASE = float(os.environ.get("EG_VIOLATION_ENTROPY", "0.08"))

    # [Bug 1 fix] 消息去重窗口（秒）
    MESSAGE_DEDUP_WINDOW_SEC = int(os.environ.get("EG_DEDUP_WINDOW", "10"))

    # 服务器环境上下文（注入到 AI system prompt）
    SERVER_CONTEXT = """
你运行在以下服务器环境中：
- 系统：Alibaba Cloud Linux 3，内核 5.10，2核 CPU，1.8GB 内存
- Python：3.6（路径 /usr/bin/python3.6）
- Web 服务：Nginx（端口 80）+ Gunicorn/Uvicorn（端口 8000）
- 项目目录：/root/EntropyGuard/
- 脚本部署目录：/opt/scripts/
- 日志目录：/var/log/
- 已安装：Node.js、git、curl
- 未安装：MySQL、PostgreSQL、Redis、Docker（未运行）
- 无 Swap 分区
- 磁盘：40GB，已用约 50%，剩余约 19GB

你是Entropy Runtime项目的一部分。Entropy Runtime 是一个多模型 AI 行为审计系统
核心理念是"Record, don't restrain"（记录而非约束）。
你当前运行在这个系统的 EEAL 协议下，通过四档控制光谱（EMBRACE → EXPLORE → ADAPT → LET GO）
管理自主权限，所有操作都被 SHA-256 审计链记录。
"""

    @classmethod
    def ensure_dirs(cls):
        os.makedirs(cls.DATA_DIR, exist_ok=True)


Config.ensure_dirs()

# 档位映射
GEAR_MAP = {
    1: {"name": "EMBRACE", "sc_range": (0.0, 0.0),   "desc": "完全约束态"},
    2: {"name": "EXPLORE", "sc_range": (0.0, 0.5),   "desc": "有限建议态"},
    3: {"name": "ADAPT",   "sc_range": (0.5, 1.0),   "desc": "自主调整须报告"},
    4: {"name": "LET_GO",  "sc_range": (1.0, float("inf")), "desc": "全自主异常介入"},
}
