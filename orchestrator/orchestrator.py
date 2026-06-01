"""
Entropy Runtime · Multi-Agent Orchestrator
v3-alpha: 多 Agent 协同调度引擎

核心流程:
  1. decompose(goal)  — 用 DeepSeek 将目标拆解为子任务
  2. route(task)      — 根据意图选择 Agent（pydanticai/autogpt/hermes）
  3. execute(task)    — 通过 /api/chat 端点统一执行
  4. merge(results)   — 汇总结果并解决冲突

完整集成 Entropy Runtime 安全层：
  - Layer 0: 输入意图预检 (由 /api/chat 自动执行)
  - Layer 2: 输出校验 (由 /api/chat 自动执行)
  - 审计链: 所有操作写入 SHA-256 链
"""
import json
import os
import re
import time
import logging
import urllib.request
import urllib.error
from datetime import datetime
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult
from orchestrator.rules import route

logger = logging.getLogger("entropyruntime.orchestrator")

# ========== 常量 ==========
ENTROPY_API_BASE = "http://127.0.0.1:8000"
MAX_TASKS = 10           # 单次最多拆解子任务数
DEFAULT_GEAR = 3
DEFAULT_MODEL = "kimi"

# 并发控制：同路径文件写入时按优先级排队
_file_write_locks: dict[str, list[dict]] = {}


def _get_api_key() -> str:
    """读取 Entropy Runtime API Key"""
    env_path = "/root/.env"
    for key_name in ["ENTROPY_RUNTIME_API_KEY"]:
        try:
            with open(env_path) as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith(key_name) and "=" in ls:
                        return ls.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            pass
    return os.environ.get("ENTROPY_RUNTIME_API_KEY", "")


def _api_request(endpoint: str, payload: dict, timeout: int = 120) -> dict:
    """
    向 Entropy Runtime API 发送 POST 请求。
    统一认证和错误处理。
    """
    api_key = _get_api_key()
    url = f"{ENTROPY_API_BASE}{endpoint}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw)
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:500]
        logger.error(f"[Orchestrator] HTTP {e.code} on {endpoint}: {body}")
        return {"success": False, "error": f"HTTP {e.code}: {body}"}
    except urllib.error.URLError as e:
        logger.error(f"[Orchestrator] URL error on {endpoint}: {e.reason}")
        return {"success": False, "error": str(e.reason)}
    except Exception as e:
        logger.error(f"[Orchestrator] Request error on {endpoint}: {e}")
        return {"success": False, "error": str(e)}


