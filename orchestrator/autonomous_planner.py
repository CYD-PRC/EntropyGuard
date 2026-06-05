"""Entropy Runtime · 自主任务生成与执行
Phase 5 Module 2: 目标拆解 + 自主循环 + 调度模式。

三种模式:
  AUTO    — 系统自主执行
  MANUAL  — 等待用户指令（当前默认）
  HYBRID  — 系统建议 + 人工确认
"""
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from orchestrator.task_model import AgentTask
from orchestrator.goal_engine import GoalEngine, Goal, ValueTier

logger = logging.getLogger("entropyruntime.autonomous_planner")


class Mode(Enum):
    AUTO = "auto"
    MANUAL = "manual"
    HYBRID = "hybrid"


@dataclass
class AutonomousStatus:
    """自主执行状态"""
    mode: Mode = Mode.MANUAL
    last_cycle: float = 0.0
    goals_pending: int = 0
    goals_completed: int = 0
    tasks_executed: int = 0
    tasks_failed: int = 0
    last_error: str = ""
    cycle_count: int = 0


class AutonomousPlanner:
    """自主任务生成与执行引擎。"""

    def __init__(self):
        self.goal_engine = GoalEngine()
        self.mode = Mode.MANUAL
        self.status = AutonomousStatus()
        self._cycle_history: list[dict] = []

    def goal_to_tasks(self, goal: Goal) -> list[AgentTask]:
        """把一个 Goal 拆解为可执行的子任务序列。"""
        try:
            from orchestrator.decompose import decompose
            tasks = decompose(goal.intent)
            if tasks:
                logger.info(
                    "[AutoPlanner] goal→tasks: %s → %d 个子任务",
                    goal.id, len(tasks),
                )
                return tasks
        except Exception as e:
            logger.warning("[AutoPlanner] decompose 失败: %s", e)

        # 兜底：单任务模式
        return [AgentTask(
            id=f"auto-{goal.id}",
            description=goal.description,
            intent=goal.intent,
            gear=3 if goal.priority.value <= 2 else 2,
        )]

    def run_autonomous_cycle(self) -> dict:
        """运行一次完整的自主循环。"""
        self.status.cycle_count += 1
        t0 = time.time()
        logger.info("[AutoPlanner] === 自主循环 #%d ===", self.status.cycle_count)

        cycle_result = {
            "cycle": self.status.cycle_count,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "goals": [], "tasks": [], "errors": [],
        }

        # Step 1: 生成目标
        state = self.goal_engine.scan_environment()
        goals = self.goal_engine.derive_goals(state)
        goals = self.goal_engine.prioritize_goals(goals)

        if not goals:
            logger.info("[AutoPlanner] 无目标，跳过执行")
            cycle_result["status"] = "idle"
            return cycle_result

        # Step 2: 取最高优先级目标（跳过表达级）
        active_goals = [g for g in goals if g.priority != ValueTier.EXPRESSION]
        if not active_goals:
            logger.info("[AutoPlanner] 仅表达级目标，跳过执行")
            cycle_result["status"] = "idle_noop"
            return cycle_result

        target_goal = active_goals[0]
        cycle_result["goals"] = [{
            "id": target_goal.id, "description": target_goal.description,
            "priority": target_goal.priority.name,
        }]

        # Step 3: 拆解为子任务
        tasks = self.goal_to_tasks(target_goal)
        cycle_result["tasks"] = [{"id": t.id, "intent": t.intent[:50]} for t in tasks]

        # Step 4: 宇宙透镜评估
        try:
            for task in tasks:
                pr = self.goal_engine._lense.evaluate(task)
                task.payload["cosmic_lense"] = {
                    "tier": pr.tier_name_cn, "priority_score": pr.priority_score,
                }
        except Exception as e:
            logger.warning("[AutoPlanner] cosmic_lense 评估失败: %s", e)

        # Step 5: 执行
        try:
            from orchestrator.execute import execute_plan
            result = execute_plan(
                subtasks=tasks,
                task_id=f"auto-{target_goal.id}",
                goal=target_goal.intent,
            )
            self.status.tasks_executed += len(tasks)
            if result.success:
                self.status.goals_completed += 1
                target_goal.completed = True
            else:
                self.status.tasks_failed += 1

            # Step 6: 记录到 memory
            try:
                from orchestrator.memory_store import MemoryStore
                store = MemoryStore()
                store.save_episode(
                    task_id=f"auto-cycle-{self.status.cycle_count}",
                    agent="orchestrator:autonomous",
                    intent=target_goal.intent,
                    output=result.summary[:500],
                    success=result.success,
                    duration=result.total_time,
                    metadata={"cycle": self.status.cycle_count, "goal": target_goal.id},
                )
                store.close()
            except Exception as e:
                logger.warning("[AutoPlanner] memory 记录失败: %s", e)

            cycle_result["result"] = {
                "success": result.success,
                "summary": result.summary[:200],
                "total_time": result.total_time,
            }

            # Step 7: 失败时重规划
            if not result.success:
                try:
                    from orchestrator.replanner import Replanner
                    rp = Replanner()
                    plan = rp.replan_on_failure(
                        failed_task=tasks[0] if tasks else None,
                        result=result.results[0] if result.results else None,
                        remaining_tasks=tasks[1:] if len(tasks) > 1 else [],
                        task_output_map={},
                        all_tasks=tasks,
                    )
                    rp.save_replan_to_memory(plan, target_goal.id)
                    cycle_result["replan"] = {
                        "action": plan.action.value,
                        "reason": plan.reason,
                    }
                except Exception as e:
                    logger.warning("[AutoPlanner] 重规划失败: %s", e)

        except Exception as e:
            logger.error("[AutoPlanner] 执行异常: %s", e)
            cycle_result["errors"].append(str(e))
            self.status.last_error = str(e)

        self.status.last_cycle = time.time()
        self._cycle_history.append(cycle_result)
        logger.info(
            "[AutoPlanner] 循环 #%d 完成: %d goals, %d tasks, %.1fs",
            self.status.cycle_count, len(cycle_result["goals"]),
            len(cycle_result["tasks"]), time.time() - t0,
        )
        return cycle_result

    def set_mode(self, mode: Mode):
        self.mode = mode
        logger.info("[AutoPlanner] 模式切换: %s", mode.value)

    def get_status(self) -> dict:
        return {
            "mode": self.mode.value,
            "cycle_count": self.status.cycle_count,
            "goals_completed": self.status.goals_completed,
            "tasks_executed": self.status.tasks_executed,
            "tasks_failed": self.status.tasks_failed,
            "last_cycle": time.strftime(
                "%Y-%m-%dT%H:%M:%SZ", time.gmtime(self.status.last_cycle)
            ) if self.status.last_cycle else "",
            "last_error": self.status.last_error,
            "recent_cycles": self._cycle_history[-5:],
        }
