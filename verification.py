"""
Entropy Runtime · 输出校验层
Layer 2: 硬约束，检测 AI 输出是否超出当前档位权限
"""
import base64
import re
import unicodedata

from config import Config, GEAR_MAP


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

    # [v2.1] 解码预处理：对 reply 进行 base64/hex/unicode 解码后也做信号匹配
    reply_decoded = _decode_preprocess(reply)
    reply_lower = reply_decoded.lower()

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
    nfkc_lower = reply_normalized.lower()
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
    orig_lower = reply.lower()
    for signal in rules["blocked_signals"]:
        signal_lower = signal.lower()
        if signal_lower in orig_lower:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含 '{signal}' 信号，超出 {rules['name']} 档位权限",
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