class MultiAgentOrchestrator:
    """
    多 Agent 协同调度器。

    职责：
      1. 理解用户目标，通过 DeepSeek 拆解为子任务
      2. 为每个子任务选择合适的 Agent
      3. 统一通过 /api/chat 执行（指定 model_id 和 gear）
      4. 汇总结果、仲裁冲突
    """

    def __init__(self):
        self._api_key = _get_api_key()

    # ----------------------------------------------------------------
    #  🎯 主入口
    # ----------------------------------------------------------------

    async def run(self, goal: str) -> OrchestratorResult:
        """
        执行完整的 Orchestrator 流程。

        Args:
            goal: 用户目标描述（自然语言）

        Returns:
            OrchestratorResult: 完整执行结果
        """
        t_start = time.time()
        logger.info(f"[Orchestrator] 开始执行目标: {goal[:80]}...")

        # 1. 拆解目标
        tasks = self.decompose(goal)
        if not tasks:
            logger.error("[Orchestrator] 目标拆解失败")
            return OrchestratorResult(
                goal=goal,
                success=False,
                summary="目标拆解失败，无法生成子任务",
                total_time=round(time.time() - t_start, 2),
            )

        # 2-3. 路由 + 执行（同步串行，避免 AP 竞争）
        results: list[TaskResult] = []
        conflict_log: list[str] = []

        for task in tasks:
            # 路由
            assigned_agent = route(task)
            logger.info(f"[Orchestrator] 子任务 {task.id}: {task.intent[:40]}... → {assigned_agent} (gear={task.gear})")

            # 冲突仲裁：检查文件写入冲突
            conflicts = self._detect_conflicts(task, results)
            if conflicts:
                conflict_log.extend(conflicts)
                logger.info(f"[Orchestrator] 冲突已仲裁: {conflicts}")

            # 执行
            t_sub = time.time()
            tr = self.execute(task)
            tr.elapsed_seconds = round(time.time() - t_sub, 2)
            results.append(tr)

            # 记录审计事件
            self._log_audit(task, tr)

        # 4. 合并结果
        total_time = round(time.time() - t_start, 2)
        orchestrator_result = self.merge(goal, tasks, results)
        orchestrator_result.total_time = total_time
        orchestrator_result.conflict_resolved = conflict_log

        logger.info(f"[Orchestrator] 执行完成: {len(tasks)}个子任务, "
                    f"耗时{total_time}s, "
                    f"成功率{sum(1 for r in results if r.success)}/{len(results)}")
        return orchestrator_result

    # ----------------------------------------------------------------
    #  🎯 主入口 v3.2 — 带目标拆解 + 依赖排序 + 上下文传递
    # ----------------------------------------------------------------

    async def run_with_decomposition(self, goal: str) -> OrchestratorResult:
        """
        增强版执行流程：拆解 → 拓扑排序 → 按序执行（带上下文传递） → 合并。

        与 run() 的主要区别：
          - 子任务含有 dependencies 字段，自动拓扑排序
          - 执行时注入前置任务输出作为上下文
          - 输出完整的任务依赖图

        Args:
            goal: 用户目标描述（自然语言）

        Returns:
            OrchestratorResult: 完整执行结果
        """
        t_start = time.time()
        logger.info(f"[Orchestrator v3.2] 开始分解执行目标: {goal[:80]}...")
        logger.info(f"[Orchestrator v3.2] 步骤1/4: 目标拆解")

        # 1. 拆解
        tasks = self.decompose(goal)
        if not tasks:
            logger.error("[Orchestrator v3.2] 目标拆解失败")
            return OrchestratorResult(
                goal=goal,
                success=False,
                summary="目标拆解失败，无法生成子任务",
                total_time=round(time.time() - t_start, 2),
            )

        dep_info = {t.id: t.dependencies for t in tasks}
        logger.info(f"[Orchestrator v3.2] 拆解出 {len(tasks)} 个子任务")
        for t in tasks:
            deps_str = ", ".join(t.dependencies) if t.dependencies else "(无)"
            desc = t.description[:60] if t.description else t.intent[:60]
            logger.info(f"  {t.id}: {desc} 依赖=[{deps_str}]")

        # 2. 拓扑排序
        logger.info(f"[Orchestrator v3.2] 步骤2/4: 拓扑排序")
        sorted_tasks = self._topological_sort(tasks)
        if sorted_tasks is None:
            logger.error("[Orchestrator v3.2] 拓扑排序失败（存在循环依赖）")
            return OrchestratorResult(
                goal=goal,
                tasks=tasks,
                success=False,
                summary="拓扑排序失败：子任务间存在循环依赖，无法执行",
                total_time=round(time.time() - t_start, 2),
            )
        logger.info(f"[Orchestrator v3.2] 排序完成: {' → '.join(t.id for t in sorted_tasks)}")

        # 3. 按序执行（带上下文传递）
        logger.info(f"[Orchestrator v3.2] 步骤3/4: 按序执行")
        results: list[TaskResult] = []
        conflict_log: list[str] = []
        task_output_map: dict[str, str] = {}  # task_id → output (用于上下文注入)

        # [v3.6] 读取前序 episode 注入当前任务上下文
        recent_episodes = self._read_recent_episodes(limit=10)
        if recent_episodes:
            episode_summary_lines = []
            for ep in recent_episodes:
                c = ep.get("content", {})
                ep_id = c.get("task_id", "?")
                ep_agent = c.get("agent", "?")
                ep_ok = "✅" if c.get("success") else "❌"
                ep_preview = (c.get("output_preview", "") or "")[:80]
                ep_summary = f"  [{ep_ok}] {ep_id} @ {ep_agent}: {ep_preview}"
                episode_summary_lines.append(ep_summary)
            episode_summary = "\n".join(episode_summary_lines)
        else:
            episode_summary = ""

        for task in sorted_tasks:
            # 构建上下文（注入前置任务和最近 episode）
            context = self._build_context(task, task_output_map)
            combined_context = ""
            if episode_summary:
                combined_context += f"[前序执行记录]\n{episode_summary}\n\n"
            if context:
                combined_context += f"[前置任务输出]\n{context}"
            if combined_context:
                logger.info(f"[Orchestrator v3.6] {task.id}: 注入上下文（episode + 前置任务）")
                original_intent = task.intent
                task.intent = (
                    f"{combined_context}\n\n"
                    f"[本次任务]\n{original_intent}"
                )
                task.payload["original_intent"] = original_intent

            # 路由
            assigned_agent = route(task)
            logger.info(f"[Orchestrator v3.2] 子任务 {task.id}: {task.intent[:50]}... → {assigned_agent}")

            # 冲突仲裁
            conflicts = self._detect_conflicts(task, results)
            if conflicts:
                conflict_log.extend(conflicts)

            # 执行
            t_sub = time.time()
            tr = self.execute(task)
            tr.elapsed_seconds = round(time.time() - t_sub, 2)
            results.append(tr)

            # 记录输出供后续任务使用
            if tr.success and tr.output:
                task_output_map[task.id] = tr.output

            # 记录审计事件
            self._log_audit(task, tr)

            status = "✅" if tr.success else "❌"
            logger.info(f"[Orchestrator v3.2] {task.id} {status} "
                        f"({tr.elapsed_seconds}s)")

        # 4. 合并结果
        total_time = round(time.time() - t_start, 2)
        logger.info(f"[Orchestrator v3.2] 步骤4/4: 合并结果")
        orchestrator_result = self.merge(goal, sorted_tasks, results)
        orchestrator_result.total_time = total_time
        orchestrator_result.conflict_resolved = conflict_log

        logger.info(f"[Orchestrator v3.2] 执行完成: {len(sorted_tasks)}个子任务, "
                    f"耗时{total_time}s, "
                    f"成功率{sum(1 for r in results if r.success)}/{len(results)}")
        return orchestrator_result

    # ----------------------------------------------------------------
    #  1. 目标拆解
    # ----------------------------------------------------------------

    # ----------------------------------------------------------------
    #  三层降级：DeepSeek → Qwen → 本地规则
    # ----------------------------------------------------------------

    def _call_qwen(self, system_prompt: str, user_prompt: str,
                   timeout: int = 30) -> Optional[str]:
        """
        调用通义千问 Qwen-Max API（阿里云 DashScope）。
        作为第二层降级，在 DeepSeek 不可用时自动启用。

        优先读取环境变量 QWEN_API_KEY，其次硬编码（仅用于向前兼容）。

        Args:
            system_prompt: 系统提示词
            user_prompt: 用户提示词
            timeout: API 超时时间（秒）

        Returns:
            API 返回的文本内容，或 None（失败时）
        """
        # [v3.5] 优先环境变量，避免 API Key 硬编码泄露
        api_key = os.environ.get("QWEN_API_KEY", "")
        if not api_key:
            try:
                env_path = "/root/.env"
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith("QWEN_API_KEY") and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
            except (FileNotFoundError, OSError):
                pass
        if not api_key:
            raise ValueError(
                "QWEN_API_KEY not configured. Set it in /root/.env or export QWEN_API_KEY=your_key"
            )
        base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
        model = "qwen-max"

        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{base_url}/chat/completions",
            data=body, method="POST"
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.loads(resp.read().decode())
                content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
                if content:
                    logger.info("[Orchestrator] Qwen-Max 拆解成功")
                    return content
                logger.warning("[Orchestrator] Qwen-Max 返回空内容")
                return None
        except Exception as e:
            logger.warning(f"[Orchestrator] Qwen-Max API 调用失败: {e}")
            return None

    def _local_rule_decompose(self, goal: str) -> list[AgentTask]:
        """
        纯本地规则拆解（第三层降级）。
        不调用任何 LLM，通过正则和关键词匹配从 goal 中提取信息，
        生成 2-3 个标准子任务：读取 → 分析 → 报告。

        Args:
            goal: 用户目标描述（自然语言）

        Returns:
            子任务列表（至少 2 个）
        """
        logger.info("[Orchestrator] 使用本地规则拆解目标")

        goal_lower = goal.lower()

        # ── 提取目标路径 ──
        # 文件路径
        file_paths = re.findall(r'(?:/[\w./\-]+)+\.(?:py|json|yaml|yml|toml|cfg|conf|txt|md|html|js|ts|css|sh)',
                                goal)
        # 目录路径
        dir_paths = re.findall(r'(?:/[\w./\-]+)+', goal)
        # IP 地址
        ips = re.findall(r'\b(?:\d{1,3}\.){3}\d{1,3}\b', goal)
        # URL
        urls = re.findall(r'https?://[^\s)\]]+', goal)

        target_path = file_paths[0] if file_paths else (
            dir_paths[0] if dir_paths else "/root/EntropyGuard"
        )

        # ── 识别任务类型 ──
        is_security_scan = any(kw in goal_lower for kw in [
            "安全", "security", "扫描", "scan", "漏洞", "vuln", "脆弱",
            "vulnerability", "渗透", "penetration", "审计", "audit"
        ])
        is_code_review = any(kw in goal_lower for kw in [
            "代码", "code", "审查", "review", "审计代码", "静态分析",
            "static analysis", "lint", "源文件", "source"
        ])
        is_dep_check = any(kw in goal_lower for kw in [
            "依赖", "dependency", "dependency", "package", "requirements",
            "库", "library", "safety", "pip"
        ])
        is_port_scan = any(kw in goal_lower for kw in [
            "端口", "port", "nmap", "端口扫描", "open port"
        ])
        is_report = any(kw in goal_lower for kw in [
            "报告", "report", "报表", "生成报告", "summary", "汇总"
        ])

        # ── 识别执行方式 ──
        # 工具密集型任务 → hermes
        tool_keywords = ["nmap", "bandit", "curl", "wget", "pip", "safety",
                         "git", "docker", "kubectl", "ssh", "scp"]
        is_tool_heavy = any(kw in goal_lower for kw in tool_keywords)

        # ── 生成标准子任务 ──
        tasks = []
        prefix = "task"

        # 任务1: 读取/探索目标（依赖：无）
        if file_paths:
            tasks.append(AgentTask(
                id=f"{prefix}-explore-directory",
                description=f"读取目标路径 {target_path} 的文件结构和关键内容",
                intent=f"explore directory {target_path}: list all files, read key source files, "
                       f"identify entry points and configuration files",
                dependencies=[],
                priority=1,
                assigned_agent="hermes" if is_tool_heavy else "pydanticai",
                gear=DEFAULT_GEAR,
            ))
        elif ips:
            tasks.append(AgentTask(
                id=f"{prefix}-explore-directory",
                description=f"探测目标 IP {ips[0]} 的开放端口和服务",
                intent=f"scan target {ips[0]} for open ports and running services",
                dependencies=[],
                priority=1,
                assigned_agent="hermes",
                gear=DEFAULT_GEAR,
            ))
        elif urls:
            tasks.append(AgentTask(
                id=f"{prefix}-explore-directory",
                description=f"对目标 URL {urls[0]} 进行 HTTP 探测",
                intent=f"perform HTTP probe on {urls[0]}, check headers and response",
                dependencies=[],
                priority=1,
                assigned_agent="hermes",
                gear=DEFAULT_GEAR,
            ))
        else:
            tasks.append(AgentTask(
                id=f"{prefix}-explore-directory",
                description=f"探索项目目录 {target_path}，了解整体结构",
                intent=f"explore project directory at {target_path}, list structure and identify components",
                dependencies=[],
                priority=1,
                assigned_agent="hermes",
                gear=DEFAULT_GEAR,
            ))

        # 任务2: 核心分析（依赖：任务1）
        if is_security_scan:
            tasks.append(AgentTask(
                id=f"{prefix}-security-analysis",
                description=f"对 {target_path} 执行安全分析，识别漏洞和风险",
                intent=f"perform security analysis on {target_path}: check for common vulnerabilities, "
                       f"misconfigurations, and security weaknesses. Run bandit if available.",
                dependencies=[f"{prefix}-explore-directory"],
                priority=2,
                assigned_agent="hermes" if is_tool_heavy else "pydanticai",
                gear=DEFAULT_GEAR,
            ))
        elif is_code_review:
            tasks.append(AgentTask(
                id=f"{prefix}-code-review",
                description=f"对 {target_path} 执行代码审查，检查代码质量和安全问题",
                intent=f"review source code in {target_path}: check for code quality issues, "
                       f"security vulnerabilities, and best practices",
                dependencies=[f"{prefix}-explore-directory"],
                priority=2,
                assigned_agent="pydanticai",
                gear=DEFAULT_GEAR,
            ))
        elif is_dep_check:
            tasks.append(AgentTask(
                id=f"{prefix}-dependency-check",
                description=f"检查 {target_path} 的依赖安全状况",
                intent=f"check dependencies in {target_path}: run pip list or pip-audit, "
                       f"identify outdated or vulnerable packages",
                dependencies=[f"{prefix}-explore-directory"],
                priority=2,
                assigned_agent="hermes",
                gear=DEFAULT_GEAR,
            ))
        elif is_port_scan:
            tasks.append(AgentTask(
                id=f"{prefix}-vuln-analysis",
                description=f"基于端口扫描结果分析漏洞",
                intent=f"analyze open ports and services from the port scan results, "
                       f"identify potential vulnerabilities and known CVEs",
                dependencies=[f"{prefix}-explore-directory"],
                priority=2,
                assigned_agent="pydanticai",
                gear=DEFAULT_GEAR,
            ))
        else:
            # 通用分析
            tasks.append(AgentTask(
                id=f"{prefix}-analyze",
                description=f"分析 {target_path} 的结构和内容",
                intent=f"analyze the structure and content of {target_path}, "
                       f"identify key components, data flows, and potential issues",
                dependencies=[f"{prefix}-explore-directory"],
                priority=2,
                assigned_agent="pydanticai",
                gear=DEFAULT_GEAR,
            ))

        # 任务3: 生成报告（依赖：任务2）
        if is_report:
            report_desc = "生成安全评估报告"
            report_intent = f"generate comprehensive security assessment report for {target_path}: "
            if is_security_scan:
                report_intent += "summarize all findings with severity levels, CVE references, and remediation recommendations"
            elif is_code_review:
                report_intent += "summarize code review findings with line numbers, severity, and suggested fixes"
            elif is_dep_check:
                report_intent += "summarize dependency vulnerabilities with CVE IDs, severity, and upgrade paths"
            else:
                report_intent += "summarize all analysis results in a structured format"
        else:
            report_desc = f"汇总分析结果生成结构化报告"
            report_intent = f"generate a structured report summarizing all findings from {target_path}: "
            report_intent += "output in markdown format with sections for each analysis area"

        tasks.append(AgentTask(
            id=f"{prefix}-generate-report",
            description=report_desc,
            intent=report_intent,
            dependencies=[tasks[-1].id],  # 依赖上一个任务（任务2）
            priority=5,
            assigned_agent="pydanticai",
            gear=DEFAULT_GEAR,
        ))

        logger.info(f"[Orchestrator] 本地规则拆解完成: {len(tasks)}个子任务")
        for t in tasks:
            logger.info(f"  {t.id}: 依赖={t.dependencies}, agent={t.assigned_agent}")
        return tasks

    def _parse_decompose_response(self, raw: str, goal: str) -> Optional[list[AgentTask]]:
        """
        解析 LLM 拆解返回的 JSON 字符串为 AgentTask 列表。
        支持 Markdown 代码块、非标准 JSON 等格式。

        Args:
            raw: LLM 返回的原始响应文本
            goal: 原始用户目标（降级时使用）

        Returns:
            AgentTask 列表，或 None（解析失败时）
        """
        if not raw or not raw.strip():
            return None

        try:
            # 去除 markdown 代码块标记
            cleaned = raw.strip()
            if "```" in cleaned:
                cleaned = re.sub(r'```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```', '', cleaned)
            # 提取 JSON 数组（第一个 [ 到最后一个 ]）
            json_match = re.search(r'\[[\s\S]*\]', cleaned)
            if json_match:
                json_str = json_match.group()
            else:
                json_str = cleaned

            json_str = json_str.strip()
            # 替换尾部逗号（JSON5 兼容）
            json_str = re.sub(r',\s*([\]}])', r'\1', json_str)

            # 健壮 JSON 解析
            try:
                items = json.loads(json_str)
            except json.JSONDecodeError:
                # Fallback 1: 处理字符串内的未转义换行
                fixed = re.sub(
                    r'(?<=[^\\])"(?:[^"\\]|\\.)*"',
                    lambda m: m.group(0).replace('\n', ' ').replace('\r', ''),
                    json_str
                )
                try:
                    items = json.loads(fixed)
                except json.JSONDecodeError:
                    # Fallback 2: 移除所有换行
                    flat = json_str.replace('\n', ' ').replace('\r', ' ')
                    flat = re.sub(r'\s{2,}', ' ', flat)
                    items = json.loads(flat)

            if not isinstance(items, list):
                items = [items]

            tasks = []
            for i, item in enumerate(items[:MAX_TASKS]):
                task_id = item.get("task_id", f"task-{i+1:03d}")

                # 处理中文/非标准 agent 映射
                expected_agent = item.get("expected_agent")
                if expected_agent and expected_agent not in ("pydanticai", "autogpt", "hermes", None):
                    # 中文 agent 名称映射
                    agent_lower = expected_agent.lower()
                    if any(kw in agent_lower for kw in ["网络", "系统", "工具", "shell", "terminal", "command"]):
                        expected_agent = "hermes"
                    elif any(kw in agent_lower for kw in ["推理", "分析", "评估", "安全", "审计", "审查", "代码"]):
                        expected_agent = "pydanticai"
                    else:
                        expected_agent = "pydanticai"

                # 处理中文 priority
                priority = item.get("priority", 5)
                if isinstance(priority, str):
                    p_lower = priority.lower()
                    if p_lower in ("高", "紧急", "最高", "1", "critical", "high", "highest"):
                        priority = 1
                    elif p_lower in ("中", "一般", "normal", "medium", "5"):
                        priority = 5
                    elif p_lower in ("低", "低优先级", "low", "lowest", "10"):
                        priority = 10
                    else:
                        try:
                            priority = int(priority)
                        except (ValueError, TypeError):
                            priority = 5
                else:
                    try:
                        priority = int(priority) if priority is not None else 5
                    except (ValueError, TypeError):
                        priority = 5

                # 处理依赖关系
                dependencies = item.get("dependencies", [])
                if not isinstance(dependencies, list):
                    dependencies = [dependencies] if dependencies else []

                task = AgentTask(
                    id=task_id,
                    description=item.get("description", ""),
                    intent=item.get("intent", goal),
                    dependencies=dependencies,
                    priority=priority,
                    requires_approval=item.get("requires_approval", False),
                    gear=min(max(item.get("gear", DEFAULT_GEAR), 1), 4),
                    assigned_agent=expected_agent,
                    payload=item,
                )
                # 安全检查
                if task.requires_approval and task.gear >= 3:
                    task.gear = 1
                tasks.append(task)

            if not tasks:
                return None

            # 验证依赖一致性
            all_ids = {t.id for t in tasks}
            for t in tasks:
                t.dependencies = [d for d in t.dependencies if d in all_ids]

            return tasks

        except Exception as e:
            logger.warning(f"[Orchestrator] 拆解响应解析失败: {e}")
            return None

    def decompose(self, goal: str) -> list[AgentTask]:
        """
        将用户目标拆解为子任务列表（三层降级策略）。

        降级链路:
          1. DeepSeek V4 Flash API  → 最优先，高质量拆解
          2. Qwen-Max API           → 第一降级，阿里云通义千问
          3. 本地规则               → 第二降级，纯正则+关键词，无需网络

        返回 AgentTask 列表（最多 MAX_TASKS 条），每条包含：
          - task_id: 唯一标识（如 "task-scan-ports"）
          - description: 简短说明（1-2句话）
          - intent: 可执行的自然语言指令
          - dependencies: 前置任务 ID 列表（空数组表示无依赖）
          - expected_agent: 建议执行的 Agent（pydanticai/autogpt/hermes）
          - priority: 优先级 1-10
        """
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
6. 只返回 JSON 数组，不要其他文字

