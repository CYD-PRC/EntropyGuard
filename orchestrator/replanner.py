"""Entropy Runtime · 动态重规划引擎
v7.2 Phase 3.4: 子任务失败或元认知异常时自动修改后续计划。

三种重规划触发:
  1. replan_on_failure    — 子任务失败
  2. replan_on_drift      — 元认知偏离/红旗
  3. replan_on_conflict   — 宇宙透镜冲突

防循环机制:
  - 同一子任务最多重规划 3 次
  - 全局重规划次数不超过计划总任务数的 50%
  - 超限标记为 ESCALATE_HUMAN
"""
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult
from orchestrator.metacognition import CheckResult, CheckStatus, Suggestion

logger = logging.getLogger("entropyruntime.replanner")

MAX_REPLANS_PER_TASK = 3
MAX_REPLAN_RATIO = 0.5


class ReplanAction(Enum):
    CONTINUE = "continue"             # 无需重规划
    RETRY_WITH_NEW_AGENT = "retry_new_agent"  # 换 Agent 重试
    RETRY_WITH_MODIFIED_INTENT = "retry_modify"  # 修改 intent 后重试
    SKIP_AND_CONTINUE = "skip"        # 跳过失败子任务，继续
    REMOVE_DEPENDENTS = "remove"      # 移除所有依赖失败的后续任务
    ESCALATE_HUMAN = "escalate"       # 请求人工介入
    REPLAN_ENTIRE = "replan_entire"   # 完全重规划后续
    REORDER = "reorder"               # 重排剩余任务顺序


@dataclass
class ModifiedPlan:
    """重规划后的修改计划"""
    action: ReplanAction
    modified_tasks: list[AgentTask] = field(default_factory=list)
    removed_tasks: list[str] = field(default_factory=list)
    new_tasks: list[AgentTask] = field(default_factory=list)
    reason: str = ""
    reorder: bool = False
    escalated: bool = False
    replan_count: int = 0

    def has_changes(self) -> bool:
        return bool(self.modified_tasks or self.removed_tasks or self.new_tasks)


