"""Entropy Runtime Orchestrator · 断点续跑
v7.2: 每个子任务执行完成后持久化 checkpoint，进程重启后自动恢复。

checkpoint 结构 (/tmp/entropy_checkpoint_{task_id}.json):
{
  "task_id": "orch-abc123",
  "goal": "original goal text",
  "checkpoint_time": "2026-06-05T12:30:00Z",
  "completed_tasks": {
    "task-001": {"success": true, "agent": "hermes", "output": "...", "elapsed_seconds": 5.2},
    "task-002": {"success": true, "agent": "pydanticai", "output": "...", "elapsed_seconds": 3.1}
  },
  "task_output_map": {"task-001": "...", "task-002": "..."},
  "remaining_task_ids": ["task-003", "task-004"],
  "total_tasks": 4
}
"""
import json
import logging
import os
import time
from typing import Optional

from orchestrator.task_model import AgentTask, TaskResult

logger = logging.getLogger("entropyruntime.checkpoint")

CHECKPOINT_DIR = "/tmp"


def _checkpoint_path(task_id: str) -> str:
    """checkpoint 文件路径"""
    # 清理 task_id 防止路径穿越
    safe_id = task_id.replace("/", "_").replace("..", "_")
    return os.path.join(CHECKPOINT_DIR, f"entropy_checkpoint_{safe_id}.json")


def save_checkpoint(
    task_id: str,
    goal: str,
    completed_tasks: list[AgentTask],
    completed_results: list[TaskResult],
    remaining_tasks: list[AgentTask],
    task_output_map: dict[str, str],
) -> None:
    """保存当前执行进度到 checkpoint 文件。

    Args:
        task_id: 执行计划 ID
        goal: 原始目标
        completed_tasks: 已完成的子任务列表
        completed_results: 已完成的任务结果列表
        remaining_tasks: 未完成的子任务列表
        task_output_map: {task_id: output_text} 映射
    """
    if not completed_tasks and not completed_results:
        return  # 无进度，不写 checkpoint

    ckpt = {
        "task_id": task_id,
        "goal": goal,
        "checkpoint_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "completed_tasks": {},
        "task_output_map": task_output_map,
        "remaining_task_ids": [t.id for t in remaining_tasks],
        "total_tasks": len(completed_tasks) + len(remaining_tasks),
    }

    for task, result in zip(completed_tasks, completed_results):
        ckpt["completed_tasks"][task.id] = {
            "success": result.success,
            "agent": result.agent or task.assigned_agent or "?",
            "output_preview": (result.output or "")[:500],
            "error": result.error,
            "elapsed_seconds": result.elapsed_seconds,
            "gear": task.gear,
            "intent": task.intent[:200],
        }

    path = _checkpoint_path(task_id)
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(ckpt, f, indent=2, ensure_ascii=False)
        logger.info(
            "[Checkpoint] 已保存 %s: %d/%d 完成",
            path, len(completed_tasks), len(completed_tasks) + len(remaining_tasks),
        )
    except (OSError, IOError) as e:
        logger.error("[Checkpoint] 保存失败 %s: %s", path, e)


def load_checkpoint(
    task_id: str,
    all_tasks: list[AgentTask],
) -> Optional[dict]:
    """检测并加载 checkpoint。

    Returns:
        {
            "completed": [(AgentTask, TaskResult), ...],
            "remaining": [AgentTask, ...],
            "task_output_map": {task_id: output_text}
        }
        如果 checkpoint 不存在或已过期，返回 None。
    """
    path = _checkpoint_path(task_id)
    if not os.path.exists(path):
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            ckpt = json.load(f)
    except (json.JSONDecodeError, OSError, IOError) as e:
        logger.warning("[Checkpoint] 读取失败 %s: %s，将从头执行", path, e)
        return None

    completed_ids = set(ckpt.get("completed_tasks", {}).keys())
    remaining_ids = set(ckpt.get("remaining_task_ids", []))

    if not completed_ids and not remaining_ids:
        logger.warning("[Checkpoint] 空 checkpoint，忽略")
        return None

    completed: list[tuple[AgentTask, TaskResult]] = []
    remaining: list[AgentTask] = []
    task_map = {t.id: t for t in all_tasks}

    for tid in completed_ids:
        task = task_map.get(tid)
        if task is None:
            logger.warning("[Checkpoint] checkpoint 中的任务 %s 不在当前计划中", tid)
            continue
        ckpt_entry = ckpt["completed_tasks"].get(tid, {})
        result = TaskResult(
            task_id=tid,
            success=ckpt_entry.get("success", False),
            output=ckpt_entry.get("output_preview", ""),
            error=ckpt_entry.get("error"),
            agent=ckpt_entry.get("agent"),
            gear=ckpt_entry.get("gear", 3),
            elapsed_seconds=ckpt_entry.get("elapsed_seconds", 0.0),
        )
        completed.append((task, result))

    for tid in remaining_ids:
        task = task_map.get(tid)
        if task:
            remaining.append(task)
        else:
            logger.warning("[Checkpoint] 剩余任务 %s 不在当前计划中", tid)

    # 如果 remaining 为空但还有未归类任务，补全
    if not remaining and remaining_ids:
        logger.warning(
            "[Checkpoint] 所有剩余任务 ID 均未匹配当前计划，从头执行"
        )
        return None

    task_output_map = ckpt.get("task_output_map", {})
    # 补全未在 task_output_map 中的已完成任务
    for tid in completed_ids:
        if tid not in task_output_map:
            ckpt_entry = ckpt["completed_tasks"].get(tid, {})
            task_output_map[tid] = ckpt_entry.get("output_preview", "")

    logger.info(
        "[Checkpoint] 恢复进度: %d 已完成, %d 待执行 (来源: %s)",
        len(completed), len(remaining), path,
    )
    return {
        "completed": completed,
        "remaining": remaining,
        "task_output_map": task_output_map,
    }


def clear_checkpoint(task_id: str) -> None:
    """清除 checkpoint 文件（任务全部完成后调用）"""
    path = _checkpoint_path(task_id)
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.info("[Checkpoint] 已清除 %s", path)
    except OSError as e:
        logger.warning("[Checkpoint] 清除失败 %s: %s", path, e)
