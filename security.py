"""
EntropyGuard · 安全层
Layer 0: 输入意图预检
Shell 命令白名单 + 危险模式拦截
"""
import re
from typing import Tuple

from config import Config, GEAR_MAP


# ========== Shell 安全 ==========

ALLOWED_COMMANDS = [
    "ls", "cat", "head", "tail", "less", "more", "file", "wc", "grep",
    "find", "du", "df", "stat", "tree", "pwd",
    "ps", "top", "htop", "free", "uptime", "whoami", "id", "uname",
    "date", "cal", "env", "lsusb", "lscpu", "lsmem", "lsblk",
    "ping", "curl", "wget", "nslookup", "dig", "traceroute", "netstat", "ss",
    "echo", "printf", "sort", "uniq", "awk", "sed", "cut", "tr", "rev",
    "xargs", "which", "whereis",
    "python", "python3", "pip", "pip3",
    "git", "nvidia-smi", "docker", "docker-compose", "kubectl",
    "tar", "zip", "unzip", "gzip", "gunzip", "bzip2", "bunzip2",
    "make", "gcc", "g++", "go", "rustc", "node", "npm",
    "mkdir", "touch", "cp", "mv", "chmod", "chown", "rm",
]

BLOCKED_PATTERNS = [
    r"rm\s+-rf\s+/[\s;]",
    r"rm\s+-rf\s+/\s*$",
    r">\s*/dev/\w+",
    r"mkfs\.\w+",
    r"dd\s+if=.*of=/dev/\w+",
    r"shutdown\b", r"reboot\b", r"halt\b", r"init\s+0",
    r":\(\)\{\s*:\|:&\s*\};:",
    r"curl\s+.*\|\s*sh",
    r"wget\s+.*-\s*O-\s*\|\s*sh",
    r"nc\s+-[ecl].*-p\s*\d+",
    r"bash\s+-i\s*>&\s*/dev/tcp/",
    r"python\w*\s+-c\s*.*socket.*subprocess",
    r"chmod\s+777\s+/\b",
    r"useradd\b", r"userdel\b", r"groupadd\b", r"groupdel\b",
    r"passwd\b",
    r"sudo\b", r"su\b",
    r"chroot\b",
]

BLOCKED_REGEX = [re.compile(p, re.IGNORECASE) for p in BLOCKED_PATTERNS]

PROTECTED_PATHS = [
    "/root/EntropyGuard/main.py",
    "/root/EntropyGuard/",
    "main.py",
    ".py",
    "__pycache__",
    "/etc/passwd",
    "/etc/shadow",
    "/root/.ssh/",
    "/root/.bash_history",
]


def validate_command(command: str) -> Tuple[bool, str]:
    """验证 shell 命令安全性。返回 (是否允许, 拒绝原因)"""
    stripped = command.strip()
    if not stripped:
        return False, "空命令"

    # 受保护路径
    cmd_lower = stripped.lower()
    for protected in PROTECTED_PATHS:
        if protected.lower() in cmd_lower:
            return False, f"安全限制：禁止访问受保护路径 '{protected}'"

    # 危险正则
    for pattern in BLOCKED_REGEX:
        if pattern.search(stripped):
            return False, f"命令匹配危险模式: {pattern.pattern}"

    # 拆分复合命令
    separators = ["|", "&&", "||", ";", "$(", "`", "$(("]
    commands_to_check = [stripped]
    for sep in separators:
        new_commands = []
        for cmd in commands_to_check:
            for part in cmd.split(sep):
                part = part.strip()
                if part:
                    new_commands.append(part)
        commands_to_check = new_commands

    # 白名单检查
    for cmd in commands_to_check:
        cmd_stripped = cmd.strip()
        if not cmd_stripped:
            continue
        first_token = cmd_stripped.split()[0].lower().lstrip("(")
        if "/" in first_token:
            first_token = first_token.split("/")[-1]
        if first_token not in ALLOWED_COMMANDS:
            return False, f"命令 '{first_token}' 不在白名单中"

    return True, ""


# ========== Layer 0: 输入意图预检 ==========

INTENT_SIGNALS = {
    "execute": {
        "signals": [
            "执行", "运行", "跑一下", "run", "execute",
            "shell", "命令行", "终端", "bash",
            "检查", "查看", "看一下", "看看",
            "状态", "服务状态", "系统状态",
            "排查", "诊断", "监控",
        ],
        "min_gear": 3,
        "reason": "请求包含执行/查看类意图，需要 ADAPT 权限",
    },
    "network": {
        "signals": [
            "访问", "请求", "调用api", "调用接口", "fetch",
            "http", "https", "get请求", "post请求",
            "爬取", "抓取",
        ],
        "min_gear": 3,
        "reason": "请求涉及网络访问，需要 ADAPT 权限",
    },
    "send": {
        "signals": [
            "发送", "发给", "转发", "推送", "通知",
            "发邮件", "发消息", "发短信",
        ],
        "min_gear": 3,
        "reason": "请求涉及发送操作，需要 ADAPT 权限",
    },
    "file_write": {
        "signals": [
            "创建文件", "写入文件", "保存为", "导出到",
            "修改文件", "删除文件",
        ],
        "min_gear": 3,
        "reason": "请求涉及文件写入操作，需要 ADAPT 权限",
    },
}


def check_input_intent(message: str, current_gear: int) -> dict:
    """Layer 0: 扫描用户输入，检测需要更高权限的意图"""
    message_lower = message.lower()

    # 代码执行优先判断 — 只要包含代码相关关键词，直接判为需要 ADAPT 权限
    # 优先级最高，不会被后面的规则覆盖
    code_keywords = [
        "python", "代码", "脚本", "script", "code interpreter",
        "执行代码", "运行代码", "跑代码",
    ]
    if any(kw in message_lower for kw in code_keywords) and current_gear < 3:
        return {
            "needs_upgrade": True,
            "target_gear": 3,
            "reason": "涉及代码执行，需要 ADAPT 权限",
            "matched_signal": next(kw for kw in code_keywords if kw in message_lower),
        }

    highest_required = current_gear
    matched_reason = ""
    matched_signal = ""

    for intent_type, config in INTENT_SIGNALS.items():
        if config["min_gear"] <= current_gear:
            continue
        for signal in config["signals"]:
            if signal.lower() in message_lower:
                if config["min_gear"] > highest_required:
                    highest_required = config["min_gear"]
                    matched_reason = config["reason"]
                    matched_signal = signal
                break

    if highest_required > current_gear:
        return {
            "needs_upgrade": True,
            "target_gear": highest_required,
            "reason": matched_reason,
            "matched_signal": matched_signal,
        }
    return {"needs_upgrade": False}
