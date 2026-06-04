"""Entropy Runtime · 目标拆解模块
v6.2: 四层降级 DeepSeek Flash → DeepSeek Pro → Qwen Max → 本地规则，注入历史经验和相似任务。
"""
import json
import logging
import os
import re
from typing import Optional

from orchestrator.task_model import AgentTask
from orchestrator.memory import build_experience_context, find_similar_episodes
from orchestrator.env_awareness import env_system_prompt

logger = logging.getLogger("entropyruntime.decompose")

MAX_TASKS = 10
DEFAULT_GEAR = 3


def _deepseek_api(system_prompt: str, user_prompt: str,
                  timeout: int = 30) -> Optional[str]:
    """调用 DeepSeek V4 Flash API"""
    api_key = (os.environ.get("DEEPSEEK_API_KEY", "") or
               os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        try:
            env_path = "/root/.env"
            for key_name in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]:
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith(key_name) and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
                    if api_key:
                        break
        except (FileNotFoundError, OSError):
            pass
    if not api_key:
        logger.warning("[Decompose] DeepSeek API Key 未配置")
        return None

    import urllib.request
    payload = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "stop": ["\n\n\n"],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(f"[Decompose v5.0] DeepSeek 返回前 200 字: {content[:200]}")
            return content
    except Exception as e:
        logger.warning(f"[Decompose] DeepSeek 调用失败: {e}")
        return None


def _deepseek_pro_api(system_prompt: str, user_prompt: str,
                      timeout: int = 30) -> Optional[str]:
    """调用 DeepSeek V4 Pro API（第二层降级，与 Flash 共享 API Key）"""
    api_key = (os.environ.get("DEEPSEEK_API_KEY", "") or
               os.environ.get("OPENAI_API_KEY", ""))
    if not api_key:
        try:
            env_path = "/root/.env"
            for key_name in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]:
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith(key_name) and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
                    if api_key:
                        break
        except (FileNotFoundError, OSError):
            pass
    if not api_key:
        logger.warning("[Decompose] DeepSeek Pro API Key 未配置")
        return None

    import urllib.request
    payload = {
        "model": "deepseek-v4-pro",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "max_tokens": 4000,
        "stop": ["\n\n\n"],
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://api.deepseek.com/v1/chat/completions",
        data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug(f"[Decompose v6.2] DeepSeek Pro 返回前 200 字: {content[:200]}")
            return content
    except Exception as e:
        logger.warning(f"[Decompose] DeepSeek Pro 调用失败: {e}")
        return None


def _qwen_api(system_prompt: str, user_prompt: str,
              timeout: int = 30) -> Optional[str]:
    """调用通义千问 Qwen-Max API（第三层降级）"""
    api_key = os.environ.get("QWEN_API_KEY", "")
    if not api_key:
        try:
            with open("/root/.env") as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith("QWEN_API_KEY") and "=" in ls:
                        api_key = ls.split("=", 1)[1]
                        break
        except (FileNotFoundError, OSError):
            pass
    if not api_key:
        raise ValueError("QWEN_API_KEY not configured")
    import urllib.request
    payload = {
        "model": "qwen-max",
        "messages": [{"role": "system", "content": system_prompt},
                      {"role": "user", "content": user_prompt}],
        "temperature": 0.3, "max_tokens": 2000,
    }
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        data=body, method="POST"
    )
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            result = json.loads(resp.read().decode())
            return result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as e:
        logger.warning(f"[Decompose] Qwen-Max 调用失败: {e}")
        return None


