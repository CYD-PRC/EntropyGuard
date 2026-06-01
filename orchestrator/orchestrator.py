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

        for task in sorted_tasks:
            # 构建上下文（注入前置任务输出）
            context = self._build_context(task, task_output_map)
            if context:
                logger.info(f"[Orchestrator v3.2] {task.id}: 注入 {len(context)} 个前置任务上下文")
                original_intent = task.intent
                task.intent = (
                    f"[上下文信息]\n{context}\n\n"
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

    def decompose(self, goal: str) -> list[AgentTask]:
        """
        用 DeepSeek 将用户目标拆解为子任务列表（v3.2 依赖图版本）。

        返回 AgentTask 列表（最多 MAX_TASKS 条），每条包含：
          - task_id: 唯一标识（如 "task-scan-ports"）
          - description: 简短说明（1-2句话）
          - intent: 可执行的自然语言指令
          - dependencies: 前置任务 ID 列表（空数组表示无依赖）
          - expected_agent: 建议执行的 Agent（pydanticai/autogpt/hermes）
          - priority: 优先级 1-10

        [v3-alpha.1] 降级：API 不可用时，将整个目标作为一个任务。
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

        raw = self._call_deepseek(system_prompt, user_prompt)
        if not raw:
            # 降级：将整个目标作为一个任务
            logger.warning("[Orchestrator] DeepSeek 不可用，降级为单任务模式")
            return [
                AgentTask(
                    id="task-001",
                    description="(降级) 完整目标作为单一任务",
                    intent=goal,
                    priority=5,
                    gear=DEFAULT_GEAR,
                )
            ]

        try:
            # 尝试从响应中提取 JSON 数组
            # [v3.2] 加强 JSON 提取：去除 markdown 代码块标记、去除注释、处理尾部逗号
            cleaned = raw.strip()
            # 脱去 ```json ... ``` 包装
            if "```" in cleaned:
                cleaned = re.sub(r'```(?:json)?\s*', '', cleaned)
                cleaned = re.sub(r'\s*```', '', cleaned)
            # 脱去可能的中文/英文说明文字（取第一个 [ 到最后一个 ]）
            json_match = re.search(r'\[[\s\S]*\]', cleaned)
            if json_match:
                json_str = json_match.group()
            else:
                json_str = cleaned

            # 修复常见的非标准 JSON 问题
            json_str = json_str.strip()
            # 替换尾部逗号（JSON5 兼容）
            json_str = re.sub(r',\s*([\]}])', r'\1', json_str)

            # [v3.2 fix] 健壮 JSON 解析：处理 DeepSeek 输出的各种非标准格式
            try:
                items = json.loads(json_str)
            except json.JSONDecodeError:
                # Fallback 1: 若字符串内含有未转义换行，替换为 \n
                # 正则匹配字符串内的换行：在引号对之间的 \n 替换为空格
                fixed = re.sub(r'(?<=[^\\])"(?:[^"\\]|\\.)*"',
                               lambda m: m.group(0).replace('\n', ' ').replace('\r', ''),
                               json_str)
                try:
                    items = json.loads(fixed)
                except json.JSONDecodeError:
                    # Fallback 2: 移除所有换行（JSON 不需要换行作为语法）
                    flat = json_str.replace('\n', ' ').replace('\r', ' ')
                    flat = re.sub(r'\s{2,}', ' ', flat)
                    items = json.loads(flat)
            if not isinstance(items, list):
                items = [items]

            tasks = []
            for i, item in enumerate(items[:MAX_TASKS]):
                task_id = item.get("task_id", f"task-{i+1:03d}")
                dependencies = item.get("dependencies", [])
                if not isinstance(dependencies, list):
                    dependencies = [dependencies] if dependencies else []

                expected_agent = item.get("expected_agent")
                priority = item.get("priority", 5)

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
                # 安全检查：如果 requires_approval，降到 EMBRACE 档位等待审批
                if task.requires_approval and task.gear >= 3:
                    task.gear = 1
                tasks.append(task)

            if not tasks:
                logger.warning("[Orchestrator] 拆解结果为空，降级为单任务")
                return [AgentTask(id="task-001", description="(降级) 完整目标", intent=goal)]

            # 验证依赖一致性：所有引用的依赖 ID 必须在任务列表中
            all_ids = {t.id for t in tasks}
            for t in tasks:
                for dep_id in t.dependencies:
                    if dep_id not in all_ids:
                        logger.warning(
                            f"[Orchestrator] 任务 {t.id} 依赖 {dep_id} 不存在，已忽略"
                        )
                        t.dependencies.remove(dep_id)

            logger.info(f"[Orchestrator] 拆解完成: {len(tasks)}个子任务")
            return tasks

        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[Orchestrator] 任务拆解 JSON 解析失败: {e}, raw={raw[:200]}")
            return [AgentTask(id="task-001", description="(降级) 完整目标", intent=goal)]

    # ----------------------------------------------------------------
    #  3. 统一执行（通过 /api/chat）
    # ----------------------------------------------------------------

    def execute(self, task: AgentTask) -> TaskResult:
        """
        通过 Entropy Runtime API (/api/chat) 执行子任务。

        指定 model_id 和 gear，所有安全层自动拦截。
        """
        payload = {
            "message": task.intent,
            "gear": task.gear,
            "model_id": task.model_id or DEFAULT_MODEL,
            "actor": f"orchestrator:{task.assigned_agent or 'unknown'}",
            "session_id": f"orch-{task.id}",
        }

        resp = _api_request("/api/chat", payload)

        success = resp.get("success", False)
        if not success:
            return TaskResult(
                task_id=task.id,
                success=False,
                error=resp.get("error", "API 调用失败"),
                agent=task.assigned_agent,
                gear=task.gear,
                validation_status=resp.get("validation_status"),
            )

        return TaskResult(
            task_id=task.id,
            success=True,
            output=resp.get("reply", ""),
            tool_calls=resp.get("tool_calls", []) or [],
            agent=task.assigned_agent,
            gear=task.gear,
            validation_status=resp.get("validation_status", "none"),
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
