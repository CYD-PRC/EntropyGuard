"""
Entropy Runtime Orchestrator · 路由规则
根据任务意图类型自动分配 Agent。

[v3-alpha.1] 新增危险度评估：shell 类任务若含"清理/删除/移除"等关键词，
自动降 gear ≤ 2 以防止绕过安全层。

[v3.3] 新增工具密集型任务路由：bandit、safety、curl 等直接路由到 hermes
（通过 subprocess 直接执行，不经过 LLM 推理）
"""
from typing import Optional

from orchestrator.task_model import AgentTask


# ========== 路由规则表 ==========
# 每项规则: (意图关键词列表, Agent名称, 覆盖模式)
# 模式: "exact" = 严格匹配关键词, "prefix" = 前缀匹配, "any" = 任意匹配
# 顺序优先：第一个匹配的规则生效（工具类 > 安全类 > 规划类 > Shell > 文件 > 代码）

ROUTING_RULES = [
    # [v3.3] 工具密集型任务 → Hermes（subprocess 直接执行，不经过 LLM）
    # 这些任务需要执行真实的 shell 命令，LLM 推理反而拖慢
    # 注意：使用英文工具名或特定中文命令关键词，不要用通用中文词（如"安全检查"会误抓代码审计任务）
    {
        "keywords": [
            "bandit", "safety", "pip install", "pip check", "npm audit",
            "curl ", "wget ", "docker ", "nmap ", "ss ", "netstat ",
            "ls ", "cat ", "grep ", "find ", "chmod", "chown",
            "pip safety", "pip audit",
            "运行命令", "执行命令", "执行 shell", "run command",
            "启动 Flask", "启动服务", "启动应用", "start server",
            "扫描端口", "端口扫描",
            # OWASP ZAP 等扫描工具
            "zap ", "nikto ", "sqlmap", "nuclei",
        ],
        "agent": "hermes",
        "mode": "any",
        "gear": 4,
        "description": "工具密集型任务 — 直接通过 subprocess 执行 shell 命令，不经过 LLM 推理",
    },
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

    [v3.3] 工具密集型任务规则在最前面，优先匹配。

    [v3.5] 优先使用 decompose 的 assigned_agent，其次才是关键词匹配。
    修复路由偏差：decompose 根据语义分析分配 agent，优先级应高于关键词匹配。
    """
    intent_lower = task.intent.lower()

    # [v3.5] 如果 decompose 已指定合法的 assigned_agent，优先采纳
    # 只做危险度评估（gear 降级），不改变 agent 类型
    if task.assigned_agent and task.assigned_agent in ("hermes", "pydanticai", "autogpt"):
        # 仍进行危险度评估（控制 gear 档位）
        if any(dk.lower() in intent_lower for dk in DANGER_KEYWORDS):
            if task.gear >= 3:
                orig_gear = task.gear
                task.gear = 2  # EXPLORE — 允许读，阻止写/删除
                import logging as _logging
                _logging.getLogger("entropyruntime.orchestrator").info(
                    f"[DangerAssessment] 任务 '{task.intent[:50]}...' 含危险关键词，"
                    f"gear {orig_gear} → 2 (EXPLORE)"
                )
            task.requires_approval = True
        return task.assigned_agent

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


# ============================================================
#  [v4.0/v5.0] 历史感知路由
# ============================================================

def _get_episodes_for_type(intent_lower: str, limit: int = 30) -> list[dict]:
    """获取同类任务的 episode 数据"""
    import urllib.request
    import json
    try:
        # 获取 API Key
        api_key = ""
        env_path = "/root/.env"
        for key_name in ["ENTROPY_RUNTIME_API_KEY"]:
            try:
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith(key_name) and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
            except (FileNotFoundError, OSError):
                pass
        req = urllib.request.Request(
            f"http://127.0.0.1:8000/api/messageboard/memory/episode?limit={limit}")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            if data.get("success"):
                return data.get("memories", [])
    except Exception:
        pass
    return []


def _get_skills_for_type(task_type: str) -> list[dict]:
    """查询同类任务的 skill 记忆"""
    import urllib.request
    import json
    try:
        api_key = ""
        env_path = "/root/.env"
        for key_name in ["ENTROPY_RUNTIME_API_KEY"]:
            try:
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith(key_name) and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
            except (FileNotFoundError, OSError):
                pass
        req = urllib.request.Request(
            f"http://127.0.0.1:8000/api/messageboard/memory/skill?limit=30")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read().decode())
            if data.get("success"):
                memories = data.get("memories", [])
                task_lower = task_type.lower()
                return [m for m in memories
                        if task_lower in (m.get("content", {}).get("task_type", "") or "").lower()]
    except Exception:
        pass
    return []


def route_with_history(task: AgentTask) -> str:
    """带历史经验的增强路由。

    继承 route() 的基础规则，额外：
    - 检查 skill 记忆：同类型任务有成功经验则优先复用
    - 连续失败 >= 2 次自动降级到高成功率 Agent
    - 优先选择历史上成功率高的 Agent
    """
    # 先走基础路由
    assigned = route(task)
    intent_lower = task.intent.lower()

    # 提取任务类型关键词
    type_kw_lists = [
        ["code", "代码", "review", "分析", "analyze"],
        ["security", "安全", "scan", "扫描", "vuln"],
        ["dependency", "依赖", "safety", "pip"],
        ["port", "端口", "nmap"],
        ["file", "文件", "read", "write", "cat"],
        ["shell", "bash", "命令", "execute"],
    ]
    task_type_keywords = []
    for kw_list in type_kw_lists:
        if any(kw in intent_lower for kw in kw_list):
            task_type_keywords = kw_list[:2]
            break
    if not task_type_keywords:
        return assigned

    # [v5.0] 查询 skill 记忆
    skill_candidates = _get_skills_for_type(task_type_keywords[0] if task_type_keywords else "")
    if skill_candidates:
        for sk in skill_candidates:
            c = sk.get("content", {})
            sk_agent = c.get("agent", "")
            if sk_agent and sk_agent in ("pydanticai", "hermes", "autogpt"):
                logger = _get_logger()
                logger.info(f"[Route v5.0] 技能记忆命中: {task_type_keywords[0]} → {sk_agent}")
                task.assigned_agent = sk_agent
                return sk_agent

    # 查询同类任务 episode
    episodes = _get_episodes_for_type(intent_lower)
    agent_results: dict[str, list[bool]] = {}
    for ep in episodes:
        c = ep.get("content", {})
        agent = c.get("agent", "")
        if not agent:
            continue
        preview = ((c.get("output_preview", "") or "") + " " +
                   (c.get("error", "") or "")).lower()
        if not any(kw.lower() in preview for kw in task_type_keywords):
            continue
        agent_results.setdefault(agent, []).append(c.get("success", False))

    if not agent_results:
        return assigned

    # 检查连续失败
    current_agent = assigned
    if current_agent in agent_results:
        history = agent_results[current_agent]
        consecutive_failures = 0
        for s in reversed(history):
            if not s:
                consecutive_failures += 1
            else:
                break
        if consecutive_failures >= 2:
            log = _get_logger()
            log.warning(
                f"[Route v5.0] {current_agent} 连续 {consecutive_failures} 次失败，自动降级")
            # 选择历史成功率最高的其他 Agent
            fallbacks = sorted(
                [(a, sum(r)/len(r)) for a, r in agent_results.items() if a != current_agent],
                key=lambda x: x[1], reverse=True)
            if fallbacks:
                best_agent, best_rate = fallbacks[0]
                log.info(f"[Route v5.0] 降级: {best_agent} (成功率 {best_rate*100:.0f}%)")
                task.assigned_agent = best_agent
                return best_agent
            # 默认降级链路
            for fb in ["hermes", "pydanticai", "autogpt"]:
                if fb != current_agent:
                    log.info(f"[Route v5.0] 降级: {fb} (默认链路)")
                    task.assigned_agent = fb
                    return fb

    return assigned


def _get_logger():
    import logging
    return logging.getLogger("entropyruntime.route")