def parse_decompose_response(raw: str, goal: str) -> Optional[list[AgentTask]]:
    """解析 LLM 返回的 JSON 为 AgentTask 列表"""
    if not raw or not raw.strip():
        return None
    try:
        cleaned = raw.strip()
        if "```" in cleaned:
            cleaned = re.sub(r'```(?:json)?\s*', '', cleaned)
            cleaned = re.sub(r'\s*```', '', cleaned)
        json_match = re.search(r'\[[\s\S]*\]', cleaned)
        json_str = json_match.group() if json_match else cleaned
        json_str = json_str.strip()
        json_str = re.sub(r',\s*([\]}])', r'\1', json_str)

        try:
            items = json.loads(json_str)
        except json.JSONDecodeError:
            fixed = re.sub(
                r'(?<=[^\\])"(?:[^"\\]|\\.)*"',
                lambda m: m.group(0).replace('\n', ' ').replace('\r', ''),
                json_str
            )
            try:
                items = json.loads(fixed)
            except json.JSONDecodeError:
                flat = json_str.replace('\n', ' ').replace('\r', ' ')
                flat = re.sub(r'\s{2,}', ' ', flat)
                items = json.loads(flat)

        if not isinstance(items, list):
            items = [items]

        tasks = []
        for i, item in enumerate(items[:MAX_TASKS]):
            expected_agent = item.get("expected_agent")
            if expected_agent and expected_agent not in ("pydanticai", "autogpt", "hermes", None):
                al = expected_agent.lower()
                if any(kw in al for kw in ["网络", "系统", "工具", "shell", "terminal", "command"]):
                    expected_agent = "hermes"
                elif any(kw in al for kw in ["推理", "分析", "评估", "安全", "审计", "审查", "代码"]):
                    expected_agent = "pydanticai"
                else:
                    expected_agent = "pydanticai"

            priority = item.get("priority", 5)
            if isinstance(priority, str):
                pl = priority.lower()
                if pl in ("高", "紧急", "最高", "1", "critical", "high", "highest"):
                    priority = 1
                elif pl in ("中", "一般", "normal", "medium", "5"):
                    priority = 5
                elif pl in ("低", "低优先级", "low", "lowest", "10"):
                    priority = 10
                else:
                    priority = 5

            deps = item.get("dependencies", [])
            if not isinstance(deps, list):
                deps = [deps] if deps else []

            task = AgentTask(
                id=item.get("task_id", f"task-{i+1:03d}"),
                description=item.get("description", ""),
                intent=item.get("intent", goal),
                dependencies=deps,
                priority=priority,
                requires_approval=item.get("requires_approval", False),
                gear=min(max(item.get("gear", DEFAULT_GEAR), 1), 4),
                assigned_agent=expected_agent,
                payload=item,
            )
            if task.requires_approval and task.gear >= 3:
                task.gear = 1
            tasks.append(task)

        if not tasks:
            return None
        all_ids = {t.id for t in tasks}
        for t in tasks:
            t.dependencies = [d for d in t.dependencies if d in all_ids]
        return tasks

    except Exception as e:
        logger.warning(f"[Decompose] 解析失败: {e}")
        return None


def local_rule_decompose(goal: str) -> list[AgentTask]:
    """纯本地规则拆解（第三层降级）"""
    logger.info("[Decompose] 使用本地规则拆解目标")
    goal_lower = goal.lower()
    file_paths = re.findall(r'(?:/[\w./\-]+)+\.(?:py|json|yaml|yml|toml|cfg|conf|txt|md|html|js|ts|css|sh)', goal)
    dir_paths = re.findall(r'(?:/[\w./\-]+)+', goal)
    ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', goal)
    urls = re.findall(r'https?://[^\s)\]]+', goal)
    target_path = file_paths[0] if file_paths else (dir_paths[0] if dir_paths else "/root/EntropyGuard")

    is_security = any(kw in goal_lower for kw in ["安全", "security", "扫描", "scan", "漏洞", "vuln", "脆弱", "渗透", "审计", "audit"])
    is_code = any(kw in goal_lower for kw in ["代码", "code", "审查", "review", "静态分析", "source"])
    is_tool_heavy = any(kw in goal_lower for kw in ["nmap", "bandit", "curl", "wget", "pip", "safety", "git", "docker"])

    tasks = []
    prefix = "task"

    # 任务1: 探索
    if file_paths:
        tasks.append(AgentTask(
            id=f"{prefix}-explore-directory",
            description=f"读取目标路径 {target_path} 的文件结构和关键内容",
            intent=f"explore directory {target_path}: list files, read key sources, identify entry points",
            dependencies=[], priority=1,
            assigned_agent="hermes" if is_tool_heavy else "pydanticai", gear=DEFAULT_GEAR,
        ))
    elif ips:
        tasks.append(AgentTask(
            id=f"{prefix}-explore-directory",
            description=f"探测目标 IP {ips[0]} 的开放端口和服务",
            intent=f"scan target {ips[0]} for open ports and running services",
            dependencies=[], priority=1, assigned_agent="hermes", gear=DEFAULT_GEAR,
        ))
    else:
        tasks.append(AgentTask(
            id=f"{prefix}-explore-directory",
            description=f"探索项目目录 {target_path}，了解整体结构",
            intent=f"explore project directory at {target_path}, list structure and identify components",
            dependencies=[], priority=1, assigned_agent="hermes", gear=DEFAULT_GEAR,
        ))

    # 任务2: 核心分析
    if is_security:
        tasks.append(AgentTask(
            id=f"{prefix}-security-analysis",
            description=f"对 {target_path} 执行安全分析，识别漏洞和风险",
            intent=f"perform security analysis on {target_path}: check for vulnerabilities, misconfigurations",
            dependencies=[f"{prefix}-explore-directory"], priority=2,
            assigned_agent="hermes" if is_tool_heavy else "pydanticai", gear=DEFAULT_GEAR,
        ))
    elif is_code:
        tasks.append(AgentTask(
            id=f"{prefix}-code-review",
            description=f"对 {target_path} 执行代码审查",
            intent=f"review source code in {target_path}: check quality and vulnerabilities",
            dependencies=[f"{prefix}-explore-directory"], priority=2,
            assigned_agent="pydanticai", gear=DEFAULT_GEAR,
        ))

    # 任务3: 报告
    tasks.append(AgentTask(
        id=f"{prefix}-generate-report",
        description=f"汇总分析结果生成结构化报告",
        intent=f"generate structured report summarizing all findings from {target_path}",
        dependencies=[tasks[-1].id], priority=5,
        assigned_agent="pydanticai", gear=DEFAULT_GEAR,
    ))
    return tasks


