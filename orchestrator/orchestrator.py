"""Entropy Runtime · Multi-Agent Orchestrator
v5.0: 模块化重构 — decompose/execute/planner/memory/route/env 分离。

核心流程:
  1. decompose(goal)  — 目标拆解（三层降级 + 经验注入）
  2. route(task)      — 路由选择（历史成功率 + 技能记忆）
  3. execute(task)    — 统一执行（自动重试 + Agent 切换）
  4. merge(results)   — 结果合并 + 审计
"""
import asyncio
import logging
import time
from typing import Optional

from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult
from orchestrator.decompose import decompose
from orchestrator.execute import execute, merge, log_audit, detect_conflicts
from orchestrator.planner import topological_sort_levels, build_context, should_replan
from orchestrator.rules import route_with_history
from orchestrator.memory import (read_recent_episodes, manage_episode_lifecycle,
                                 write_skill_memory, find_skill_by_task_type)
from notify.wechat import send_orchestrator_complete

logger = logging.getLogger("entropyruntime.orchestrator")


class MultiAgentOrchestrator:
    """多 Agent 协同调度器 — 模块化门面类。"""

    def __init__(self):
        self._consecutive_failures = 0

    # ----------------------------------------------------------------
    #  🎯 主入口
    # ----------------------------------------------------------------

    async def run(self, goal: str) -> OrchestratorResult:
        """执行完整的 Orchestrator 流程（基础版）"""
        t_start = time.time()
        logger.info(f"[Orchestrator] 开始执行: {goal[:80]}...")
        tasks = decompose(goal)
        if not tasks:
            return OrchestratorResult(goal=goal, success=False,
                                      summary="目标拆解失败", total_time=round(time.time()-t_start, 2))

        results: list[TaskResult] = []
        conflict_log: list[str] = []
        for task in tasks:
            assigned_agent = route_with_history(task)
            logger.info(f"[Orchestrator] {task.id}: {task.intent[:40]}... → {assigned_agent} (gear={task.gear})")
            conflicts = detect_conflicts(task, results)
            if conflicts:
                conflict_log.extend(conflicts)
            t_sub = time.time()
            tr = execute(task)
            tr.elapsed_seconds = round(time.time() - t_sub, 2)
            results.append(tr)
            log_audit(task, tr)

        total_time = round(time.time() - t_start, 2)
        orchestrator_result = merge(goal, tasks, results)
        orchestrator_result.total_time = total_time
        orchestrator_result.conflict_resolved = conflict_log
        logger.info(f"[Orchestrator] 完成: {len(tasks)}子任务, 耗时{total_time}s, "
                    f"成功率{sum(1 for r in results if r.success)}/{len(results)}")
        return orchestrator_result

    async def run_with_decomposition(self, goal: str) -> OrchestratorResult:
        """增强版执行：环境感知 → 拆解 → 并行排序 → 分层执行 → 重规划 → 技能沉淀"""
        t_start = time.time()
        logger.info(f"[Orchestrator v5.0] 开始分解执行: {goal[:80]}...")
        self._consecutive_failures = 0
        self._current_goal = goal

        # 1. 拆解（环境感知 + 经验注入已在 decompose 模块内部完成）
        logger.info(f"[Orchestrator v5.0] 步骤1/4: 目标拆解")
        tasks = decompose(goal)
        if not tasks:
            return OrchestratorResult(goal=goal, success=False,
                                      summary="目标拆解失败", total_time=round(time.time()-t_start, 2))

        dep_info = {t.id: t.dependencies for t in tasks}
        logger.info(f"[Orchestrator v5.0] 拆解出 {len(tasks)} 个子任务")
        for t in tasks:
            logger.info(f"  {t.id}: {t.description[:60]} 依赖={t.dependencies or '(无)'}")

        # 2. 拓扑排序
        logger.info(f"[Orchestrator v5.0] 步骤2/4: 拓扑排序")
        levels = topological_sort_levels(tasks)
        if levels is None:
            return OrchestratorResult(goal=goal, tasks=tasks, success=False,
                                      summary="循环依赖", total_time=round(time.time()-t_start, 2))

        level_info = " → ".join(
            f"L{i}[{'|'.join(t.id for t in level)}]" for i, level in enumerate(levels))
        logger.info(f"[Orchestrator v5.0] 并行层级: {level_info}")

        # 3. 分层执行
        logger.info(f"[Orchestrator v5.0] 步骤3/4: 分层执行 ({len(levels)} 层)")
        results: list[TaskResult] = []
        conflict_log: list[str] = []
        task_output_map: dict[str, str] = {}
        loop = asyncio.get_event_loop()

        # 读取前序 episode 做上下文
        recent_episodes = read_recent_episodes(limit=10)
        episode_summary = ""
        if recent_episodes:
            lines = []
            for ep in recent_episodes:
                c = ep.get("content", {})
                ep_ok = "✅" if c.get("success") else "❌"
                ep_preview = ((c.get("output_preview", "") or "")[:80] or
                              (c.get("error", "") or "")[:80])
                lines.append(f"  [{ep_ok}] {c.get('task_id','?')} @ {c.get('agent','?')}: {ep_preview}")
            episode_summary = "\n".join(lines)

        all_tasks_flat = [t for level in levels for t in level]

        for level_idx, level in enumerate(levels):
            logger.info(f"[Orchestrator v5.0] 层级 {level_idx+1}/{len(levels)}: "
                        f"{' '.join(t.id for t in level)}")

            if self._consecutive_failures >= 2:
                logger.warning(f"[Orchestrator v5.0] 连续 {self._consecutive_failures} 次失败，提级 gear→4")
                for t in level:
                    t.gear = min(t.gear + 1, 4)

            async def run_single_task(t: AgentTask) -> TaskResult:
                context = build_context(t, task_output_map)
                combined = ""
                if episode_summary:
                    combined += f"[前序执行记录]\n{episode_summary}\n\n"
                if context:
                    combined += f"[前置任务输出]\n{context}"
                if combined:
                    t.intent = f"{combined}\n\n[本次任务]\n{t.intent}"
                    t.payload["original_intent"] = t.intent

                route_with_history(t)
                t_sub = time.time()
                tr = await loop.run_in_executor(None, execute, t)
                tr.elapsed_seconds = round(time.time() - t_sub, 2)
                return tr

            level_coros = [run_single_task(t) for t in level]
            level_results = await asyncio.gather(*level_coros, return_exceptions=True)

            for t, tr_or_err in zip(level, level_results):
                if isinstance(tr_or_err, Exception):
                    tr = TaskResult(task_id=t.id, success=False,
                                    error=f"执行异常: {tr_or_err}",
                                    agent=t.assigned_agent, gear=t.gear)
                else:
                    tr = tr_or_err
                results.append(tr)
                if tr.success and tr.output:
                    task_output_map[t.id] = tr.output
                log_audit(t, tr)

                if not tr.success:
                    self._consecutive_failures += 1
                else:
                    self._consecutive_failures = 0

                logger.info(f"[Orchestrator v5.0] {t.id} {'✅' if tr.success else '❌'} ({tr.elapsed_seconds}s)")

                # 动态重规划
                remaining = [lt for later_level in levels[level_idx + 1:] for lt in later_level]
                new_tasks = should_replan(t, tr, remaining, task_output_map, goal)
                if new_tasks is not None:
                    new_levels = topological_sort_levels(new_tasks)
                    if new_levels:
                        levels = levels[:level_idx + 1] + new_levels
                    break

        # 4. 合并结果
        total_time = round(time.time() - t_start, 2)
        logger.info(f"[Orchestrator v5.0] 步骤4/4: 合并结果")
        orchestrator_result = merge(goal, all_tasks_flat, results)
        orchestrator_result.total_time = total_time
        orchestrator_result.conflict_resolved = conflict_log

        logger.info(f"[Orchestrator v5.0] 完成: {len(all_tasks_flat)}子任务, "
                    f"耗时{total_time}s, 成功率{sum(1 for r in results if r.success)}/{len(results)}")

        # 微信通知
        passed = sum(1 for r in results if r.success)
        total = len(all_tasks_flat)
        send_orchestrator_complete(goal, orchestrator_result.success, total, total_time, f"{passed}/{total}")

        # [v5.0] 技能沉淀：成功任务的学习经验
        for task, tr in zip(all_tasks_flat, results):
            if tr.success and tr.agent:
                task_type = "security" if any(kw in task.intent.lower()
                                              for kw in ["security", "安全", "vuln", "漏洞"]) else "code"
                write_skill_memory(
                    task_type=task_type,
                    agent=tr.agent,
                    steps=[task.intent[:120]] if task.intent else [],
                    success_rate=1.0,
                    avg_duration=tr.elapsed_seconds or 0,
                    total_count=1,
                )

        # episode 生命周期管理
        manage_episode_lifecycle()

        return orchestrator_result


# ========== 全局单例 ==========
_orchestrator_instance: Optional[MultiAgentOrchestrator] = None


def get_orchestrator() -> MultiAgentOrchestrator:
    global _orchestrator_instance
    if _orchestrator_instance is None:
        _orchestrator_instance = MultiAgentOrchestrator()
    return _orchestrator_instance


async def run_orchestrator(goal: str) -> OrchestratorResult:
    return await get_orchestrator().run(goal)


async def run_orchestrator_with_decomposition(goal: str) -> OrchestratorResult:
    return await get_orchestrator().run_with_decomposition(goal)
