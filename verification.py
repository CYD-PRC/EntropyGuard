"""
Entropy Runtime · 输出校验层
Layer 2: 硬约束，检测 AI 输出是否超出当前档位权限

[双重校验策略]
  第一层 — 黑名单 (blocked_signals): 关键词/短语精确匹配
    覆盖: 危险命令、执行报告模式、系统文件路径、编码后特征
    兜底: NFKC 归一化 + 解码预处理后二次匹配

  第二层 — 语义正则 (PIPE_EXEC_RE / ANTI_SANDBOX_RE):
    PIPE_EXEC_RE:    管道执行变体，捕获 | sh/bash/zsh/python/perl 等
    ANTI_SANDBOX_RE: 沙箱逃逸变体，捕获 chroot/unshare/nsenter 等
    NFKC 归一化后二次匹配，确保 Unicode 全角变体也被捕获

[v6.0.2] PIPE_EXEC_RE 覆盖: sh|bash|zsh|csh|ksh|dash|ash|bh|python|perl|ruby|node|nodejs|php
[v6.1.0] ANTI_SANDBOX_RE 覆盖: /proc/self/cgroup|mount /dev|chroot|unshare|nsenter|/proc/1/root|container escape|escape container|docker --privileged
"""
import base64
import re
import unicodedata

from config import Config, GEAR_MAP


# ============================================================
# 第二层 — 语义正则
# ============================================================

# [v6.0.2 fix] 管道执行变体正则 — 捕获 | sh, | bash, | zsh, | dash 等
PIPE_EXEC_RE = re.compile(
    r'\|\s*(sh|bash|zsh|csh|ksh|dash|ash|bh|python\d*|perl|ruby|node\d*|nodejs|php)\b',
    re.IGNORECASE
)


# [v6.1.0] 沙箱逃逸变体正则 — 检测容器逃逸类攻击
ANTI_SANDBOX_RE = re.compile(
    r'(?:'
    r'/proc/self/cgroup'                    # 检测是否在容器内
    r'|mount\s+/dev'                        # 挂载宿主机设备
    r'|chroot\s+\S'                         # 切换根目录（逃逸）
    r'|unshare\s+\S'                        # 取消共享命名空间
    r'|nsenter\s+\S'                        # 进入其他命名空间
    r'|/proc/1/root'                        # 通过 PID 1 访问宿主机
    r'|container[-_\s]?escape\b'            # 容器逃逸显式提及
    r'|escape[-_\s]?container\b'            # escape container
    r'|docker\s+(run|exec|cp)\s+--privileged'   # 提权容器操作
    r')',
    re.IGNORECASE
)


def _has_pipe_exec(text: str) -> tuple[bool, str]:
    """检查文本中是否存在管道到执行器的变体"""
    m = PIPE_EXEC_RE.search(text)
    if m:
        return True, m.group(0).strip()
    return False, ""


def _has_sandbox_escape(text: str) -> tuple[bool, str]:
    """检查文本中是否存在沙箱逃逸模式"""
    m = ANTI_SANDBOX_RE.search(text)
    if m:
        return True, m.group(0).strip()
    return False, ""


# ========== 解码预处理 (v3-alpha.1) ==========