class Replanner:
    """动态重规划引擎 — 执行循环的智能决策层。"""

    def __init__(self):
        self._task_replan_count: dict[str, int] = {}
        self._global_replan_count = 0
        self._replan_log: list[dict] = []

    @property
    def replan_log(self) -> list[dict]:
        return list(self._replan_log)

    # ---- 公共 API ----

    def replan_on_failure(
        self,
        failed_task: AgentTask,
        result: TaskResult,
        remaining_tasks: list[AgentTask],
        task_output_map: dict[str, str],
        all_tasks: list[AgentTask],
        context: Optional[dict] = None,
    ) -> ModifiedPlan:
        """子任务执行失败后的重规划。

        分析失败原因，决定如何修改后续计划。
        """
        task_id = failed_task.id
        replan_count = self._task_replan_count.get(task_id, 0) + 1
        self._task_replan_count[task_id] = replan_count
        self._global_replan_count += 1

        # 防循环检查（_check_limits 处理 per-task 和 global 双重限制）
        limit_check = self._check_limits(task_id, replan_count, len(all_tasks))
        if limit_check:
            return limit_check

        error_text = (result.error or "").lower()
        dependents = [
            t for t in remaining_tasks
            if task_id in t.dependencies
        ]

        # --- 策略 1: 网络/外部依赖错误 → 跳过 + 修改依赖任务 intent ---
        if any(kw in error_text for kw in [
            "timeout", "connection", "refused", "dns", "无法访问",
            "time out", "i/o timeout",
        ]):
            modified = self._modify_dependents_intent(
                dependents, f"由于 {task_id} 网络异常，请直接从上下文分析"
            )
            log_entry = self._log_replan(
                task_id, ReplanAction.SKIP_AND_CONTINUE,
                f"网络错误, 跳过 {task_id}, 修改 {len(dependents)} 个依赖任务",
            )
            return ModifiedPlan(
                action=ReplanAction.SKIP_AND_CONTINUE,
                modified_tasks=modified,
                removed_tasks=[task_id],
                reason=f"{task_id}: 网络错误，跳过该子任务",
                reorder=False, replan_count=replan_count,
            )

        # --- 策略 2: Agent 可重试 → 换 Agent ---
        if replan_count <= 2:
            recommended_agent = self._get_recommended_agent(
                failed_task.intent, exclude=failed_task.assigned_agent,
            )
            if not recommended_agent:
                # 无历史数据时，默认尝试 pydanticai（全类型通用）
                if failed_task.assigned_agent and failed_task.assigned_agent != "pydanticai":
                    recommended_agent = "pydanticai"
                elif failed_task.assigned_agent != "hermes":
                    recommended_agent = "hermes"
            if recommended_agent:
                failed_task.assigned_agent = recommended_agent
                log_entry = self._log_replan(
                    task_id, ReplanAction.RETRY_WITH_NEW_AGENT,
                    f"换 Agent: {recommended_agent}",
                )
                return ModifiedPlan(
                    action=ReplanAction.RETRY_WITH_NEW_AGENT,
                    modified_tasks=[failed_task] + dependents,
                    reason=f"为 {task_id} 重新分配 Agent: {recommended_agent}",
                    reorder=False, replan_count=replan_count,
                )

        # --- 策略 3: 多次失败 → 移除依赖的后续任务 ---
        if replan_count >= MAX_REPLANS_PER_TASK:
            removed = [t.id for t in dependents]
            log_entry = self._log_replan(
                task_id, ReplanAction.REMOVE_DEPENDENTS,
                f"重试 {replan_count} 次仍失败, 移除依赖: {removed}",
            )
            return ModifiedPlan(
                action=ReplanAction.REMOVE_DEPENDENTS,
                removed_tasks=[task_id] + removed,
                reason=f"{task_id}: 重试 {replan_count} 次仍失败, 移除依赖",
                escalated=True, replan_count=replan_count,
            )

        # --- 策略 4: 默认 → 记录告警，继续执行 ---
        log_entry = self._log_replan(
            task_id, ReplanAction.CONTINUE,
            f"失败但继续 ({error_text[:50]})",
        )
        return ModifiedPlan(
            action=ReplanAction.CONTINUE,
            reason=f"{task_id}: 失败继续 ({error_text[:50]})",
            replan_count=replan_count,
        )

    def replan_on_drift(
        self,
        task: AgentTask,
        check_result: CheckResult,
        remaining_tasks: list[AgentTask],
        all_tasks: list[AgentTask],
    ) -> ModifiedPlan:
        """元认知检测到偏离/红旗后的重规划。"""
        task_id = task.id
        replan_count = self._task_replan_count.get(task_id, 0) + 1
        self._task_replan_count[task_id] = replan_count
        self._global_replan_count += 1

        limit_check = self._check_limits(task_id, replan_count, len(all_tasks))
        if limit_check:
            return limit_check

        drift = check_result.drift_score

        # 根据建议选择策略
        if check_result.suggestion == Suggestion.ESCALATE:
            log_entry = self._log_replan(
                task_id, ReplanAction.ESCALATE_HUMAN,
                f"元认知建议人工介入 (drift={drift:.2f})",
            )
            return ModifiedPlan(
                action=ReplanAction.ESCALATE_HUMAN,
                reason=f"{task_id}: 元认知建议人工介入 (drift={drift:.2f})",
                escalated=True, replan_count=replan_count,
            )

        if check_result.suggestion == Suggestion.DEGRADE:
            # 降级 Agent 尝试
            recommended = self._get_recommended_agent(task.intent)
            if recommended and recommended != task.assigned_agent:
                task.assigned_agent = recommended
                log_entry = self._log_replan(
                    task_id, ReplanAction.RETRY_WITH_NEW_AGENT,
                    f"偏离降级 Agent: {recommended}",
                )
                return ModifiedPlan(
                    action=ReplanAction.RETRY_WITH_NEW_AGENT,
                    modified_tasks=[task],
                    reason=f"{task_id}: 偏离降级 {recommended} (drift={drift:.2f})",
                    replan_count=replan_count,
                )

        # 默认: 修改 intent 重试
        task.intent = f"{task.intent}\n[重规划提示] 之前尝试偏离度 {drift:.2f}，请严格按目标输出"
        log_entry = self._log_replan(
            task_id, ReplanAction.RETRY_WITH_MODIFIED_INTENT,
            f"注入偏离修正提示 (drift={drift:.2f})",
        )
        return ModifiedPlan(
            action=ReplanAction.RETRY_WITH_MODIFIED_INTENT,
            modified_tasks=[task],
            reason=f"{task_id}: 注入偏离修正 (drift={drift:.2f})",
            replan_count=replan_count,
        )

    def replan_on_conflict(
        self,
        conflict_info: dict,
        remaining_tasks: list[AgentTask],
        all_tasks: list[AgentTask],
    ) -> ModifiedPlan:
        """宇宙透镜冲突时的计划重排。"""
        self._global_replan_count += 1

        severity = conflict_info.get("severity", "NONE")
        if severity == "CRITICAL":
            # 生存级任务插队到最前面
            survival_tasks = [
                t for t in remaining_tasks
                if t.payload.get("cosmic_lense", {}).get("tier") == "生存"
            ]
            non_survival = [
                t for t in remaining_tasks if t not in survival_tasks
            ]
            reordered = survival_tasks + non_survival
            log_entry = self._log_replan(
                "conflict", ReplanAction.REORDER,
                f"CRITICAL 冲突, {len(survival_tasks)} 个生存级任务插队",
            )
            return ModifiedPlan(
                action=ReplanAction.REORDER,
                modified_tasks=reordered,
                reason=f"生存级任务插队: {len(survival_tasks)} 个前置",
                reorder=True, replan_count=0,
            )

        if severity == "WARNING":
            log_entry = self._log_replan(
                "conflict", ReplanAction.CONTINUE,
                f"WARNING 冲突, 继续执行",
            )
            return ModifiedPlan(
                action=ReplanAction.CONTINUE,
                reason=f"冲突 WARNING, 继续执行",
                replan_count=0,
            )

        return ModifiedPlan(
            action=ReplanAction.CONTINUE,
            reason="无冲突",
            replan_count=0,
        )

    # ---- 重规划历史 ----

    def get_log(self) -> list[dict]:
        return self._replan_log[-100:]

    # ---- 内部方法 ----

    def _check_limits(
        self, task_id: str, replan_count: int, total_tasks: int,
    ) -> Optional[ModifiedPlan]:
        """防循环检查。"""
        if replan_count > MAX_REPLANS_PER_TASK:
            self._log_replan(
                task_id, ReplanAction.ESCALATE_HUMAN,
                f"同一子任务重规划 {replan_count} 次超过限制",
            )
            return ModifiedPlan(
                action=ReplanAction.ESCALATE_HUMAN,
                reason=f"{task_id}: 同一子任务重规划 {replan_count} 次",
                escalated=True, replan_count=replan_count,
            )
        total_replanned = len(self._replan_log)
        max_allowed = max(int(total_tasks * MAX_REPLAN_RATIO) + 1, MAX_REPLANS_PER_TASK)
        if total_replanned > max_allowed:
            self._log_replan(
                task_id, ReplanAction.ESCALATE_HUMAN,
                f"全局重规划 {len(self._replan_log)} 次超限 ({max_allowed})",
            )
            return ModifiedPlan(
                action=ReplanAction.ESCALATE_HUMAN,
                reason=f"全局重规划 {len(self._replan_log)} 次超限",
                escalated=True, replan_count=replan_count,
            )
        return None

    def _modify_dependents_intent(
        self, dependents: list[AgentTask], hint: str,
    ) -> list[AgentTask]:
        """修改依赖任务的 intent，附加上下文提示。"""
        modified = []
        for t in dependents:
            t.intent = f"{t.intent}\n[上下文提示] {hint}"
            modified.append(t)
        return modified

    def _get_recommended_agent(
        self, intent: str, exclude: Optional[str] = None,
    ) -> Optional[str]:
        """从记忆存储中查询推荐的 Agent。"""
        try:
            from orchestrator.memory_store import MemoryStore
            store = MemoryStore()
            recommended = store.get_recommended_agent(intent)
            store.close()
            if recommended and recommended != exclude:
                return recommended
        except Exception as e:
            logger.warning("[Replanner] 查询推荐 Agent 失败: %s", e)
        return None

    def _log_replan(
        self, task_id: str, action: ReplanAction, reason: str,
    ) -> dict:
        """记录重规划事件到内部日志。"""
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_id": task_id,
            "action": action.value,
            "reason": reason,
        }
        self._replan_log.append(entry)
        logger.info("[Replanner] %s → %s: %s", task_id, action.value, reason)
        return entry

    def save_replan_to_memory(self, plan: ModifiedPlan, task_id: str):
        """将重规划事件写入 memory_store 持久化。"""
        try:
            from orchestrator.memory_store import MemoryStore
            store = MemoryStore()
            store.save_episode(
                task_id=f"replan-{task_id}-{int(time.time())}",
                agent="orchestrator:replanner",
                intent=f"replan: {plan.reason}",
                output=json.dumps({
                    "action": plan.action.value,
                    "reason": plan.reason,
                    "removed": plan.removed_tasks,
                    "modified": [t.id for t in plan.modified_tasks],
                    "escalated": plan.escalated,
                }, ensure_ascii=False),
                success=not plan.escalated,
                metadata={"replan_type": plan.action.value},
            )
            store.close()
        except Exception as e:
            logger.warning("[Replanner] 保存重规划记忆失败: %s", e)

    def reset(self):
        """重置重规划状态（新计划开始时调用）。"""
        self._task_replan_count.clear()
        self._global_replan_count = 0
        self._replan_log.clear()
        logger.info("[Replanner] 状态已重置")
