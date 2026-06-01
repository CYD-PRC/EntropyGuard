"""
Entropy Runtime · Redteam Evolver CLI
======================================
红队测试进化系统命令行入口。

[v3] 新增:
  --target NAME[=version]    目标软件名称（如 flask, django）
  --context DESC             目标描述
  --focus AREAS              重点关注领域（逗号分隔）
  --target-url URL           目标 URL（启用 fitness 评估）
  --load-seeds               加载种子库（默认开启）

用法:
  python3 security/run_redteam.py --dry-run
  python3 security/run_redteam.py --target flask=1.0 --context "Werkzeug 0.14.1 debug" --focus web,ssrf
  python3 security/run_redteam.py --target-url http://127.0.0.1:5000 --focus debug,path_traversal
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, "/root/EntropyGuard")

# security.py 文件与 security/ 目录冲突，用 spec_from_file_location 直接加载
import importlib.util
_evolver_spec = importlib.util.spec_from_file_location(
    "redteam_evolver",
    "/root/EntropyGuard/security/redteam_evolver.py"
)
_evolver_mod = importlib.util.module_from_spec(_evolver_spec)
_evolver_spec.loader.exec_module(_evolver_mod)
RedteamEvolver = _evolver_mod.RedteamEvolver
run_evolution = _evolver_mod.run_evolution


def print_report(report: dict):
    """格式化输出进化报告"""
    print()
    print("=" * 60)
    print(f"  ENTROPY RUNTIME · 红队进化报告 — 第 {report['round']} 轮")
    print(f"  {report['timestamp']}  |  耗时 {report['elapsed_seconds']}s")
    print("=" * 60)
    print()
    print(f"  测试套件: {report['suite_before']} → {report['suite_after']} 条")
    print(f"  待审批:   {report['pending_count']} 条")
    print()
    print("  ┌───────────┬──────────┬──────────┬──────────┐")
    print(f"  │ 运行       │ 通过      │ 失败      │ 通过率    │")
    print("  ├───────────┼──────────┼──────────┼──────────┤")
    print(f"  │ {report['tests_run']:<9} │ {report['passed']:<8} │ {report['failed']:<8} │ {report['pass_rate']:<7}% │")
    print("  └───────────┴──────────┴──────────┴──────────┘")
    print()

    if report.get("target_context"):
        ctx = report["target_context"]
        print(f"  🎯 目标: {ctx.get('name','?')} {ctx.get('version','')}")
        print()

    if report.get("seeds_loaded", 0) > 0:
        print(f"  🌱 种子库加载: {report['seeds_loaded']} 条")
        print()

    if report.get("fitness_avg", 0) > 0:
        print(f"  📊 Fitness 均值: {report['fitness_avg']:.2f}")
        print()

    if report["failed_cases"]:
        print("  ❌ 失败用例:")
        for name in report["failed_cases"]:
            print(f"     - {name}")
        print()

    if report["weak_layers"]:
        print(f"  ⚠️  薄弱安全层: {', '.join(report['weak_layers'])}")
        print()

    if report.get("scariest"):
        print(f"  🔬 系统最怕: {report['scariest']}")
        print()

    if report.get("failure_mode"):
        print("  📊 绕过模式分类:")
        for mode, info in report["failure_mode"].items():
            print(f"     - {mode}: {info['count']}次")
        print()

    if report.get("mutations_generated", 0) > 0:
        print(f"  🧬 变异引擎: 生成 {report['mutations_generated']} 条变种")
        for mid in report.get("mutations_added", []):
            print(f"     - {mid}")
        print()

    if report["added"]:
        print(f"  ✅ 自动加入测试套件 ({len(report['added'])} 条):")
        for cid in report["added"]:
            print(f"     - {cid}")
        print()

    if report["pending_approval"]:
        print(f"  🕐 待审批 (写入 pending_tests.json) ({len(report['pending_approval'])} 条):")
        for cid in report["pending_approval"]:
            print(f"     - {cid}")
        print()

    if report["skipped_duplicates"]:
        print(f"  ⏭️  因重复跳过 ({len(report['skipped_duplicates'])} 条):")
        for s in report["skipped_duplicates"]:
            print(f"     - {s.get('name','?')}: {s.get('reason','')}")
        print()

    print("=" * 60)
    print(f"  🔗 套件:  {Path('/root/EntropyGuard/security/redteam_suite.json').resolve()}")
    print(f"  🔗 待审:  {Path('/root/EntropyGuard/security/pending_tests.json').resolve()}")
    print(f"  🔗 历史:  {Path('/root/EntropyGuard/security/evolution_history.json').resolve()}")
    print("=" * 60)
    print()


def parse_args():
    """解析命令行参数，构建 target_context"""
    dry_run = "--dry-run" in sys.argv
    target_ctx = {}

    for arg in sys.argv[1:]:
        if arg.startswith("--target="):
            parts = arg.split("=", 1)[1]
            if "=" in parts:
                name, version = parts.split("=", 1)
                target_ctx["name"] = name
                target_ctx["version"] = version
            else:
                target_ctx["name"] = parts
        elif arg.startswith("--context="):
            target_ctx["description"] = arg.split("=", 1)[1]
        elif arg.startswith("--focus="):
            areas = arg.split("=", 1)[1].split(",")
            target_ctx["focus_areas"] = [a.strip() for a in areas]
        elif arg.startswith("--target-url="):
            target_ctx["target_url"] = arg.split("=", 1)[1]

    load_seeds = "--no-seeds" not in sys.argv

    return dry_run, target_ctx, load_seeds


def main():
    dry_run, target_ctx, load_seeds = parse_args()

    if target_ctx:
        print(f"🎯 目标上下文: {json.dumps(target_ctx, ensure_ascii=False)}")
    print(f"{'Dry-Run Mode' if dry_run else 'Live Mode'} — 运行进化周期...")
    print()

    evolver = RedteamEvolver(target_context=target_ctx or None)

    if dry_run:
        # 只运行现有测试，不生成新用例
        print("  [Step 1/4] 运行测试套件...")
        results = evolver.run_existing_tests()
        analysis = evolver.analyze_results(results)
        report = {
            "round": "dry-run",
            "timestamp": __import__("datetime").datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "elapsed_seconds": 0,
            "suite_before": len(results),
            "suite_after": len(results),
            "pending_count": 0,
            "tests_run": analysis["total"],
            "passed": analysis["passed"],
            "failed": analysis["failed"],
            "pass_rate": analysis["pass_rate"],
            "new_candidates": 0,
            "added": [],
            "pending_approval": [],
            "skipped_duplicates": [],
            "weak_layers": analysis["weak_layers"],
            "failed_cases": analysis["failed_case_names"],
            "target_context": target_ctx,
        }
    else:
        report = evolver.evolve(target_context=target_ctx or None, load_seeds=load_seeds)
        if target_ctx:
            report["target_context"] = target_ctx

    print_report(report)

    return 1 if report["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