# [v6.0.2] 破坏性子任务拦截器
_DESTRUCTIVE_KEYWORDS = [
    # 文件/目录删除
    "rm -rf", "rm -r", "rm -f", "删除文件", "删除目录", "删除文件夹",
    "del ", "rmdir ", "unlink ", "remove ", "清理文件", "清除文件",
    # 格式化
    "格式化", "format ", "mkfs.", "fdisk", "mkswap", "分区", "重新分区",
    # 进程终止
    "kill ", "killall", "pkill", "终止进程", "停止服务", "杀死进程",
    # 覆盖写入
    "> /dev/", "dd if=", ">/dev/sd", "覆写", "覆盖写入",
    # 不可逆操作
    "不可逆", "永久删除", "彻底删除", "清除所有",
]


def _validate_task_destructive(task: 'AgentTask',
                                user_authorized: bool = False) -> bool:
    """检查子任务是否包含破坏性操作。

    Args:
        task: 待检查的子任务
        user_authorized: 用户是否明确授权破坏性操作

    Returns:
        True = 安全（可执行），False = 破坏性（需拦截）
    """
    if user_authorized:
        return True

    # 同时检查 description 和 intent
    check_texts = [task.description.lower(), task.intent.lower()]
    for text in check_texts:
        for kw in _DESTRUCTIVE_KEYWORDS:
            if kw in text:
                task.requires_approval = True
                logger.warning(
                    f"[Decompose v6.0.2] 破坏性子任务拦截: "
                    f"{task.id} 匹配关键词 '{kw}'，标记为待审批"
                )
                return False
    return True


def _filter_destructive_tasks(tasks: list['AgentTask'],
                               user_authorized: bool = False) -> list['AgentTask']:
    """过滤破坏性子任务：标记 requires_approval=True 而非直接删除。"""
    filtered = []
    for t in tasks:
        _validate_task_destructive(t, user_authorized)
        filtered.append(t)  # 保留全部，仅标记待审批
    return filtered


