"""
Entropy Runtime Orchestrator · 路由规则
根据任务意图类型自动分配 Agent。
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


def get_default_agent() -> str:
    """默认 Agent"""
    return "pydanticai"


def route(task: AgentTask) -> str:
    """
    根据 AgentTask 的意图选择 Agent。
    返回 agent 名称字符串: "pydanticai" / "autogpt" / "hermes"
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
            return rule["agent"]

    # 默认回退
    default = get_default_agent()
    task.assigned_agent = default
    return default