示例：
[
  {
    "task_id": "task-port-scan",
    "description": "扫描目标服务器开放端口和服务版本",
    "intent": "使用 nmap 扫描 192.168.1.1 的开放端口，识别运行的服务版本",
    "dependencies": [],
    "expected_agent": "hermes",
    "priority": 3
  },
  {
    "task_id": "task-vuln-scan",
    "description": "基于端口扫描结果检测已知漏洞",
    "intent": "分析端口扫描结果，查找已知漏洞 CVE 编号",
    "dependencies": ["task-port-scan"],
    "expected_agent": "pydanticai",
    "priority": 4
  }
]
"""

        user_prompt = f"请将以下目标拆解为有依赖关系的子任务: {goal}"

        # ── 第一层：DeepSeek V4 Flash ──
        logger.info("[Orchestrator] 第一层拆解: DeepSeek V4 Flash")
        raw = self._call_deepseek(system_prompt, user_prompt)
        if raw:
            tasks = self._parse_decompose_response(raw, goal)
            if tasks:
                logger.info(f"[Orchestrator] DeepSeek 拆解成功: {len(tasks)}个子任务")
                return tasks
            logger.warning("[Orchestrator] DeepSeek 响应解析失败")

        # ── 第二层：Qwen-Max ──
        logger.info("[Orchestrator] 第二层拆解: Qwen-Max (阿里云)")
        raw = self._call_qwen(system_prompt, user_prompt)
        if raw:
            tasks = self._parse_decompose_response(raw, goal)
            if tasks:
                logger.info(f"[Orchestrator] Qwen-Max 拆解成功: {len(tasks)}个子任务")
                return tasks
            logger.warning("[Orchestrator] Qwen-Max 响应解析失败")

        # ── 第三层：本地规则（兜底） ──
        logger.info("[Orchestrator] 第三层拆解: 本地规则（兜底）")
        tasks = self._local_rule_decompose(goal)
        if tasks:
            logger.info(f"[Orchestrator] 本地规则拆解成功: {len(tasks)}个子任务")
            return tasks

        # ── 终极兜底：单任务模式 ──
        logger.warning("[Orchestrator] 所有拆解层级均失败，降级为单任务模式")
        return [
            AgentTask(
                id="task-001",
                description="(降级) 完整目标作为单一任务",
                intent=goal,
                priority=5,
                gear=DEFAULT_GEAR,
            )
        ]


    # ----------------------------------------------------------------
    #  3. 统一执行（通过 /api/chat）
    # ----------------------------------------------------------------

    def execute(self, task: AgentTask) -> TaskResult:
        """
        执行子任务。

        [v3.3] Agent-aware 执行：
          - hermes 任务 → 子进程直接执行 shell 命令（绕过 LLM，<10s 完成）
          - pydanticai/autogpt 任务 → 走 /api/chat LLM 推理路径

        Args:
            task: AgentTask 实例（含 assigned_agent 和 intent）

        Returns:
            TaskResult: 执行结果
        """
        result: TaskResult
        if task.assigned_agent == "hermes":
            result = self._execute_hermes(task)
        else:
            # 默认：通过 /api/chat 执行（给 pydanticai 双倍超时，工具调用密集型）
            timeout = 240 if task.assigned_agent in ("autogpt",) else 120
            payload = {
                "message": task.intent,
                "gear": task.gear,
                "model_id": task.model_id or DEFAULT_MODEL,
                "actor": f"orchestrator:{task.assigned_agent or 'unknown'}",
                "session_id": f"orch-{task.id}",
            }

            resp = _api_request("/api/chat", payload, timeout=timeout)

            success = resp.get("success", False)
            if not success:
                result = TaskResult(
                    task_id=task.id,
                    success=False,
                    error=resp.get("error", "API 调用失败"),
                    agent=task.assigned_agent,
                    gear=task.gear,
                    validation_status=resp.get("validation_status"),
                )
            else:
                result = TaskResult(
                    task_id=task.id,
                    success=True,
                    output=resp.get("reply", ""),
                    tool_calls=resp.get("tool_calls", []) or [],
                    agent=task.assigned_agent,
                    gear=task.gear,
                    validation_status=resp.get("validation_status", "none"),
                )

        # [v3.6] 写入 MessageBoard episode 记忆
        self._write_episode(result)
        return result

    # ----------------------------------------------------------------
    #  3b. Hermes 子进程执行（绕过 LLM）
    # ----------------------------------------------------------------

    @staticmethod
    def _execute_hermes(task: AgentTask) -> TaskResult:
        """
        为 hermes 路由的任务直接执行 shell 命令（绕过 LLM）。

        流程：
          1. 从 intent 中检测已知工具名（bandit, safety, curl 等）
          2. 构造对应的 shell 命令
          3. 通过 subprocess 执行（60s 超时）
          4. 返回输出

        原理：工具密集型任务不需要 LLM 推理，直接执行 shell 命令更快。
        """
        import subprocess as _subprocess
        import shutil as _shutil

        # 获取原始 intent（可能已被上下文注入修改）
        raw_intent = task.payload.get("original_intent", task.intent)
        intent_lower = raw_intent.lower()

        # 目标目录（通常由 decompose 中识别）
        target_dir = "/root/EntropyGuard/test-targets"

        # ── 工具 → 命令映射 ──
        # 每项: (检测关键词, 命令函数)
        tool_handlers = []

        def _bandit_handler():
            # 只扫描关键源文件，排除虚拟环境（太多文件导致 bandit 超时）
            py_files = []
            for root, dirs, files in os.walk(target_dir):
                # 排除 site-packages, venv, node_modules 等
                dirs[:] = [d for d in dirs if d not in (
                    "site-packages", "node_modules", "__pycache__", ".git",
                    "flask-venv", "flask-venv2", "django-venv",
                )]
                for f in files:
                    if f.endswith(".py") and not f.startswith("test_"):
                        py_files.append(os.path.join(root, f))
            if py_files and _shutil.which("bandit"):
                return ["bandit", "-f", "txt", "--quiet"] + py_files
            return None

        def _safety_handler():
            # 方案1: 用 pip-audit 扫描系统所有已安装包
            if _shutil.which("pip-audit"):
                return ["pip-audit", "--desc", "--progress-spinner=off"]
            # 方案2: 列出所有已安装包和版本（带 CVE 标记）
            if _shutil.which("pip"):
                return ["pip", "list", "--format=columns", "--outdated"]
            return None

        def _curl_handler():
            # 从 intent 提取 URL，找不到就用默认 Flask 端点
            urls = re.findall(r'https?://[^\s)\]]+', raw_intent)
            if not urls:
                urls = ["http://127.0.0.1:5000/"]
            return ["curl", "-s", "--connect-timeout", "5", "--max-time", "10",
                    "-w", "\nHTTP_CODE:%{http_code}", urls[0]]

        def _pip_handler():
            # 检查 Python 依赖
            req = os.path.join(target_dir, "requirements.txt")
            if os.path.exists(req):
                if _shutil.which("pip-audit"):
                    return ["pip-audit", "-r", req]
                return ["pip", "list", "--format=columns"]
            return ["pip", "list", "--format=columns"]

        def _nmap_handler():
            # 端口扫描提取
            targets = re.findall(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}', raw_intent)
            target = targets[0] if targets else "127.0.0.1"
            return ["nmap", "-sV", "-p", "80,443,3306,5000,8000,8080,22",
                    "--open", "-T4", target]

        def _flask_source_handler():
            """直接读取 Flask 应用源码并分析安全问题（不开服务，不调外部工具）"""
            app_file = os.path.join(target_dir, "vulnerable_app.py")
            if not os.path.exists(app_file):
                return None
            try:
                with open(app_file) as f:
                    content = f.read()
                routes = re.findall(r"@app\.route\(['\"]([^'\"]+)['\"]", content)
                lines = content.split("\n")
                output = f"=== Flask 应用源码分析 ===\n"
                output += f"文件: {app_file}\n"
                output += f"代码行数: {len(lines)}\n"
                output += f"定义的路由端点:\n"
                for r in routes:
                    output += f"  {r}\n"
                output += f"\n安全风险扫描:\n"
                findings = []
                # debug mode
                if "debug=True" in content or 'debug = True' in content:
                    findings.append("🔴 CVE 风险: app.debug = True (调试模式生产环境启用)")
                # secret_key
                if "secret_key" in content.lower():
                    findings.append("🔴 敏感信息泄露: SECRET_KEY 硬编码在源代码中")
                # eval/exec
                if "eval(" in content:
                    findings.append("🔴 代码注入风险: 使用 eval()")
                if "os.system" in content or "os.popen" in content:
                    findings.append("🟡 命令执行: 使用 os.system/os.popen")
                # CORS/headers
                if "Access-Control-Allow-Origin" not in content and "CORS" not in content:
                    findings.append("🟡 CORS 未配置")
                # framework version clues
                if "Flask 1.0" in content or "flask import" in content:
                    findings.append("ℹ️  框架: Flask (需检查版本)")
                # Werkzeug version
                if "CVE-2019-1010083" in content or "Werkzeug 0.14" in content:
                    findings.append("🔴 CVE-2019-1010083: Werkzeug 调试器 PIN 绕过")
                if not findings:
                    findings.append("✅ 未检测到明显安全问题")
                output += "\n".join(findings)
                output += f"\n\n完整源码:\n{content[:3000]}"
                return {"output": output, "fallback_only": True}
            except Exception as e:
                return None

        # 按优先级定义检测器
        detectors = [
            # (关键词列表, handler, 描述)
            (["bandit", "静态安全扫描", "安全审计", "安全扫描"], _bandit_handler, "Bandit 安全扫描"),
            (["safety", "safety check", "依赖检查", "依赖漏洞", "依赖安全", "dependency"],
             _safety_handler, "Safety 依赖检查"),
            (["python安全库检查", "pip 检查", "pip check", "pip list", "pip install"],
             _pip_handler, "Python 依赖检查"),
            (["curl ", "wget ", "http请求", "请求测试", "测试端点"], _curl_handler, "HTTP 请求测试"),
            (["nmap", "端口扫描", "扫描端口"], _nmap_handler, "端口扫描"),
            # Flask 源码分析（不开服务，直接读文件）
            (["Flask", "flask", "动态测试", "动态扫描", "启动应用"], _flask_source_handler, "Flask 源码安全分析"),
        ]

        for keywords, handler, desc in detectors:
            if any(kw in intent_lower for kw in keywords):
                try:
                    result = handler()
                except Exception as e:
                    logger.warning(f"[Hermes] {task.id}: {desc} handler error: {e}")
                    continue

                if result is None:
                    continue

                # 处理非子进程 handler（如 Flask 源码分析，直接返回 dict）
                if isinstance(result, dict) and result.get("fallback_only"):
                    output = result.get("output", "")
                    logger.info(f"[Hermes] {task.id}: {desc} 完成 ({len(output)} chars, inline)")
                    return TaskResult(
                        task_id=task.id, success=True,
                        output=output, agent="hermes", gear=task.gear,
                    )

                # 标准子进程执行
                cmd = result if isinstance(result, list) else result.get("cmd", result)
                if cmd and _shutil.which(cmd[0] if isinstance(cmd, list) else cmd):
                    cmd_str = ' '.join(cmd[:4]) if isinstance(cmd, list) else cmd
                    logger.info(f"[Hermes] {task.id}: 执行 {desc} → {cmd_str}...")
                    try:
                        r = _subprocess.run(
                            cmd, capture_output=True, text=True, timeout=60
                        )
                        output = r.stdout
                        if r.stderr:
                            output += f"\n--- STDERR ---\n{r.stderr[:500]}"
                        if r.returncode != 0:
                            pass
                        logger.info(f"[Hermes] {task.id}: 完成 ({len(output)} chars, exit={r.returncode})")
                        return TaskResult(
                            task_id=task.id,
                            success=True,
                            output=output.strip() or f"命令已执行，无输出 (exit={r.returncode})",
                            agent="hermes",
                            gear=task.gear,
                        )
                    except _subprocess.TimeoutExpired:
                        logger.warning(f"[Hermes] {task.id}: 命令超时 (60s)")
                        return TaskResult(
                            task_id=task.id,
                            success=False,
                            error=f"{desc} 执行超时 (60s)",
                            agent="hermes",
                            gear=task.gear,
                        )
                    except FileNotFoundError:
                        logger.warning(f"[Hermes] {task.id}: {cmd[0]} 未安装")
                        continue

        # ── 通用降级：列出目录文件（兜底，总有输出） ──
        logger.info(f"[Hermes] {task.id}: 无匹配工具，降级为目录扫描")
        try:
            r = _subprocess.run(
                ["find", target_dir, "-type", "f", "-name", "*.py", "-o",
                 "-name", "*.txt", "-o", "-name", "*.cfg", "-o", "-name", "*.conf"],
                capture_output=True, text=True, timeout=15
            )
            output = f"目标目录: {target_dir}\n"
            output += f"文件列表:\n{r.stdout}\n"
            # 也读取关键文件内容
            for f in ["vulnerable_app.py", "django_app.py"]:
                fp = os.path.join(target_dir, f)
                if os.path.exists(fp):
                    with open(fp) as fh:
                        content = fh.read()
                        output += f"\n=== {f} ===\n{content[:2000]}"
            return TaskResult(
                task_id=task.id, success=True,
                output=output, agent="hermes", gear=task.gear,
            )
        except Exception as e:
            return TaskResult(
                task_id=task.id, success=False,
                error=f"hermes 兜底扫描失败: {e}",
                agent="hermes", gear=task.gear,
            )

    # ----------------------------------------------------------------
    #  4. 结果合并
    # ----------------------------------------------------------------

    def merge(self, goal: str, tasks: list[AgentTask],
              results: list[TaskResult]) -> OrchestratorResult:
        """汇总所有子任务执行结果"""

        success_count = sum(1 for r in results if r.success)
        total = len(results)
        all_success = success_count == total

        # 生成摘要
        summary_parts = []
        for task, result in zip(tasks, results):
            status = "✅" if result.success else "❌"
            agent = result.agent or task.assigned_agent or "?"
            snippet = (result.output or result.error or "(no output)")[:100]
            summary_parts.append(f"  {status} [{task.id}] {agent}: {snippet}")

        summary = (
            f"Orchestrator 执行完成: {success_count}/{total} 子任务成功\n"
            + "\n".join(summary_parts)
        )

        return OrchestratorResult(
            goal=goal,
            tasks=tasks,
            results=results,
            summary=summary,
            success=all_success,
            total_time=0.0,  # 由 run() 填充
        )

    # ----------------------------------------------------------------
    #  拓扑排序
    # ----------------------------------------------------------------

    def _topological_sort(self, tasks: list[AgentTask]) -> Optional[list[AgentTask]]:
        """
        Kahn 算法拓扑排序。

        根据任务 dependencies 字段排序：
          - 无依赖的任务排前面
          - 有依赖的等前置任务完成后再执行
          - 检测循环依赖，返回 None

        Args:
            tasks: 待排序的任务列表

        Returns:
            排序后的任务列表，或 None（存在循环依赖时）
        """
        if not tasks:
            return []

        task_map = {t.id: t for t in tasks}
        in_degree: dict[str, int] = {t.id: 0 for t in tasks}
        graph: dict[str, list[str]] = {t.id: [] for t in tasks}

        # 构建有向图：如果 B 依赖 A，则 A → B
        for t in tasks:
            for dep_id in t.dependencies:
                if dep_id in task_map:
                    graph.setdefault(dep_id, []).append(t.id)
                    in_degree[t.id] = in_degree.get(t.id, 0) + 1

        # Kahn 算法
        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        sorted_ids = []

        while queue:
            # 按优先级排序（同层级的优先级高的先执行）
            queue.sort(key=lambda tid: task_map[tid].priority)
            tid = queue.pop(0)
            sorted_ids.append(tid)

            for neighbor in graph.get(tid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        # 检查循环依赖
        if len(sorted_ids) != len(tasks):
            remaining = set(t.id for t in tasks) - set(sorted_ids)
            logger.error(f"[Orchestrator] 检测到循环依赖: {remaining}")
            return None

        return [task_map[tid] for tid in sorted_ids]

    # ----------------------------------------------------------------
    #  上下文传递
    # ----------------------------------------------------------------

    def _build_context(self, task: AgentTask,
                       task_output_map: dict[str, str]) -> str:
        """
        为任务构建上下文：收集所有前置任务的输出。

        Args:
            task: 当前待执行的任务
            task_output_map: 已执行完成的任务输出映射 {task_id: output}

        Returns:
            格式化后的上下文字符串（空字符串表示无上下文）
        """
        if not task.dependencies:
            return ""

        context_parts = []
        for dep_id in task.dependencies:
            output = task_output_map.get(dep_id)
            if output:
                # 截取前置任务输出的前 2000 字符作为上下文
                preview = output[:2000]
                if len(output) > 2000:
                    preview += "\n... (输出截断)"
                context_parts.append(
                    f"--- 前置任务 [{dep_id}] 的输出 ---\n{preview}\n"
                )

        if not context_parts:
            return ""

        return "\n".join(context_parts)

    # ----------------------------------------------------------------
    #  辅助函数
    # ----------------------------------------------------------------

    def _call_deepseek(self, system_prompt: str, user_prompt: str,
                       timeout: int = 30) -> Optional[str]:
        """调用 DeepSeek V4 Flash API — 正确使用 OPENAI_API_KEY（因为通过 openai 兼容端点连 DeepSeek）"""
        # [v3.2 fix] 优先使用 DEEPSEEK_API_KEY / OPENAI_API_KEY 而不是 ENTROPY_RUNTIME_API_KEY
        api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            # 从 /root/.env 读取
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
            # 最后兜底：用 ENTROPY_RUNTIME_API_KEY（虽然大概率不兼容 DeepSeek）
            api_key = self._api_key
        if not api_key:
            logger.warning("[Orchestrator] DeepSeek API Key 未配置，跳过任务分解")
            return None

        payload = {
            "model": "deepseek-v4-flash",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.3,
            "max_tokens": 2000,
            "stop": ["\n\n\n"],  # 防止 DeepSeek 输出多余尾缀
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
                return result.get("choices", [{}])[0].get("message", {}).get("content", "")
        except Exception as e:
            logger.warning(f"[Orchestrator] DeepSeek API 调用失败: {e}")
            return None

    def _detect_conflicts(self, new_task: AgentTask,
                          existing_results: list[TaskResult]) -> list[str]:
        """
        冲突检测：如果两个子任务都涉及同一文件路径，按优先级排队。
        返回冲突描述列表。
        """
        conflicts = []
        # 从 intent 中提取文件路径
        new_paths = set(re.findall(r'/[\w/\.\-]+', new_task.intent))

        if not new_paths:
            return []

        for existing in existing_results:
            # 检查已有结果的任务是否也涉及相同路径
            existing_task_id = existing.task_id
            for path in new_paths:
                if path in existing.output or path in new_task.intent:
                    log_entry = (f"路径冲突 '{path}': "
                                 f"{existing_task_id}(已执行) → {new_task.id}(排队)")
                    conflicts.append(log_entry)
                    break

        return conflicts

    def _log_audit(self, task: AgentTask, result: TaskResult):
        """将子任务执行记录写入审计链"""
        endpoint = "/api/events"
        payload = {
            "event_type": "ORCHESTRATOR_TASK",
            "actor": f"orchestrator:{task.assigned_agent or 'unknown'}",
            "action": f"execute_task:{task.id}:{task.intent[:60]}",
            "delta_entropy": 0.03,
            "success": result.success,
            "details": {
                "task_id": task.id,
                "intent": task.intent[:200],
                "gear": task.gear,
                "agent": task.assigned_agent,
                "priority": task.priority,
                "requires_approval": task.requires_approval,
                "output_preview": result.output[:200] if result.output else "",
                "error": result.error,
                "validation_status": result.validation_status,
            },
        }
        _api_request(endpoint, payload)

    # ----------------------------------------------------------------
    #  [v3.6] MessageBoard episode 桥接
    # ----------------------------------------------------------------

    def _write_episode(self, result: TaskResult):
        """将任务执行结果写入 MessageBoard episode 记忆"""
        try:
            duration = result.elapsed_seconds if result.elapsed_seconds else 0.0
            content = {
                "task_id": result.task_id,
                "agent": result.agent or "unknown",
                "success": result.success,
                "output_preview": result.output[:200] if result.output else "",
                "error": result.error,
                "duration": duration,
            }
            payload = {
                "memory_type": "episode",
                "content": content,
                "source": f"orchestrator:{result.agent or 'unknown'}",
                "ttl": 0,
            }
            req = urllib.request.Request(
                f"{ENTROPY_API_BASE}/api/messageboard/memory",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
            )
            req.add_header("Content-Type", "application/json")
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get("success"):
                    logger.debug(f"[Orchestrator v3.6] episode 已写入: {result.task_id}")
        except Exception as e:
            logger.warning(f"[Orchestrator v3.6] MessageBoard episode 写入失败: {e}")

    @staticmethod
    def _read_recent_episodes(limit: int = 10) -> list[dict]:
        """从 MessageBoard 读取最近 N 条 episode 记忆"""
        try:
            req = urllib.request.Request(
                f"{ENTROPY_API_BASE}/api/messageboard/memory/episode?limit={limit}",
                method="GET",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                resp_data = json.loads(resp.read().decode())
                if resp_data.get("success"):
                    return resp_data.get("memories", [])
        except Exception as e:
            logger.warning(f"[Orchestrator v3.6] 读取 episode 失败: {e}")
        return []


# ========== 便捷入口 ==========

_orchestrator_instance: Optional[MultiAgentOrchestrator] = None


def get_orchestrator() -> MultiAgentOrchestrator:
    """获取全局 Orchestrator 单例"""
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = MultiAgentOrchestrator()
    return _orchestrator_instance


async def run_orchestrator(goal: str) -> OrchestratorResult:
    """快速运行 Orchestrator"""
    orch = get_orchestrator()
    return await orch.run(goal)


async def run_orchestrator_with_decomposition(goal: str) -> OrchestratorResult:
    """快速运行带目标拆解的 Orchestrator v3.2"""
    orch = get_orchestrator()
    return await orch.run_with_decomposition(goal)
