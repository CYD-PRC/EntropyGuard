"""
Entropy Runtime · 反馈循环引擎
v8.0: 规划 → 执行 → 评估 → 调整 → 再执行
通过 MessageBoard 实现 AutoGPT 规划层与 Hermes 执行层的状态同步。

核心数据流:
  iterate(goal)
    ├─ Step 1: AutoGPT analyze_goal → GoalAnalysis
    ├─ Step 2: AutoGPT decompose → TaskGraph
    ├─ Step 3: AutoGPT route → RouteTable
    ├─ Step 4: AutoGPT generate_plan → ExecutionPlan
    ├─ Step 5: Hermes receive_plan → verify + execute
    │   ├─ 每步 metacognition.self_check
    │   └─ 失败时 replanner 重规划
    ├─ Step 6: Hermes verify_results → 安全三道校验
    ├─ Step 7: AutoGPT evaluate_result → 评估是否达标
    ├─ Step 8: 不达标 → adjust_strategy → 回到 Step 5
    └─ Step 9: 达标 → 记录 memory_store + 报告
"""
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult
from orchestrator.planner_gateway import (
    PlannerGateway, GoalAnalysis, TaskGraph, RouteTable,
    ExecutionPlan, ToolAssignment, ToolType,
)
from orchestrator.checkpoint import clear_checkpoint
from orchestrator.memory_store import MemoryStore

logger = logging.getLogger("entropyruntime.feedback_loop")


# ========== 数据模型 ==========

@dataclass
class EvalResult:
    """执行结果评估"""
    passed: bool
    score: float               # 0.0 ~ 1.0
    gap_analysis: str = ""     # 差距分析
    failed_criteria: list[str] = field(default_factory=list)
    passed_criteria: list[str] = field(default_factory=list)
    suggestion: str = "continue"  # continue / replan / escalate
    timestamp: float = field(default_factory=time.time)


@dataclass
class AdjustedPlan:
    """调整后的计划"""
    original_plan: ExecutionPlan
    changes: list[str] = field(default_factory=list)
    retry_count: int = 0
    escalated: bool = False
    reason: str = ""


@dataclass
class CycleResult:
    """一轮迭代结果"""
    iteration: int
    plan: ExecutionPlan
    hermes_result: Optional[OrchestratorResult] = None
    eval_result: Optional[EvalResult] = None
    adjusted: Optional[AdjustedPlan] = None
    success: bool = False
    error: str = ""
    elapsed: float = 0.0


@dataclass
class FinalResult:
    """完整反馈循环的最终结果"""
    goal: str
    iterations: list[CycleResult] = field(default_factory=list)
    total_iterations: int = 0
    final_success: bool = False
    total_elapsed: float = 0.0
    summary: str = ""


