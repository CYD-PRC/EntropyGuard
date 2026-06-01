"""Debug: call decompose() directly within asyncio context (like run_with_decomposition does)"""
import asyncio, json, os, sys, time, logging

sys.path.insert(0, "/root/EntropyGuard")
os.chdir("/root/EntropyGuard")

logging.basicConfig(level=logging.DEBUG)
logging.getLogger("entropyruntime.orchestrator").setLevel(logging.DEBUG)

from orchestrator.orchestrator import MultiAgentOrchestrator

goal = "分析 /root/EntropyGuard/test-targets/ 下 Flask 测试应用的安全性，生成完整安全报告"

# Test 1: Direct decompose call (synchronous)
orch = MultiAgentOrchestrator()
print("=" * 60)
print("Test 1: decompose() — synchronous call")
print("=" * 60)
t0 = time.time()
tasks = orch.decompose(goal)
t1 = time.time()
print(f"  Time: {t1-t0:.1f}s")
print(f"  Tasks: {len(tasks)}")
for t in tasks:
    print(f"    {t.id:30s} deps=[{','.join(t.dependencies)}] agent={t.assigned_agent}")
print()

# Test 2: run_with_decomposition() — async call
print("=" * 60)
print("Test 2: run_with_decomposition() — async call")
print("=" * 60)

async def test_async():
    orch2 = MultiAgentOrchestrator()
    t2 = time.time()
    result = await orch2.run_with_decomposition(goal)
    t3 = time.time()
    print(f"  Time: {t3-t2:.1f}s")
    print(f"  Tasks: {len(result.tasks)}")
    for t in result.tasks:
        print(f"    {t.id:30s} deps={t.dependencies} agent={t.assigned_agent}")
    print(f"  Results: {len(result.results)}")
    for r in result.results:
        print(f"    {r.task_id:30s} success={r.success}")
    return result

asyncio.run(test_async())
