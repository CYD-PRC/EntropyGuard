"""Entropy Runtime · 规划模块
v5.0: 拓扑排序、并行层级划分、动态重规划。
"""
import json
import logging
import time
from typing import Optional

from orchestrator.task_model import AgentTask, TaskResult
from orchestrator.memory import write_episode
from orchestrator.decompose import decompose
from notify.wechat import send_retry_notification

logger = logging.getLogger("entropyruntime.planner")


def topological_sort_levels(tasks: list[AgentTask]) -> Optional[list[list[AgentTask]]]:
    """Kahn 算法拓扑排序 + 并行层级划分"""
    if not tasks:
        return []

    task_map = {t.id: t for t in tasks}
    in_degree: dict[str, int] = {t.id: 0 for t in tasks}
    graph: dict[str, list[str]] = {t.id: [] for t in tasks}

    for t in tasks:
        for dep_id in t.dependencies:
            if dep_id in task_map:
                graph.setdefault(dep_id, []).append(t.id)
                in_degree[t.id] = in_degree.get(t.id, 0) + 1

    current_level = [tid for tid, deg in in_degree.items() if deg == 0]
    levels = []
    processed = set()

    while current_level:
        current_level.sort(key=lambda tid: task_map[tid].priority)
        levels.append([task_map[tid] for tid in current_level])
        processed.update(current_level)

        next_level = []
        for tid in current_level:
            for neighbor in graph.get(tid, []):
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    next_level.append(neighbor)
        current_level = next_level

    all_ids = set(t.id for t in tasks)
    if len(processed) != len(all_ids):
        remaining = all_ids - processed
        logger.error(f"[Planner] 循环依赖: {remaining}")
        return None
    return levels


def build_context(task: AgentTask, task_output_map: dict[str, str]) -> str:
    """为任务构建前置任务输出的上下文"""
    if not task.dependencies:
        return ""
    context_parts = []
    for dep_id in task.dependencies:
        output = task_output_map.get(dep_id)
        if output:
            preview = output[:2000]
            if len(output) > 2000:
                preview += "\n... (截断)"
            context_parts.append(f"--- 前置任务 [{dep_id}] 的输出 ---\n{preview}\n")
    return "\n".join(context_parts)


def should_replan(task: AgentTask, result: TaskResult,
                  remaining_tasks: list[AgentTask],
                  task_output_map: dict[str, str],
                  goal: str) -> Optional[list[AgentTask]]:
    """检查是否需要重规划剩余任务（失败且依赖存在时触发）"""
    if result.success:
        return None

    dependent_tasks = [t for t in remaining_tasks if task.id in t.dependencies]
    if not dependent_tasks:
        return None

    dep_ids = [t.id for t in dependent_tasks]
    logger.warning(f"[Planner v5.0] {task.id} 失败，{len(dep_ids)} 个依赖任务: {dep_ids}")

    replan_parts = [f"原目标剩余部分"]
    if task_output_map:
        replan_parts.append("已完成任务:")
        for tid, output in task_output_map.items():
            replan_parts.append(f"  {tid}: {output[:200]}")
    replan_parts.append(f"失败任务 [{task.id}]: {result.error or '未知错误'}")
    replan_parts.append(f"未完成任务: {'; '.join(t.description[:60] for t in dependent_tasks)}")
    replan_goal = "\n".join(replan_parts)

    # 记录重规划事件
    dummy_result = TaskResult(
        task_id=f"replan-{task.id}", success=False,
        output="", error=result.error, agent="orchestrator")
    write_episode(f"replan-{task.id}", "orchestrator", False,
                  error=result.error, source="orchestrator:planner",
                  replan=True,
                  reason=f"{task.id} 失败，{len(dep_ids)} 个任务依赖")

    logger.info(f"[Planner v5.0] 重新拆解剩余部分...")
    new_tasks = decompose(replan_goal)
    if new_tasks:
        logger.info(f"[Planner v5.0] 重规划: {len(new_tasks)} 个新任务")
        for nt in new_tasks:
            logger.info(f"  {nt.id}: {nt.description[:60]}")
    return new_tasks
