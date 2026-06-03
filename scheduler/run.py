#!/usr/bin/env python3
"""
Entropy Runtime · 调度器 CLI 入口
===================================
v6.0 Part 1 — 基础自动化调度器

用法:
    python3 scheduler/run.py --task redteam   运行红队测试
    python3 scheduler/run.py --task health    执行健康检查
    python3 scheduler/run.py --task audit     执行系统审计（预留）
    python3 scheduler/run.py --task redteam --live  完整进化周期（非 dry-run）
"""

import argparse
import logging
import sys
from pathlib import Path

# ── 确保项目目录在 sys.path ──────────────────────────────────────
_PROJECT_DIR = Path(__file__).resolve().parent.parent
if str(_PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(_PROJECT_DIR))


# ── CLI ───────────────────────────────────────────────────────────
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Entropy Runtime 自动化调度器 CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python3 scheduler/run.py --task redteam     # 默认 dry-run 模式
    python3 scheduler/run.py --task redteam --live  # 完整进化周期
    python3 scheduler/run.py --task health      # 健康检查
    python3 scheduler/run.py --task audit       # 系统审计（预留）
        """,
    )
    parser.add_argument(
        "--task", "-t",
        choices=["redteam", "health", "audit"],
        required=True,
        help="调度任务类型",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="红队测试：运行完整进化周期（默认 dry-run）",
    )
    return parser.parse_args()


# ── 主入口 ────────────────────────────────────────────────────────
def main():
    args = parse_args()

    # 配置日志（覆盖 scheduler 内部 logger）
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("entropyruntime.cli")

    logger.info(f"═══ Entropy Runtime Scheduler v6.0 ═══")
    logger.info(f"任务: {args.task}")

    if args.task == "redteam":
        from scheduler.scheduler import schedule_redteam
        report = schedule_redteam(dry_run=not args.live)

        print()
        print("=" * 60)
        print("  调度器执行报告")
        print("=" * 60)
        print(f"  任务:       redteam{' (dry-run)' if not args.live else ''}")
        print(f"  测试数:     {report['tests_run']}")
        print(f"  通过:       {report['passed']}")
        print(f"  失败:       {report['failed']}")
        print(f"  通过率:     {report['pass_rate']}")
        if report.get("weak_layers"):
            print(f"  薄弱层:     {', '.join(report['weak_layers'])}")
        if report.get("failed_cases"):
            print(f"  失败用例:   {len(report['failed_cases'])} 个")
        if report.get("error"):
            print(f"  错误:       {report['error']}")
        print("=" * 60)
        print()

        sys.exit(1 if report["failed"] > 0 or report.get("error") else 0)

    elif args.task == "health":
        from scheduler.scheduler import schedule_healthcheck
        result = schedule_healthcheck()

        print()
        print("=" * 60)
        print("  调度器执行报告 — 健康检查")
        print("=" * 60)
        status = result.get("status", "unknown")
        status_emoji = {"ok": "✅", "warning": "⚠️", "critical": "🚨"}.get(status, "❓")
        print(f"  状态:       {status_emoji} {status.upper()}")
        for check_name, check_val in result.get("checks", {}).items():
            print(f"  {check_name}:     {check_val}")
        if result.get("anomalies"):
            print(f"  异常 ({len(result['anomalies'])} 个):")
            for a in result["anomalies"]:
                print(f"     {a}")
        print("=" * 60)
        print()

        sys.exit(0 if status == "ok" else 1)

    elif args.task == "audit":
        # [预留] 系统审计 — v6.0 Part 2
        logger.info("[audit] 系统审计任务已预留，将在 v6.0 Part 2 实现")
        print()
        print("=" * 60)
        print("  ⏳ 系统审计任务已预留")
        print("  将在 v6.0 Part 2 实现")
        print("=" * 60)
        print()
        sys.exit(0)


if __name__ == "__main__":
    main()