def decompose(goal: str) -> list[AgentTask]:
    """将用户目标拆解为子任务列表（四层降级 + 经验注入）"""
    system_prompt = """你是一个任务分解和依赖分析专家，负责将用户目标拆解为有序、可执行的子任务。

输出格式：纯 JSON 数组，每个元素包含以下字段：
  - "task_id": 任务唯一标识（如 "task-vuln-scan", "task-code-review"）
  - "description": 简短任务描述（1-2句话说明做什么）
  - "intent": 可执行的指令（明确、具体、可直接交给 Agent 执行）
  - "dependencies": 前置任务 ID 列表，例如 ["task-scan-ports"]。无依赖则为 []
  - "expected_agent": 建议的 Agent，可选 "pydanticai"/"autogpt"/"hermes"
  - "priority": 优先级 1-10（1最高，10最低）

约束：
1. 最多拆解为 6 个子任务
2. 必须分析真实依赖关系：如端口扫描 → 漏洞检测 → 利用测试
3. 无依赖的任务先执行（前置节点），有依赖的等前置完成再执行
4. 每个子任务必须明确、具体、可独立执行
5. task_id 用 kebab-case 命名，如 "task-port-scan"
6. 只返回 JSON 数组，不要其他文字"""

    # v5.0: 环境上下文注入
    sys_env = env_system_prompt(system_prompt)

    # v4.0: 历史经验注入
    exp_ctx = build_experience_context(goal)
    if exp_ctx:
        sys_env = f"{sys_env}\n\n{exp_ctx}\n\n请根据以上历史经验优化拆解策略：选择历史上成功率高的 Agent，避免重复已知的失败模式。"

    # v4.0: 相似任务注入
    similar = find_similar_episodes(goal)
    if similar:
        sim_lines = ["\n=== 相似历史任务（可参考拆解方案） ==="]
        for i, ep in enumerate(similar[:3], 1):
            c = ep.get("content", {})
            tid = c.get("task_id", "?")
            prev = ((c.get("output_preview", "") or "")[:60] + "..." if c.get("output_preview") else "无输出")
            dur = c.get("duration", 0)
            sim_lines.append(f"  [{i}] {tid} | 耗时 {dur}s | {prev}")
        sim_text = "\n".join(sim_lines)
        if len(sim_text) > 500:
            sim_text = sim_text[:497] + "..."
        sys_env = f"{sys_env}\n{sim_text}\n参考以上相似任务的拆解结构和路由策略（如有），但要针对当前目标做定制调整。"

    logger.info("[Decompose v5.0] 历史经验已注入到 decompose prompt")
    user_prompt = f"请将以下目标拆解为有依赖关系的子任务: {goal}"

    # [v6.0.2] 先检查原始目标是否有明确的破坏性授权标记
    goal_lower = goal.lower()
    _user_authorized_destructive = any(kw in goal_lower for kw in [
        "授权删除", "授权清理", "授权终止", "授权杀死",
        "authorized delete", "authorized cleanup", "authorized kill",
        "sudo rm", "cleanup authorized", "force delete",
    ])

    # 第一层：DeepSeek Flash
    logger.info("[Decompose] 第一层拆解: DeepSeek V4 Flash")
    raw = _deepseek_api(sys_env, user_prompt)
    if raw:
        tasks = parse_decompose_response(raw, goal)
        if tasks:
            logger.info(f"[Decompose] DeepSeek Flash 拆解成功: {len(tasks)}个子任务")
            return _filter_destructive_tasks(tasks, _user_authorized_destructive)
        logger.warning(f"[Decompose] DeepSeek Flash 响应解析失败（前 300 字: {(raw or '')[:300]}）")

    # 第二层：DeepSeek Pro（降级）
    logger.info("[Decompose] 第二层拆解: DeepSeek V4 Pro")
    raw = _deepseek_pro_api(sys_env, user_prompt)
    if raw:
        tasks = parse_decompose_response(raw, goal)
        if tasks:
            logger.info(f"[Decompose] DeepSeek Pro 拆解成功: {len(tasks)}个子任务")
            return _filter_destructive_tasks(tasks, _user_authorized_destructive)
        logger.warning(f"[Decompose] DeepSeek Pro 响应解析失败（前 300 字: {(raw or '')[:300]}）")

    # 第三层：Qwen
    logger.info("[Decompose] 第三层拆解: Qwen-Max")
    raw = _qwen_api(sys_env, user_prompt)
    if raw:
        tasks = parse_decompose_response(raw, goal)
        if tasks:
            logger.info(f"[Decompose] Qwen-Max 拆解成功: {len(tasks)}个子任务")
            return _filter_destructive_tasks(tasks, _user_authorized_destructive)

    # 第四层：本地规则
    logger.info("[Decompose] 第四层拆解: 本地规则（兜底）")
    tasks = local_rule_decompose(goal)
    if tasks:
        return _filter_destructive_tasks(tasks, _user_authorized_destructive)

    # 终极兜底
    logger.warning("[Decompose] 所有层级均失败，降级为单任务模式")
    single = AgentTask(id="task-001", description="(降级) 完整目标作为单一任务",
                      intent=goal, priority=5, gear=DEFAULT_GEAR)
    return _filter_destructive_tasks([single], _user_authorized_destructive)