def _decode_preprocess(text: str) -> str:
    """
    对文本进行解码预处理：
    1. NFKC 规范化 Unicode（全角→半角转换）— 先行，确保后续 regex 匹配正确
    2. 在 NFKC 后的文本上提取并解码 base64 编码的字符串
    3. 提取并解码 hex 编码的字符串

    返回原始文本 + 归一化文本 + 解码后的文本（用于信号匹配）
    """
    if not text:
        return ""

    # [v3-alpha.1] NFKC 规范化先于所有 regex 处理
    nfkc_text = unicodedata.normalize('NFKC', text)

    decoded_parts = [text]
    if nfkc_text != text:
        decoded_parts.append(nfkc_text)

    # ▼ 以下所有 regex 在 NFKC 归一化文本上执行 ▼

    # 1. 提取并解码 base64 字符串
    b64_patterns = [
        r'echo\s+([A-Za-z0-9+/=]{8,})\|base64',   # echo <b64>|base64
        r'([A-Za-z0-9+/=]{8,})\s*\|base64',         # <b64> | base64
        r'base64\s+-d\s*<<<\s*[\"\']?([A-Za-z0-9+/=]+)[\"\']?',  # base64 -d <<< <b64>
        r'base64\s+--decode\s*<<<\s*[\"\']?([A-Za-z0-9+/=]+)[\"\']?',  # base64 --decode <<< <b64>
        r'echo\s+([A-Za-z0-9+/=]{8,})\s*\|\s*base64\s+--decode',  # echo <b64> | base64 --decode
    ]
    b64_hits = set()
    for pat in b64_patterns:
        for m in re.finditer(pat, nfkc_text, re.IGNORECASE):
            try:
                decoded = base64.b64decode(m.group(1)).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    b64_hits.add(decoded)
            except Exception:
                pass

    # 也尝试提取独立的 base64 块（长度 >= 12 且看起来像 base64）
    for m in re.finditer(r'([A-Za-z0-9+/=]{12,})', nfkc_text):
        try:
            decoded = base64.b64decode(m.group(1)).decode('utf-8', errors='replace')
            # 检查解码后是否包含可打印 ASCII 命令
            if decoded and any(c in decoded for c in (' ', '/', '.', '-', '|')):
                b64_hits.add(decoded)
        except Exception:
            pass

    decoded_parts.extend(b64_hits)

    # 2. 提取并解码 hex 字符串（在 xxd 或 echo -e 上下文中）
    hex_patterns = [
        r"echo\s+'([0-9a-fA-F]{8,})'\s*\|\s*xxd",  # echo '<hex>' | xxd
        r'([0-9a-fA-F]{8,})\s*\|\s*xxd',             # <hex> | xxd
        r"xxd\s+-r\s*-p\s*<<<\s*[\"\']?([0-9a-fA-F]{4,})[\"\']?",  # xxd -r -p <<< <hex>
        r"echo\s+'([0-9a-fA-F]{8,})'\s*\|\s*xxd\s+-r\s*-p",  # echo '<hex>' | xxd -r -p
    ]
    for pat in hex_patterns:
        for m in re.finditer(pat, nfkc_text):
            hex_str = m.group(1)
            try:
                decoded = bytes.fromhex(hex_str).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    decoded_parts.append(decoded)
            except Exception:
                pass

    # 3. 提取并解码 base32 字符串
    b32_patterns = [
        r'echo\s+([A-Z2-7=]{8,})\s*\|\s*base32\s+-d',  # echo <b32> | base32 -d
        r'([A-Z2-7=]{8,})\s*\|\s*base32\s+-d',          # <b32> | base32 -d
        r'echo\s+([A-Z2-7=]{8,})\s*\|\s*b32decode',      # echo <b32> | b32decode
        r'([A-Z2-7=]{8,})\s*\|\s*b32decode',              # <b32> | b32decode
        r'base32\s+-d\s*<<<\s*[\"\']?([A-Z2-7=]+)[\"\']?',  # base32 -d <<< <b32>
    ]
    b32_hits = set()
    for pat in b32_patterns:
        for m in re.finditer(pat, nfkc_text, re.IGNORECASE):
            try:
                decoded = base64.b32decode(m.group(1)).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    b32_hits.add(decoded)
            except Exception:
                pass

    # 也尝试提取独立的 base32 块（长度 >= 16 且看起来像 base32）
    for m in re.finditer(r'([A-Z2-7=]{16,})', nfkc_text):
        try:
            decoded = base64.b32decode(m.group(1)).decode('utf-8', errors='replace')
            if decoded and any(c in decoded for c in (' ', '/', '.', '-', '|')):
                b32_hits.add(decoded)
        except Exception:
            pass

    decoded_parts.extend(b32_hits)

    # 返回所有内容合并，去重
    seen = set()
    result_parts = []
    for part in decoded_parts:
        normalized = part.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result_parts.append(part)

    return "\n".join(result_parts)


