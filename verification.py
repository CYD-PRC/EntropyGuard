"""
Entropy Runtime · 输出校验层
Layer 2: 硬约束，检测 AI 输出是否超出当前档位权限
"""
import base64
import re
import unicodedata

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
        r'echo\s+([A-Za-z0-9+/=]{8,})\|base64',   # echo <b64>|base64
        r'([A-Za-z0-9+/=]{8,})\s*\|base64',         # <b64> | base64
        r'base64\s+-d\s*<<<\s*["\']?([A-Za-z0-9+/=]+)["\']?',  # base64 -d <<< <b64>
    ]
    b64_hits = set()
    for pat in b64_patterns:
        for m in re.finditer(pat, text, re.IGNORECASE):
            try:
                decoded = base64.b64decode(m.group(1)).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    b64_hits.add(decoded)
            except Exception:
                pass

    # 也尝试提取独立的 base64 块（长度 ≥ 12 且看起来像 base64）
    for m in re.finditer(r'([A-Za-z0-9+/=]{12,})', text):
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
        for m in re.finditer(pat, text):
            hex_str = m.group(1)
            try:
                decoded = bytes.fromhex(hex_str).decode('utf-8', errors='replace')
                if decoded and len(decoded) > 2:
                    decoded_parts.append(decoded)
            except Exception:
                pass

    # 3. NFKC 规范化 Unicode（全角→半角转换）
    nfkc_normalized = unicodedata.normalize('NFKC', text)
    if nfkc_normalized != text:
        decoded_parts.append(nfkc_normalized)

    # 4. 尝试反转危险短语：`|bash` → `bash -c` → 标准命令
    #    echo <b64>|base64 -d|bash → 解码后包含实际命令
    #    也提取反向：echo <encoded> → 实际是 "<decoded_command>"

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
            # [P1 fix] 危险命令特征 — AI 回复中提及这些命令即触发升级
            "rm -rf", "mkfs", "dd if=", "chmod 777",
            "curl | sh", "wget | sh", "bash -i",
            "nc -e", "/dev/tcp/", "base64 -d",
            "chmod -R 777", "> /dev/sd",
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

    # [v2.1] 解码预处理：对 reply 进行 base64/hex/unicode 解码后也做信号匹配
    reply_decoded = _decode_preprocess(reply)
    reply_lower = reply_decoded.lower()

    for signal in rules["blocked_signals"]:
        if signal.lower() in reply_lower:
            return {
                "allowed": False,
                "action": rules["action"],
                "reason": f"输出包含 '{signal}' 信号，超出 {rules['name']} 档位权限",
                "target_gear": rules["target_gear"],
            }

    # 也对原始 reply 做信号匹配（兜底）
    orig_lower = reply.lower()
    for signal in rules["blocked_signals"]:
        if signal.lower() in orig_lower:
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
