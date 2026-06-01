"""
Entropy Runtime · 自审计 Orchestrator 执行脚本
记录所有子任务的路由、耗时、结果，暴露 Orchestrator 自身缺陷
"""
import asyncio
import json
import logging
import os
import sys
import time
import traceback

# ── 硬编码日志 ──
sys.path.insert(0, "/root/EntropyGuard")
os.chdir("/root/EntropyGuard")

from orchestrator.orchestrator import MultiAgentOrchestrator
from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult

LOG_FILE = "/root/EntropyGuard/orchestrator/self-audit-exec.log"

# 配置日志：同时写入文件和控制台
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(name)s | %(levelname)s | %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, mode="w", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)

logger = logging.getLogger("self-audit")

# ============================================================
#  审计执行器
# ============================================================

class SelfAuditRunner:
    """包装 Orchestrator 的自审计执行器，记录每一层细节"""

    def __init__(self):
        self.orch = MultiAgentOrchestrator()
        self.global_start = time.time()
        self.decompose_log = None
        self.topology_log = None
        self.task_logs = []       # 每个子任务的完整记录
        self.merge_log = None
        self.orchestrator_defects = []  # Orchestrator 自身缺陷

    def record_defect(self, severity: str, category: str, detail: str, task_id: str = ""):
        self.orchestrator_defects.append({
            "severity": severity,
            "category": category,
            "detail": detail,
            "task_id": task_id,
        })
        logger.warning(f"[DEFECT][{severity}] {category}: {detail[:100]}...")

    async def run(self, goal: str):
        t_start = time.time()
        logger.info(f"=" * 60)
        logger.info(f"自审计开始: {goal}")
        logger.info(f"=" * 60)

        # ── Phase 1: Decompose ──
        logger.info(f"\n{'='*60}")
        logger.info(f"阶段1/5: 目标拆解 (decompose)")
        logger.info(f"{'='*60}")
        t1 = time.time()
        try:
            tasks = self.orch.decompose(goal)
            dt = time.time() - t1
            self.decompose_log = {
                "elapsed_seconds": round(dt, 2),
                "task_count": len(tasks),
                "tasks": [{"id": t.id, "desc": t.description, "deps": list(t.dependencies),
                           "agent": t.assigned_agent, "prio": t.priority}
                          for t in tasks],
            }
            logger.info(f"拆解完成: {len(tasks)}个子任务, 耗时{dt:.1f}s")
            for t in tasks:
                logger.info(f"  [{t.id}] agent={t.assigned_agent} deps={t.dependencies} prio={t.priority}")
        except Exception as e:
            dt = time.time() - t1
            self.decompose_log = {"elapsed_seconds": round(dt, 2), "error": str(e)}
            self.record_defect("P0", "decompose崩溃", f"decompose() 抛出异常: {e}")
            logger.error(f"decompose崩溃: {e}\n{traceback.format_exc()}")
            return None

        if not tasks:
            self.decompose_log["empty"] = True
            self.record_defect("P0", "decompose返回空", "decompose() 未生成任何子任务")
            return None

        # 检查任务ID是否重复
        ids = [t.id for t in tasks]
        if len(ids) != len(set(ids)):
            dupes = [i for i in ids if ids.count(i) > 1]
            self.record_defect("P1", "任务ID重复", f"重复的任务ID: {set(dupes)}")

        # ── Phase 2: Topological Sort ──
        logger.info(f"\n{'='*60}")
        logger.info(f"阶段2/5: 拓扑排序")
        logger.info(f"{'='*60}")
        t2 = time.time()
        try:
            sorted_tasks = self.orch._topological_sort(tasks)
            dt = time.time() - t2
            if sorted_tasks is None:
                self.topology_log = {"elapsed_seconds": round(dt, 2), "error": "循环依赖"}
                self.record_defect("P0", "拓扑排序失败", "子任务间存在循环依赖，无法执行")
                return None
            self.topology_log = {
                "elapsed_seconds": round(dt, 2),
                "sorted_ids": [t.id for t in sorted_tasks],
            }
            logger.info(f"排序完成: {' → '.join(t.id for t in sorted_tasks)}")
        except Exception as e:
            self.topology_log = {"error": str(e)}
            self.record_defect("P0", "拓扑排序崩溃", f"_topological_sort() 异常: {e}")
            return None

        # ── Phase 3: Execute (with context injection) ──
        logger.info(f"\n{'='*60}")
        logger.info(f"阶段3/5: 按序执行（带上下文传递）")
        logger.info(f"{'='*60}")

        results = []
        task_output_map = {}
        route_stats = {"hermes": 0, "pydanticai": 0, "autogpt": 0, "unknown": 0}

        for i, task in enumerate(sorted_tasks):
            logger.info(f"\n--- 子任务 {i+1}/{len(sorted_tasks)}: [{task.id}] ---")

            # 路由
            try:
                from orchestrator.rules import route
                assigned_agent = route(task)
            except Exception as e:
                assigned_agent = task.assigned_agent or "pydanticai"
                self.record_defect("P1", "路由失败", f"route() 异常: {e}, 使用降级agent={assigned_agent}", task.id)

            route_stats[assigned_agent if assigned_agent in route_stats else "unknown"] = \
                route_stats.get(assigned_agent if assigned_agent in route_stats else "unknown", 0) + 1

            # 上下文构建
            context = self.orch._build_context(task, task_output_map)
            context_injected = bool(context)
            if context_injected:
                logger.info(f"  注入 {len(context)} 字符上下文")
                original_intent = task.intent
                task.intent = f"[上下文信息]\n{context}\n\n[本次任务]\n{original_intent}"
                task.payload["original_intent"] = original_intent

            # 冲突检测
            try:
                conflicts = self.orch._detect_conflicts(task, results)
                if conflicts:
                    logger.info(f"  冲突检测: {conflicts}")
            except Exception as e:
                conflicts = []
                self.record_defect("P2", "冲突检测异常", f"_detect_conflicts() 异常: {e}", task.id)

            # 记录审计事件
            try:
                self.orch._log_audit(task, TaskResult(task_id=task.id))
            except Exception as e:
                self.record_defect("P2", "审计日志失败", f"_log_audit() 异常: {e}", task.id)

            # 执行
            t_sub = time.time()
            tr = None
            exec_error = None
            try:
                tr = self.orch.execute(task)
                tr.elapsed_seconds = round(time.time() - t_sub, 2)
                results.append(tr)
            except Exception as e:
                exec_error = str(e)
                tr = TaskResult(task_id=task.id, success=False, error=exec_error, agent=assigned_agent)
                tr.elapsed_seconds = round(time.time() - t_sub, 2)
                results.append(tr)
                self.record_defect("P0" if "timeout" in str(e).lower() else "P1",
                                   "执行异常", f"execute() 异常: {e}", task.id)

            # 记录任务日志
            log_entry = {
                "task_id": task.id,
                "index": i + 1,
                "assigned_agent": assigned_agent,
                "description": task.description,
                "dependencies": list(task.dependencies),
                "priority": task.priority,
                "context_injected": context_injected,
                "context_size": len(context) if context else 0,
                "elapsed_seconds": tr.elapsed_seconds if tr else 0,
                "success": tr.success if tr else False,
                "output_size": len(tr.output) if tr and tr.output else 0,
                "error": tr.error if tr and tr.error else None,
                "has_tool_calls": bool(tr.tool_calls) if tr else False,
                "validation_status": tr.validation_status if tr else None,
            }
            self.task_logs.append(log_entry)

            # 输出前100字符摘要
            status = "✅" if tr.success else "❌"
            snippet = (tr.output[:100] if tr.output else tr.error[:100]) if tr else "NULL"
            logger.info(f"  {status} [{task.id}] {assigned_agent} | {tr.elapsed_seconds}s | {snippet}...")

            # 注入上下文
            if tr.success and tr.output:
                task_output_map[task.id] = tr.output

        # ── Phase 4: Merge ──
        logger.info(f"\n{'='*60}")
        logger.info(f"阶段4/5: 结果合并")
        logger.info(f"{'='*60}")
        t4 = time.time()
        try:
            orchestrator_result = self.orch.merge(goal, sorted_tasks, results)
            orchestrator_result.total_time = round(time.time() - t_start, 2)
            orchestrator_result.conflict_resolved = []
            dt = time.time() - t4
            self.merge_log = {
                "elapsed_seconds": round(dt, 2),
                "summary": orchestrator_result.summary,
                "success": orchestrator_result.success,
            }
            logger.info(f"合并完成 -> 成功率 {sum(1 for r in results if r.success)}/{len(results)}")
        except Exception as e:
            self.merge_log = {"error": str(e)}
            self.record_defect("P0", "merge崩溃", f"merge() 异常: {e}")

        # ── Phase 5: 缺陷分析 ──
        logger.info(f"\n{'='*60}")
        logger.info(f"阶段5/5: Orchestrator 缺陷分析")
        logger.info(f"{'='*60}")
        self._analyze_defects(tasks, results, task_output_map)

        # 汇总
        total_elapsed = round(time.time() - t_start, 2)
        logger.info(f"\n{'='*60}")
        logger.info(f"自审计完成: 总耗时 {total_elapsed}s")
        logger.info(f"  子任务: {len(sorted_tasks)}")
        logger.info(f"  成功/总数: {sum(1 for r in results if r.success)}/{len(results)}")
        logger.info(f"  Orchestrator 缺陷: {len(self.orchestrator_defects)}")
        logger.info(f"  路由分布: {route_stats}")
        logger.info(f"{'='*60}")

        return {
            "goal": goal,
            "total_elapsed": total_elapsed,
            "decompose": self.decompose_log,
            "topology": self.topology_log,
            "tasks": self.task_logs,
            "merge": self.merge_log,
            "route_stats": route_stats,
            "defects": self.orchestrator_defects,
        }

    def _analyze_defects(self, tasks, results, task_output_map):
        """分析收集到的缺陷"""
        # 1. 上下文注入完整性
        for t in self.task_logs:
            if not t["context_injected"] and t["dependencies"]:
                self.record_defect("P2", "上下文丢失",
                                   f"任务 {t['task_id']} 声明依赖 {t['dependencies']} 但未注入上下文",
                                   t["task_id"])

        # 2. 超时检测
        for t in self.task_logs:
            if t["elapsed_seconds"] > 120:
                self.record_defect("P2", "任务超时",
                                   f"任务 {t['task_id']} 耗时 {t['elapsed_seconds']}s (>120s)",
                                   t["task_id"])

        # 3. 空输出检测
        for t in self.task_logs:
            if t["success"] and t["output_size"] == 0:
                self.record_defect("P2", "空输出",
                                   f"任务 {t['task_id']} 标注成功但输出为空",
                                   t["task_id"])

        # 4. 路由准确性
        from orchestrator.rules import route
        for task in tasks:
            try:
                actual_agent = route(task)
                if actual_agent != task.assigned_agent:
                    self.record_defect("P2", "路由不一致",
                                       f"任务 {task.id}: decompose 建议 {task.assigned_agent} 但 route() 返回 {actual_agent}",
                                       task.id)
            except Exception as e:
                pass

        # 5. 依赖图死节点
        all_ids = {t.id for t in tasks}
        executed_ids = {t["task_id"] for t in self.task_logs if t["success"]}
        for t in tasks:
            for dep in t.dependencies:
                if dep in executed_ids and dep not in task_output_map:
                    self.record_defect("P3", "依赖缺失",
                                       f"任务 {t.id} 依赖 {dep} 已执行但无输出注入",
                                       t.id)


# ============================================================
#  主入口
# ============================================================

async def main():
    goal = "对 /root/EntropyGuard/ 做一次完整安全审计，覆盖代码质量、依赖漏洞、配置安全、API 安全、敏感信息泄露"

    runner = SelfAuditRunner()
    result = await runner.run(goal)

    # 输出 JSON 日志
    if result:
        log_path = "/root/EntropyGuard/orchestrator/self-audit-result.json"
        with open(log_path, "w", encoding="utf-8") as f:
            # 转换为可序列化格式
            serializable = {
                "goal": result["goal"],
                "total_elapsed": result["total_elapsed"],
                "decompose": result["decompose"],
                "topology": result["topology"],
                "tasks": result["tasks"],
                "route_stats": result["route_stats"],
                "defects": result["defects"],
            }
            json.dump(serializable, f, ensure_ascii=False, indent=2)
        print(f"\nJSON 日志已保存: {log_path}")

    print(f"\nOrchestrator 缺陷总数: {len(runner.orchestrator_defects)}")
    for d in runner.orchestrator_defects:
        print(f"  [{d['severity']}] {d['category']}: {d['detail'][:120]}")

    return runner


if __name__ == "__main__":
    runner = asyncio.run(main())