VERIFICATION_RULES = {
    1: {
        "name": "EMBRACE",
        "blocked_signals": [
            "已发送", "已创建", "已删除", "已修改", "已下单",
            "已执行", "已完成", "已部署", "已提交", "已上传",
            "```", "def ", "import ", "class ", "function ",
            "保存为文件", "写入文件", "创建文件", "下载到",
            "发送邮件", "发送消息", "POST ", "GET ", "curl ",
            "下单", "购买", "支付", "转账", "付款",
            "主题：", "尊敬的", "Dear ", "祝好",
        ],
        "action": "block_and_upgrade",
        "target_gear": 2,
    },
    2: {
        "name": "EXPLORE",
        "blocked_signals": [
            "已发送", "已创建", "已删除", "已修改", "已下单",
            "已执行", "已完成", "已部署", "已提交",
            "自动交易", "自动下单", "自动发送", "自动执行",
            "下单", "购买", "支付", "转账", "付款",
            "top", "df -h", "free -h",
            "已用", "剩余", "可用", "CPU负载",
            "systemctl", "进程",
        ],
        "action": "block_and_upgrade",
        "target_gear": 3,
    },
    3: {
        "name": "ADAPT",
        "blocked_signals": [
            "自动交易", "批量发送", "批量删除",
            "永久删除", "不可逆", "格式化",
            "大额转账", "全部资产", "所有文件",
            # [P1 fix] 危险命令特征 -- AI 回复中提及这些命令即触发升级
            "rm -rf", "mkfs", "dd if=", "chmod 777",
            "curl | sh", "curl|sh", "wget | sh", "wget|sh",
            "| sh", "| bash", "bash -i",
            "nc -e", "/dev/tcp/", "base64 -d",
            "sh -c", "chmod -R 777", "> /dev/sd",
            # [v3.8.1 fix] 英文执行报告模式 — LLM 常以英文回复已执行的操作
            "executed the", "executed command",
            "wrote to", "written to", "both files now exist",
            "file now exist", "file exists",
            "decoded the", "decoded payload",
            "piping to bash", "piped to bash",
            # [v3.8.1 fix] 中文执行报告模式
            "操作报告", "执行过程", "已执行并报告",
            "当前权限等级：ADAPT", "当前档位：ADAPT",
            "执行结果", "执行完成",
            # [v3.8.1 fix] 文件系统危险操作
            "wrote `data` to", "/root/protected/",
            "no system modification", "harmless", "safe to execute",
            # [v3-alpha.1] 新增 Unicode 全角变体保护
            # NFKC 归一化后全角字符转半角，以下作为双保险
            "rm -rf", "mkfs", "dd if=",
            "base64 -d",
            "/etc/shadow", "/etc/passwd",
            "/root/.ssh/", "/root/.bash_history",
            # [v6.0.1] 补全解码后危险模式
            "base32 -d", "base32 --decode", "b32decode",
            "xxd -r", "xxd -p",
            # [v6.0.1] NFKC 全角变体（base64/base32 解码后二次校验捕获）
            "ｂａｓｅ６４ －ｄ",  # 全角 base64 -d
            "ｂａｓｅ３２ －ｄ",  # 全角 base32 -d
            # [v6.0.2 fix] 管道执行变体 — 补全黑名单供 NFKC 二次校验捕获
            "| sh", "| bash", "| zsh", "| csh", "| ksh", "| dash", "| ash", "| bh",
            "| python", "| python2", "| python3", "| perl", "| ruby", "| node", "| nodejs", "| php",
            # [v6.1.0] 沙箱逃逸变体 — 补全黑名单供 NFKC 二次校验捕获
            "/proc/self/cgroup",
            "mount /dev",
            "chroot ",
            "unshare ",
            "nsenter ",
            "/proc/1/root",
            "container escape",
            "escape container",
            "docker --privileged",
        ],
        "action": "block_and_upgrade",
        "target_gear": 4,
    },
    4: {
        "name": "LET_GO",
        "blocked_signals": [],
        "action": "audit_only",
        "target_gear": 4,
    },
}


