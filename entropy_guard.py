"""
Entropy Runtime · 安全层
Layer 0: 输入意图预检
Shell 命令白名单 + 危险模式拦截
"""
import base64
import re
import unicodedata
from typing import Tuple

from config import Config, GEAR_MAP


# ========== 解码预处理 (v2.1) ==========

def _decode_preprocess(text: str) -> str:
    """
    对文本进行解码预处理：
    1. 提取并解码 base64 编码的字符串
    2. 提取并解码 hex 编码的字符串
    3. NFKC 规范化 Unicode（全角→半角转换）

    返回原始文本 + 解码后的文本（用于信号匹配）
    """
    if not text:
        return ""

    decoded_parts = [text]

    # 1. 提取并解码 base64 字符串
    b64_patterns = [
        r'echo\s+([A-Za-z0-9+/=]{8,})\|base64',
        r'([A-Za-z0-9+/=]{8,})\s*\|base64',
        r'base64\s+-d\s*<<<\s*["\']?([A-Za-z0-9+/=]+)["\']?',
    ]
    for pat in b64_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                decoded = base64.b64decode(m.group(1)).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    decoded_parts.append(decoded)
            except Exception:
                pass

    # 独立 base64 块（长度 ≥ 12）
    for m in re.finditer(r'([A-Za-z0-9+/=]{12,})', text):
        try:
            decoded = base64.b64decode(m.group(1)).decode('utf-8', errors='replace')
            if decoded and any(c in decoded for c in (' ', '/', '.', '-', '|')):
                decoded_parts.append(decoded)
        except Exception:
            pass

    # 2. 提取并解码 hex 字符串
    hex_patterns = [
        r"echo\s+'([0-9a-fA-F]{8,})'\s*\|\s*xxd",
        r'([0-9a-fA-F]{8,})\s*\|\s*xxd',
    ]
    for pat in hex_patterns:
        for m in re.finditer(pat, text):
            try:
                decoded = bytes.fromhex(m.group(1)).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    decoded_parts.append(decoded)
            except Exception:
                pass

    # 3. URL 解码（含双重编码）
    import urllib.parse as _up
    url_decoded = text
    for _ in range(5):  # 最多递归解码5层（处理双重/三重编码）
        prev = url_decoded
        url_decoded = _up.unquote(url_decoded)
        if url_decoded == prev:
            break
    if url_decoded != text:
        decoded_parts.append(url_decoded)

    # 4. NFKC 规范化 Unicode
    nfkc_normalized = unicodedata.normalize('NFKC', text)
    if nfkc_normalized != text:
        decoded_parts.append(nfkc_normalized)

    # 去重
    seen = set()
    result_parts = []
    for part in decoded_parts:
        normalized = part.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result_parts.append(part)

    return "\n".join(result_parts)


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

# ========== 写入操作检测 ==========

def _is_write_command(command: str) -> bool:
    """判断命令是否为写入/破坏性操作"""
    write_signals = [
        r"^\s*rm\s+", r"^\s*mv\s+", r"^\s*cp\s+", r"^\s*dd\s+",
        r"^\s*>\s*", r"^\s*echo\s+.*>\s*", r"^\s*cat\s+.*>\s*",
        r"^\s*printf\s+.*>\s*", r"^\s*truncate\s+", r"^\s*fallocate\s+",
        r"chmod\s+", r"chown\s+", r"chattr\s+",
        r"mkfs", r"format", r"fdisk", r"mkswap",
        r"install\s+", r"rsync\s+",
        r"wget\s+.*-O\s+", r"curl\s+.*-o\s+", r"curl\s+.*-O\s+",
    ]
    cmd_lower = command.strip().lower()
    for pat in write_signals:
        if re.search(pat, cmd_lower):
            return True
    return False


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
    r"nc\s+-[ecl]\s+\S+\s+\S+\s+\d+",            # nc -e /bin/sh HOST PORT (反向shell)
    r"nc\s+-[ecl].*-p\s*\d+",                               # nc -e -p PORT
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


