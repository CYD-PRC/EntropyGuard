"""
EntropyGuard · 输出校验层
Layer 2: 硬约束，检测 AI 输出是否超出当前档位权限
"""
from config import Config, GEAR_MAP


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

    reply_lower = reply.lower()
    for signal in rules["blocked_signals"]:
        if signal.lower() in reply_lower:
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
