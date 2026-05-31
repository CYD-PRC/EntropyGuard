"""..."""
import sys, json
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

    if report["failed_cases"]:
        print("  ❌ 失败用例:")
        for name in report["failed_cases"]:
            print(f"     - {name}")
        print()

    if report["weak_layers"]:
        print(f"  ⚠️  薄弱安全层: {', '.join(report['weak_layers'])}")
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


def main():
    dry_run = "--dry-run" in sys.argv

    print(f"{'Dry-Run Mode' if dry_run else 'Live Mode'} — 运行进化周期...")
    print()

    evolver = RedteamEvolver()

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
        }
    else:
        report = evolver.evolve()

    print_report(report)

    # 返回退出码：有失败用例则返回 1
    return 1 if report["failed"] > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