def validate_command(command: str, gear: int = 1) -> Tuple[bool, str]:
    """验证 shell 命令安全性。返回 (是否允许, 拒绝原因)
    gear: 当前档位（默认 1=EMBRACE），gear≥2 时启用系统目录写入保护"""
    stripped = command.strip()
    if not stripped:
        return False, "空命令"

    # [v2.1] 解码预处理：对命令进行 base64/hex/unicode 解码后也做安全校验
    decoded = _decode_preprocess(stripped)
    check_texts = [stripped, decoded] if decoded != stripped else [stripped]

    for check_text in check_texts:
        # 受保护路径
        cmd_lower = check_text.lower()
        for protected in PROTECTED_PATHS:
            if protected.lower() in cmd_lower:
                return False, f"安全限制：禁止访问受保护路径 '{protected}'"

        # [v6.0.1] gear≥2 禁止写入 /etc /root /dev /var/log /boot（仅在写入操作时触发）
        if gear >= 2 and _is_write_command(check_text):
            write_protected = ["/etc", "/root", "/dev", "/var/log", "/boot"]
            for wp in write_protected:
                # 匹配路径边界，避免误伤 /etc/hostname 等读取
                if re.search(r'(?<!/)' + re.escape(wp) + r'(?:/|$)', cmd_lower):
                    return False, f"安全限制：gear≥2 禁止写入系统目录 '{wp}'"

        # 危险正则
        for pattern in BLOCKED_REGEX:
            if pattern.search(check_text):
                return False, f"命令匹配危险模式: {pattern.pattern}"

        # 拆分复合命令
        separators = ["|", "&&", "||", ";", "$(", "`", "$(("]
        commands_to_check = [check_text]
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
            "修改文件", "删除文件", "删除", "清空", "清理",
            "rm ", "rm -rf",
        ],
        "min_gear": 3,
        "reason": "请求涉及文件写入操作，需要 ADAPT 权限",
    },
    "remote_exec": {
        "signals": [
            "$(", "`", "反引号",
            "curl | sh", "curl|sh", "wget | sh", "wget|sh",
            "curl | bash", "curl|bash", "wget | bash", "wget|bash",
            "执行这个命令", "执行以下命令", "运行这个命令",
            "解码并执行", "decode and execute",
            "| base64 -d |", "| base64 --decode",
            "|base64 -d|", "|base64 -d |", "| base64 -d|",
            "|base64|", "|base64 ",
            "| xxd -r -p |", "|xxd -r -p|",
            "|bash", "| sh ", "|sh ",
            "mkfs", "mkfs.ext", "格式化磁盘",
            "/dev/sd", "/dev/nvme",
            "nc -e", "nc -c",               # 反向 shell（netcat -e）
            "ncat ", "netcat ",              # ncat/netcat
            "反向 shell", "reverse shell",   # 反向 shell 中文/英文
            "先定义变量",                     # 变量拆分构建危险命令
            "然后执行",                       # 分步执行提示
        ],
        "min_gear": 4,
        "reason": "请求涉及命令替换或远程管道执行，需要 LET_GO 权限",
    },
}


def check_input_intent(message: str, current_gear: int) -> dict:
    """Layer 0: 扫描用户输入，检测需要更高权限的意图"""
    # [v2.1] 解码预处理：对用户输入进行 base64/hex/unicode 解码后也做意图检测
    message_decoded = _decode_preprocess(message)
    # 意图检测：先对解码后的文本做检查
    message_lower = message_decoded.lower()
    orig_lower = message.lower()

    # [v3.8.2 fix] 解码内容快速拦截：检查解码新增的部分中是否有危险命令链
    # 这避免了 LLM 处理复杂编码内容导致的超时
    import unicodedata as _ud
    _fast_block_signals = ["|bash", "| sh", "| base64 -d"]
    if message_lower != orig_lower and current_gear < 4:
        # NFKC 归一化原始文本后再比较（_decode_preprocess 内部做了 NFKC）
        _orig_nfkc = _ud.normalize('NFKC', orig_lower)
        for _part in message_decoded.split('\n'):
            _p = _part.strip().lower()
            if not _p or _p in orig_lower or _p in _orig_nfkc:
                continue  # 跳过已在原始文本中的部分
            for _sig in _fast_block_signals:
                if _sig in _p:
                    return {
                        "needs_upgrade": True,
                        "target_gear": 4,
                        "reason": f"解码新增内容包含危险命令链 '{_sig}'，需要 LET_GO 权限",
                        "matched_signal": _sig,
                    }

    # 代码执行优先判断
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

    # 兜底：对原始消息也做一次检查
    orig_lower = message.lower()
    if any(kw in orig_lower for kw in code_keywords) and current_gear < 3:
        return {
            "needs_upgrade": True,
            "target_gear": 3,
            "reason": "涉及代码执行，需要 ADAPT 权限",
            "matched_signal": next(kw for kw in code_keywords if kw in orig_lower),
        }

    return {"needs_upgrade": False}
