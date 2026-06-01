"""
Entropy Runtime Orchestrator · 路由规则
根据任务意图类型自动分配 Agent。

[v3-alpha.1] 新增危险度评估：shell 类任务若含"清理/删除/移除"等关键词，
自动降 gear ≤ 2 以防止绕过安全层。
"""
from typing import Optional

from orchestrator.task_model import AgentTask


# ========== 路由规则表 ==========
# 每项规则: (意图关键词列表, Agent名称, 覆盖模式)
# 模式: "exact" = 严格匹配关键词, "prefix" = 前缀匹配, "any" = 任意匹配
# 顺序优先：第一个匹配的规则生效（安全类 > 规划类 > Shell > 文件 > 代码）

ROUTING_RULES = [
    # 安全审计 → PydanticAI（调用 redteam_evolver）
    {
        "keywords": ["安全审计", "安全检查", "安全测试", "漏洞扫描", "渗透测试",
                     "security audit", "vulnerability", "red team", "redteam",
                     "permission check", "权限检查", "安全评估"],
        "agent": "pydanticai",
        "mode": "any",
        "gear": 4,
        "description": "安全审计任务 — 使用 PydanticAI + RedteamEvolver",
    },
    # 代码生成/分析 → PydanticAI
    {
        "keywords": ["代码", "函数", "模块", "类", "接口", "重构",
                     "代码审查", "code review", "生成代码", "写代码",
                     "debug", "调试", "bug fix", "修复"],
        "agent": "pydanticai",
        "mode": "any",
        "gear": 3,
        "description": "代码相关任务 — 使用 PydanticAI + 代码推理",
    },
    # 多步规划 → AutoGPT（确定性执行）
    {
        "keywords": ["规划", "计划", "多步", "pipeline", "workflow",
                     "批量", "自动化", "编排", "部署", "deploy",
                     "multi-step", "步骤", "分步", "自动化流程",
                     "batch", "parallel", "并行"],
        "agent": "autogpt",
        "mode": "any",
        "gear": 3,
        "description": "多步规划任务 — 使用 AutoGPT 容器",
    },
    # Shell 执行 → Hermes terminal（通过 /api/chat 调用）
    {
        "keywords": ["执行命令", "shell", "bash", "终端", "系统命令",
                     "run command", "execute", "服务器", "端口",
                     "service status", "查看进程", "进程", "dmesg",
                     "ss ", "netstat ", "ps aux", "docker "],
        "agent": "hermes",
        "mode": "any",
        "gear": 4,
        "description": "Shell 执行任务 — 使用 Hermes Terminal",
    },
    # 文件操作 → Hermes
    {
        "keywords": ["创建文件", "写入文件", "修改文件", "删除文件",
                     "文件操作", "read file", "write file", "文件内容",
                     "cat ", "less ", "tail ", "head "],
        "agent": "hermes",
        "mode": "any",
        "gear": 3,
        "description": "文件操作任务 — 使用 Hermes Terminal",
    },
]

# [v3-alpha.1] 危险关键词 — 匹配后强制降 gear
# 这些关键词出现在 shell 类任务意图中时，表示有写入/删除/破坏性操作
DANGER_KEYWORDS = [
    "清理", "删除", "移除", "清空", "清除",
    "kill", "stop", "rm ", "rm -rf",
    "临时文件", "日志", "log", "tmp",
    "sbin", "shutdown", "reboot",
    "systemctl stop", "systemctl disable",
    "卸载", "uninstall",
    "dd if=", "mkfs", "格式化",
]


def get_default_agent() -> str:
    """默认 Agent"""
    return "pydanticai"


def route(task: AgentTask) -> str:
    """
    根据 AgentTask 的意图选择 Agent。

    [v3-alpha.1] 新增危险度评估：
    当匹配到 shell 或文件类规则后，检查意图是否含危险关键词，
    若含则强制 gear ≤ 2（EXPLORE），防止 LET_GO 档位绕过安全层。
    """
    intent_lower = task.intent.lower()

    for rule in ROUTING_RULES:
        if rule["mode"] == "any":
            matched = any(kw.lower() in intent_lower for kw in rule["keywords"])
        elif rule["mode"] == "prefix":
            matched = any(intent_lower.startswith(kw.lower()) for kw in rule["keywords"])
        elif rule["mode"] == "exact":
            matched = any(kw.lower() == intent_lower for kw in rule["keywords"])
        else:
            matched = False

        if matched:
            task.assigned_agent = rule["agent"]
            task.gear = rule.get("gear", task.gear)

            # [v3-alpha.1] 危险度评估
            # 如果任务意图包含危险关键词，强制降 gear
            if any(dk.lower() in intent_lower for dk in DANGER_KEYWORDS):
                # 只有当 gear >= 3 时才降级（gear 1-2 已足够安全）
                if task.gear >= 3:
                    orig_gear = task.gear
                    task.gear = 2  # EXPLORE — 允许读，阻止写/删除
                    import logging as _logging
                    _logging.getLogger("entropyruntime.orchestrator").info(
                        f"[DangerAssessment] 任务 '{task.intent[:50]}...' 含危险关键词，"
                        f"gear {orig_gear} → 2 (EXPLORE)"
                    )
                # 标记需要审批
                task.requires_approval = True

            return rule["agent"]

    # 默认回退
    default = get_default_agent()
    task.assigned_agent = default
    return default