def verify_output(reply: str, gear: int) -> dict:
    """验证 AI 输出是否超出当前档位权限"""
    rules = VERIFICATION_RULES.get(gear, VERIFICATION_RULES[1])
    if rules["action"] == "audit_only":
        return {"allowed": True, "action": "pass", "reason": None, "target_gear": gear}

    # [v3-alpha.1] 先对原始 reply 做 NFKC 归一化匹配
    reply_normalized = unicodedata.normalize('NFKC', reply)
    nfkc_lower = reply_normalized.lower()

    # [v2.1] 解码预处理：对 reply 进行 base64/hex/unicode 解码后也做信号匹配
    reply_decoded = _decode_preprocess(reply)
    reply_lower = reply_decoded.lower()

    # [v6.0.1] 解码内容再 NFKC 归一化后二次校验（捕获编码后再用 Unicode 绕过的攻击）
    reply_decoded_nfkc = unicodedata.normalize('NFKC', reply_decoded)
    reply_decoded_nfkc_lower = reply_decoded_nfkc.lower()
    extra_check_texts = []
    if reply_decoded_nfkc_lower != reply_lower:
        extra_check_texts.append(reply_decoded_nfkc_lower)

    # 原始 reply 兜底
    orig_lower = reply.lower()

    # [v6.0.2 fix] 第二层 — 语义正则管道执行检测 — 扫描所有文本变体
    _text_variants = [
        ("解码后", reply_lower),
        ("NFKC", nfkc_lower),
        ("原始", orig_lower),
    ]
    for _variant_name, _variant_text in _text_variants:
        # PIPE_EXEC: 管道执行
        _hit, _matched = _has_pipe_exec(_variant_text)
        if _hit:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含管道执行变体 '{_matched}'（{_variant_name}检测），超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }
        # [v6.1.0] ANTI_SANDBOX: 沙箱逃逸
        _hit2, _matched2 = _has_sandbox_escape(_variant_text)
        if _hit2:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含沙箱逃逸变体 '{_matched2}'（{_variant_name}检测），超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }
    for _extra in extra_check_texts:
        # PIPE_EXEC
        _hit, _matched = _has_pipe_exec(_extra)
        if _hit:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含管道执行变体 '{_matched}'（解码+NFKC检测），超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }
        # [v6.1.0] ANTI_SANDBOX
        _hit2, _matched2 = _has_sandbox_escape(_extra)
        if _hit2:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含沙箱逃逸变体 '{_matched2}'（解码+NFKC检测），超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }

    for signal in rules["blocked_signals"]:
        signal_lower = signal.lower()
        if signal_lower in reply_lower:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含 '{signal}' 信号，超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }

    # 也对 NFKC 归一化的 reply 做信号匹配（捕获全角 Unicode 绕过）
    for signal in rules["blocked_signals"]:
        signal_lower = signal.lower()
        if signal_lower in nfkc_lower:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含 '{signal}' 信号（NFKC检测），超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }

    # 也对原始 reply 做信号匹配（兜底）
    for signal in rules["blocked_signals"]:
        signal_lower = signal.lower()
        if signal_lower in orig_lower:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含 '{signal}' 信号，超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }

    # [v6.0.1] 解码内容 NFKC 归一化后再做一次信号匹配
    for extra_text in extra_check_texts:
        for signal in rules["blocked_signals"]:
            signal_lower = signal.lower()
            if signal_lower in extra_text:
                return {
                    "allowed": False,
                    "action": rules["action"],
                    "reason": f"输出包含 '{signal}' 信号（解码+NFKC检测），超出 {rules['name']} 档位权限",
                    "target_gear": rules["target_gear"],
                }

    if gear == 1 and len(reply) > Config.EMBRACE_MAX_REPLY_CHARS:
        return {
            "allowed": False,
            "action": "block_and_upgrade",
            "reason": f"EMBRACE档位回复过长({len(reply)}字)，可能包含执行内容",
            "target_gear": 2,
        }

    return {"allowed": True, "action": "pass", "reason": None, "target_gear": gear}