class FeedbackLoop:
    """
    反馈循环引擎 — 协调 AutoGPT (规划) 与 Hermes (执行) 的完整闭环。

                                          ┌──────────────┐
        用户输入 →  AutoGPT 规划           │ Hermes 执行  │
                    analyze_goal()         │ receive_plan │
                    decompose_task()  ──►  │ execute_task │
                    route_plan()           │ verify_result│
                    gen_exec_plan()        │ report_result│
                                          └──────┬───────┘
                                                 │
                    ◄───── 评估结果 ──────────────┘
                    ◄───── 不达标 → adjust_strategy ─ 最多 max_iterations 次
    """

    def __init__(self, max_iterations: int = 5):
        self.planner = PlannerGateway()
        self.max_iterations = max_iterations
        self.memory_store = MemoryStore()

    # ===== Step 1-4: AutoGPT 规划阶段 =====

    def _plan(self, goal: str) -> tuple[GoalAnalysis, TaskGraph, RouteTable, ExecutionPlan]:
        """全流程规划。"""
        analysis = self.planner.analyze_goal(goal)
        graph = self.planner.decompose_task(analysis)
        route = self.planner.route_plan(graph)
        plan = self.planner.generate_execution_plan(route, goal)
        return analysis, graph, route, plan

    # ===== Step 5-6: Hermes 执行阶段 =====

    def _execute(self, plan: ExecutionPlan) -> OrchestratorResult:
        """把计划交给 Hermes 执行。"""
        from orchestrator.execute import execute_plan

        subtasks = self._plan_to_subtasks(plan)
        result = execute_plan(
            subtasks=subtasks,
            task_id=plan.plan_id,
            goal=plan.goal,
            gear=3,
        )
        return result

    def _plan_to_subtasks(self, plan: ExecutionPlan) -> list[AgentTask]:
        """将 ExecutionPlan 转换为 AgentTask 列表供 Hermes 执行。"""
        tasks = []
        for ta in plan.route_table.assignments:
            task = AgentTask(
                id=ta.task_id,
                intent=ta.intent,
                dependencies=ta.dependencies,
                priority=ta.priority,
                description=ta.intent[:80],
                assigned_agent="hermes",
                gear=ta.tool_params.get("gear", 3),
                payload={
                    "tool": ta.tool.value,
                    "tool_params": ta.tool_params,
                    "fallback_tool": ta.fallback_tool.value if ta.fallback_tool else None,
                    "acceptance_criteria": ta.acceptance_criteria,
                },
            )
            tasks.append(task)
        return tasks

    # ===== Step 7: 结果评估 =====

    def _evaluate(self, result: OrchestratorResult, plan: ExecutionPlan) -> EvalResult:
        """AutoGPT 评估执行结果是否达标。"""
        passed_criteria = []
        failed_criteria = []

        # 标准 1: 全部子任务成功
        if result.success:
            passed_criteria.append("所有子任务执行成功")
        else:
            failed_criteria.append(f"部分子任务失败: {sum(1 for r in result.results if not r.success)}/{len(result.results)}")

        # 标准 2: 无错误
        errors = [r.error for r in result.results if r.error]
        if not errors:
            passed_criteria.append("无执行错误")
        else:
            failed_criteria.append(f"存在 {len(errors)} 个错误")

        # 标准 3: 有输出
        non_empty = sum(1 for r in result.results if r.output and len(r.output.strip()) > 0)
        if non_empty >= len(result.results) * 0.5:
            passed_criteria.append("半数以上子任务有输出内容")
        else:
            failed_criteria.append("子任务输出不足")

        # 计算综合评分
        total = len(passed_criteria) + len(failed_criteria) or 1
        score = len(passed_criteria) / total

        # 判断
        passed = score >= 0.7 and result.success

        suggestion = "continue"
        if passed:
            suggestion = "complete"
        elif score < 0.3:
            suggestion = "escalate"
        elif result.success:
            suggestion = "continue"
        else:
            suggestion = "replan"

        return EvalResult(
            passed=passed,
            score=round(score, 2),
            gap_analysis=f"通过 {len(passed_criteria)}/{total} 项标准",
            passed_criteria=passed_criteria,
            failed_criteria=failed_criteria,
            suggestion=suggestion,
        )

    # ===== Step 8: 策略调整 =====

    def _adjust(self, plan: ExecutionPlan, eval_result: EvalResult,
                cycle: int) -> AdjustedPlan:
        """根据评估结果调整计划。"""
        changes = []
        et = eval_result

        if et.suggestion == "complete":
            return AdjustedPlan(original_plan=plan, changes=["达标，无需调整"],
                                reason="结果达标")

        if et.suggestion == "escalate":
            return AdjustedPlan(
                original_plan=plan, changes=["需要人工介入"],
                escalated=True, reason=f"评分 {et.score} 过低，需人工介入",
            )

        # replan: 修改 intent 重试
        for assign in plan.route_table.assignments:
            assign.intent = (
                f"{assign.intent}\n"
                f"[重规划提示] 之前失败标准: {', '.join(et.failed_criteria[:2])}"
            )

        changes.append(f"第 {cycle} 轮评估未达标 (score={et.score})，注入失败提示重试")
        changes.extend(et.failed_criteria[:3])

        return AdjustedPlan(
            original_plan=plan, changes=changes,
            retry_count=cycle, reason=f"第 {cycle} 轮重试",
        )

    # ===== 主循环 =====

    def iterate(self, goal: str, max_iterations: Optional[int] = None) -> FinalResult:
        """完整的反馈循环：规划 → 执行 → 评估 → 调整 → 再执行。"""
        t_start = time.time()
        max_iters = max_iterations or self.max_iterations

        logger.info("[FeedbackLoop] === 开始反馈循环: %s ===", goal[:60])

        # Step 1-4: 首次规划
        analysis, graph, route, plan = self._plan(goal)
        logger.info("[FeedbackLoop] 规划完成: %s级, %d 子任务",
                     analysis.cosmic_tier, len(graph.nodes))

        iterations = []
        final_success = False

        for i in range(max_iters):
            t_cycle = time.time()
            logger.info("[FeedbackLoop] === 迭代 #%d/%d ===", i + 1, max_iters)

            # Step 5: Hermes 执行
            hermes_result = self._execute(plan)

            # Step 6: 安全校验 (由 execute.py 内部完成)
            # Step 7: AutoGPT 评估
            eval_result = self._evaluate(hermes_result, plan)

            cycle_result = CycleResult(
                iteration=i + 1,
                plan=plan,
                hermes_result=hermes_result,
                eval_result=eval_result,
                success=hermes_result.success,
                elapsed=round(time.time() - t_cycle, 2),
            )

            logger.info(
                "[FeedbackLoop] 迭代 #%d: success=%s, eval_score=%.2f, suggestion=%s",
                i + 1, hermes_result.success, eval_result.score, eval_result.suggestion,
            )

            # Step 8: 达标判断
            if eval_result.suggestion == "complete" or eval_result.passed:
                final_success = True
                logger.info("[FeedbackLoop] 目标达标，结束循环")
                cycle_result.adjusted = self._adjust(plan, eval_result, i + 1)
                iterations.append(cycle_result)
                break

            if eval_result.suggestion == "escalate":
                logger.warning("[FeedbackLoop] 需要人工介入")
                cycle_result.adjusted = self._adjust(plan, eval_result, i + 1)
                iterations.append(cycle_result)
                break

            # 不达标：调整后继续
            adjusted = self._adjust(plan, eval_result, i + 1)
            if adjusted.escalated:
                iterations.append(cycle_result)
                break

            plan = adjusted.original_plan
            cycle_result.adjusted = adjusted
            iterations.append(cycle_result)

            logger.info("[FeedbackLoop] 计划已调整: %s", adjusted.reason)

        else:
            logger.warning("[FeedbackLoop] 达到最大迭代次数 %d", max_iters)

        # Step 9: 记录
        total_elapsed = round(time.time() - t_start, 2)

        summary_lines = [
            f"反馈循环完成: {'✅ 达标' if final_success else '❌ 未达标'}",
            f"总耗时: {total_elapsed}s",
            f"迭代次数: {len(iterations)}/{max_iters}",
        ]
        if iterations:
            last = iterations[-1]
            if last.hermes_result:
                summary_lines.append(last.hermes_result.summary[:200])

        # 写入 memory_store
        try:
            self.memory_store.save_episode(
                task_id=f"feedback-{int(time.time())}",
                agent="orchestrator:feedback_loop",
                intent=goal,
                output=f"iters={len(iterations)}, success={final_success}, elapsed={total_elapsed}",
                success=final_success,
                duration=total_elapsed,
            )
        except Exception as e:
            logger.warning("[FeedbackLoop] memory 写入失败: %s", e)

        return FinalResult(
            goal=goal,
            iterations=iterations,
            total_iterations=len(iterations),
            final_success=final_success,
            total_elapsed=total_elapsed,
            summary="\n".join(summary_lines),
        )
