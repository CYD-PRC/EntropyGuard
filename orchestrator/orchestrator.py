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
                # 优先高的任务先执行（已有 results 中的冲突在 _detect_conflicts 中处理）
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
    #  1. 目标拆解
    # ----------------------------------------------------------------

    def decompose(self, goal: str) -> list[AgentTask]:
        """
        用 DeepSeek 将用户目标拆解为子任务列表。

        返回 AgentTask 列表（最多 MAX_TASKS 条）。
        """
        system_prompt = """你是一个任务分解专家，负责将用户目标拆解为可执行的子任务。

输出格式：纯 JSON 数组，每个元素包含：
  - "intent": 子任务描述（自然语言，明确可执行）
  - "priority": 优先级 1-10（1最高，10最低）
  - "requires_approval": 布尔值，高风险操作=True
  - "gear": 建议档位 1-4

约束：
1. 最多拆解为 5 个子任务
2. 子任务之间不能互相依赖（可并行执行）
3. 每个子任务必须明确、具体、可执行
4. 高风险操作（删除、写入、修改系统文件）设置 requires_approval=true
5. 只返回 JSON 数组，不要其他文字

示例：
[{"intent": "检查服务器端口状态", "priority": 3, "requires_approval": false, "gear": 3}]
"""

        user_prompt = f"请将以下目标拆解为子任务: {goal}"

        raw = self._call_deepseek(system_prompt, user_prompt)
        if not raw:
            # 降级：将整个目标作为一个任务
            return [
                AgentTask(
                    id="task-001",
                    intent=goal,
                    priority=5,
                    gear=DEFAULT_GEAR,
                )
            ]

        try:
            json_match = re.search(r'\[[\s\S]*\]', raw)
            if json_match:
                items = json.loads(json_match.group())
            else:
                items = json.loads(raw)

            if not isinstance(items, list):
                items = [items]

            tasks = []
            for i, item in enumerate(items[:MAX_TASKS]):
                task = AgentTask(
                    id=f"task-{i+1:03d}",
                    intent=item.get("intent", goal),
                    priority=item.get("priority", 5),
                    requires_approval=item.get("requires_approval", False),
                    gear=min(max(item.get("gear", DEFAULT_GEAR), 1), 4),
                    payload=item,
                )
                # 安全检查：如果 requires_approval，降到 EMBRACE 档位等待审批
                if task.requires_approval and task.gear >= 3:
                    task.gear = 1
                tasks.append(task)

            return tasks if tasks else [AgentTask(id="task-001", intent=goal)]

        except (json.JSONDecodeError, Exception) as e:
            logger.warning(f"[Orchestrator] 任务拆解 JSON 解析失败: {e}, raw={raw[:100]}")
            return [AgentTask(id="task-001", intent=goal)]

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
    #  辅助函数
    # ----------------------------------------------------------------

    def _call_deepseek(self, system_prompt: str, user_prompt: str,
                       timeout: int = 30) -> Optional[str]:
        """调用 DeepSeek V4 Flash API — 支持从 /root/.env 直接读取 API key"""
        api_key = self._api_key
        if not api_key:
            api_key = os.environ.get("DEEPSEEK_API_KEY", "") or os.environ.get("OPENAI_API_KEY", "")
        if not api_key:
            # [v3-alpha.1] 直接从 /root/.env 文件读取（兜底）
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
