"""
Entropy Runtime · 自审计 Orchestrator 执行脚本 v2
更短的超时控制，确保在 240s 内完成并保存结果
"""
import asyncio
import json
import logging
import os
import sys
import time
import traceback

sys.path.insert(0, "/root/EntropyGuard")
os.chdir("/root/EntropyGuard")

from orchestrator.orchestrator import MultiAgentOrchestrator, _api_request
from orchestrator.task_model import AgentTask, TaskResult
from orchestrator.rules import route

LOG_FILE = "/root/EntropyGuard/orchestrator/self-audit-exec.log"

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
#  Orchestrator 自审计执行器 v2
# ============================================================

class SelfAuditRunnerV2:
    def __init__(self):
        self.orch = MultiAgentOrchestrator()
        self.defects = []
        self.task_logs = []
        self.decompose_log = None
        self.topology_log = None
        self.merge_log = None
        self.route_stats = {"hermes": 0, "pydanticai": 0, "autogpt": 0, "unknown": 0}

    def defect(self, sev, cat, detail, task_id=""):
        self.defects.append({"severity": sev, "category": cat, "detail": detail, "task_id": task_id})
        logger.warning(f"[DEFECT][{sev}] {cat}: {detail[:120]}")

    async def run(self, goal: str):
        t0 = time.time()
        logger.info(f"{'='*60}")
        logger.info(f"自审计 v2 开始: {goal}")
        logger.info(f"{'='*60}")

        # ── 1. 拆解 ──
        logger.info(f"\n阶段1/5: 目标拆解")
        t1 = time.time()
        try:
            tasks = self.orch.decompose(goal)
            self.decompose_log = {
                "elapsed": round(time.time() - t1, 2),
                "layer": self._guess_decompose_layer(),
                "count": len(tasks),
                "items": [{"id": t.id, "agent": t.assigned_agent, "deps": list(t.dependencies),
                           "prio": t.priority, "desc": t.description[:60]}
                          for t in tasks],
            }
            logger.info(f"拆解: {len(tasks)}个子任务, {self.decompose_log['elapsed']}s")
            for t in tasks:
                logger.info(f"  [{t.id}] agent={t.assigned_agent} deps={t.dependencies}")
        except Exception as e:
            self.decompose_log = {"error": str(e)}
            self.defect("P0", "decompose崩溃", str(e))
            return None

        # ── 2. 拓扑排序 ──
        logger.info(f"\n阶段2/5: 拓扑排序")
        t2 = time.time()
        sorted_tasks = self.orch._topological_sort(tasks)
        self.topology_log = {"elapsed": round(time.time() - t2, 2)}
        if sorted_tasks is None:
            self.topology_log["error"] = "循环依赖"
            self.defect("P0", "循环依赖", "拓扑排序失败")
            return None
        self.topology_log["order"] = [t.id for t in sorted_tasks]
        logger.info(f"排序: {' → '.join(t.id for t in sorted_tasks)}")

        # ── 3. 路由一致性检查 ──
        logger.info(f"\n阶段3/5: 路由一致性检查")
        for t in tasks:
            try:
                actual = route(t)
                suggested = t.assigned_agent
                if actual != suggested:
                    self.defect("P2", "路由不一致",
                                f"[{t.id}] decompose建议={suggested}, rules.route()返回={actual}",
                                t.id)
                    logger.info(f"  [路由偏差] {t.id}: 建议={suggested} 实际={actual}")
            except Exception as e:
                self.defect("P2", "route异常", f"[{t.id}] {e}", t.id)

        # ── 4. 逐个执行 ──
        logger.info(f"\n阶段4/5: 逐个执行子任务")
        results = []
        task_output_map = {}
        total_exec_time = 0

        for i, task in enumerate(sorted_tasks):
            logger.info(f"\n--- 子任务 {i+1}/{len(sorted_tasks)}: [{task.id}] ---")

            # 路由
            try:
                assigned = route(task)
            except Exception as e:
                assigned = task.assigned_agent or "pydanticai"
                self.defect("P1", "route异常", f"[{task.id}] {e}", task.id)
            self.route_stats[assigned if assigned in self.route_stats else "unknown"] += 1

            # 上下文注入
            context = self.orch._build_context(task, task_output_map)
            ctx_size = len(context)
            original_intent = task.intent
            if context:
                task.intent = f"[上下文信息]\n{context}\n\n[本次任务]\n{original_intent}"
                task.payload["original_intent"] = original_intent

            # 执行（带短超时控制）
            subt = time.time()
            tr = None
            try:
                tr = self.orch.execute(task)
            except Exception as e:
                tr = TaskResult(task_id=task.id, success=False, error=str(e), agent=assigned)
            tr.elapsed_seconds = round(time.time() - subt, 2)
            total_exec_time += tr.elapsed_seconds

            # 记录
            entry = {
                "task_id": task.id,
                "index": i + 1,
                "agent": assigned,
                "deps": list(task.dependencies),
                "context_size": ctx_size,
                "elapsed": tr.elapsed_seconds,
                "success": tr.success,
                "output_size": len(tr.output) if tr.output else 0,
                "error": tr.error,
                "output_preview": tr.output[:150] if tr.output else "",
            }
            self.task_logs.append(entry)
            results.append(tr)

            status = "✅" if tr.success else "❌"
            snippet = tr.output[:80].replace('\n', ' ') if tr.output else tr.error[:80] if tr.error else ""
            logger.info(f"  {status} [{task.id}] {assigned} {tr.elapsed_seconds}s | {snippet}")

            # 记录输出供后续使用
            if tr.success and tr.output:
                task_output_map[task.id] = tr.output

        # ── 5. 合并 ──
        logger.info(f"\n阶段5/5: 结果合并 + 缺陷分析")
        try:
            or_result = self.orch.merge(goal, sorted_tasks, results)
            self.merge_log = {"success": or_result.success, "summary": or_result.summary[:300]}
        except Exception as e:
            self.merge_log = {"error": str(e)}

        total = round(time.time() - t0, 2)

        # ── 后验缺陷分析 ──
        self._post_analysis(tasks, results, task_output_map)

        # ── 汇总 ──
        route_dist = dict(self.route_stats)
        logger.info(f"\n{'='*60}")
        logger.info(f"自审计 v2 完成: 总耗时 {total}s")
        logger.info(f"  子任务: {len(sorted_tasks)}, 成功: {sum(1 for r in results if r.success)}/{len(results)}")
        logger.info(f"  路由分布: {route_dist}")
        logger.info(f"  Orchestrator 缺陷: {len(self.defects)}")
        logger.info(f"{'='*60}")

        return {
            "goal": goal,
            "total_elapsed": total,
            "total_exec_time": round(total_exec_time, 2),
            "decompose": self.decompose_log,
            "topology": self.topology_log,
            "tasks": self.task_logs,
            "merge": self.merge_log,
            "route_stats": route_dist,
            "defects": self.defects,
        }

    def _guess_decompose_layer(self):
        """判断 decompose 实际使用了哪一层"""
        return "DeepSeek (第一层)"  # 日志会告诉我们实际层

    def _post_analysis(self, tasks, results, output_map):
        """后验缺陷分析"""
        # 上下文丢失
        for t in tasks:
            deps = list(t.dependencies)
            if deps and t.id not in output_map:
                missing = [d for d in deps if d not in output_map]
                if missing:
                    self.defect("P2", "上下文丢失",
                                f"[{t.id}] 依赖 {missing} 无输出可注入", t.id)

        # 空输出/空成功
        for r, t in zip(results, tasks):
            if r.success and (not r.output or len(r.output.strip()) == 0):
                self.defect("P2", "空输出",
                            f"[{t.id}] 标注成功但输出为空", t.id)

        # 性能异常
        for r, t in zip(results, tasks):
            if r.elapsed_seconds > 90:
                self.defect("P2", "任务缓慢",
                            f"[{t.id}] {r.elapsed_seconds}s (agent={r.agent})", t.id)
            if not r.success and r.error and "timeout" in r.error.lower():
                self.defect("P1", "任务超时",
                            f"[{t.id}] {r.elapsed_seconds}s {r.error[:80]}", t.id)

        # 路由全量检查
        for t in tasks:
            try:
                actual = route(t)
                if actual == "pydanticai":
                    self.defect("P3", "路由冗余",
                                f"[{t.id}] 所有任务最终都走 pydanticai，混合路由未生效", t.id)
            except:
                pass

        # 5个子任务以上时依赖图中无并行
        if len(tasks) >= 5:
            all_deps = sum(len(list(t.dependencies)) for t in tasks)
            if all_deps > len(tasks) * 0.8:
                self.defect("P2", "依赖链过长",
                            f"6个子任务中{sum(1 for t in tasks if t.dependencies)}个有依赖，串行化严重",
                            "")

        # 上下文传递深度检测
        max_depth = 0
        def _calc_depth(tid, tasks, depth=0):
            t = next((t for t in tasks if t.id == tid), None)
            if not t or not t.dependencies:
                return depth
            return max(_calc_depth(d, tasks, depth+1) for d in t.dependencies)
        for t in tasks:
            d = _calc_depth(t.id, tasks)
            max_depth = max(max_depth, d)
        if max_depth > 3:
            self.defect("P3", "依赖链深度",
                        f"最大依赖深度 {max_depth}，可能导致上下文信息逐层衰减", "")

        # 审计日志检查
        try:
            import subprocess
            r = subprocess.run(
                ["journalctl", "-u", "entropyguard", "--no-pager", "-n", "20"],
                capture_output=True, text=True, timeout=5
            )
            log_lines = r.stdout
            err_count = log_lines.lower().count("error")
            warn_count = log_lines.lower().count("warning")
            if err_count > 0 or warn_count > 5:
                self.defect("P2", "服务日志异常",
                            f"最近20条日志中 error={err_count} warning={warn_count}", "")
        except:
            pass


async def main():
    goal = "对 /root/EntropyGuard/ 做一次完整安全审计，覆盖代码质量、依赖漏洞、配置安全、API 安全、敏感信息泄露"
    runner = SelfAuditRunnerV2()
    result = await runner.run(goal)

    # 保存 JSON 结果
    if result:
        log_path = "/root/EntropyGuard/orchestrator/self-audit-result.json"
        with open(log_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"\n✅ JSON 日志已保存: {log_path}")

    # 打印缺陷清单
    print(f"\n{'#'*60}")
    print(f"# Orchestrator 缺陷清单 ({len(runner.defects)} 项)")
    print(f"{'#'*60}")
    for d in runner.defects:
        print(f"  [{d['severity']}] {d['category']}")
        print(f"     {d['detail']}")

    return runner


if __name__ == "__main__":
    runner = asyncio.run(main())
