"""
Entropy Runtime · 调度核心
===========================
schedule_redteam()     — 调用红队测试 → 解析结果 → 微信通知
schedule_healthcheck() — 执行健康检查 → 异常时微信通知

每个函数可独立运行：
    python3 -c "from scheduler.scheduler import schedule_redteam; schedule_redteam()"
"""

import json
import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ── 路径 ──────────────────────────────────────────────────────────
_PROJECT_DIR = Path("/root/EntropyGuard")
_REDTEAM_SCRIPT = _PROJECT_DIR / "security" / "run_redteam.py"
_HEALTHCHECK_SCRIPT = Path("/usr/local/bin/entropyruntime-healthcheck.sh")

# ── 日志 ──────────────────────────────────────────────────────────
logger = logging.getLogger("entropyruntime.scheduler")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter(
        "%(asctime)s | %(levelname)-8s | scheduler | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(_h)


# ═══════════════════════════════════════════════════════════════════
# 任务 1: 红队测试
# ═══════════════════════════════════════════════════════════════════

def schedule_redteam(dry_run: bool = True) -> dict:
    """运行红队测试，解析报告，发送微信通知。

    Args:
        dry_run: True=只跑现有测试(默认), False=完整进化周期

    Returns:
        report: 解析后的测试报告 dict，含 tests_run/passed/failed/pass_rate
    """
    project_dir = str(_PROJECT_DIR)
    cmd = [sys.executable or "python3", str(_REDTEAM_SCRIPT)]
    if dry_run:
        cmd.append("--dry-run")

    logger.info(f"[schedule_redteam] 启动: {' '.join(cmd)}")
    t0 = time.time()

    try:
        result = subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        logger.error("[schedule_redteam] 超时 (600s)")
        return _notify_redteam_failed("超时 (600s)")
    except FileNotFoundError as e:
        logger.error(f"[schedule_redteam] 脚本未找到: {e}")
        return _notify_redteam_failed(f"脚本未找到: {e}")
    except Exception as e:
        logger.error(f"[schedule_redteam] 执行异常: {e}")
        return _notify_redteam_failed(str(e))

    elapsed = time.time() - t0
    logger.info(f"[schedule_redteam] 完成 ({elapsed:.1f}s), 退出码={result.returncode}")

    if result.returncode != 0:
        stderr = result.stderr.strip() or "no stderr"
        logger.warning(f"[schedule_redteam] 非零退出码: {stderr}")
        # 即使非零退出，也尝试从 stdout 提取报告

    # ── 从 stdout 提取报告 ────────────────────────────────────────
    report = _parse_redteam_output(result.stdout)

    # 补充原始输出到日志（debug 级别）
    logger.debug(f"[schedule_redteam] stdout ({len(result.stdout)} chars)")
    logger.debug(f"[schedule_redteam] stderr ({len(result.stderr)} chars)")

    # ── 发送微信通知 ──────────────────────────────────────────────
    try:
        from notify.wechat import send_test_complete
        suite_name = report.get("suite_name", "红队测试")
        send_test_complete(
            suite_name=suite_name,
            total=report["tests_run"],
            passed=report["passed"],
            failed=report["failed"],
            pass_rate=report["pass_rate"],
        )
        logger.info("[schedule_redteam] 微信通知已发送")
    except ImportError as e:
        logger.warning(f"[schedule_redteam] notify.wechat 不可用: {e}")
    except Exception as e:
        logger.warning(f"[schedule_redteam] 微信通知发送失败: {e}")

    return report


def _parse_redteam_output(stdout: str) -> dict:
    """从 run_redteam.py 的 stdout 解析测试报告。

    尝试提取关键指标: 测试总数、通过、失败、通过率。
    如果 stdout 为空或无法解析，返回默认值。
    """
    default = {
        "tests_run": 0,
        "passed": 0,
        "failed": 0,
        "pass_rate": "N/A",
        "suite_name": "红队测试",
        "elapsed_seconds": 0,
        "weak_layers": [],
        "failed_cases": [],
    }

    lines = stdout.strip().split("\n")
    if not lines or not stdout.strip():
        logger.warning("[schedule_redteam] stdout 为空")
        return default

    # 提取通过率: 从 ASCII 表格中匹配 "| N        | M       | K       | X% |"
    pass_rate = None
    total = 0
    passed = 0
    failed = 0

    for line in lines:
        line_s = line.strip()
        # 匹配类似 "| 10        | 7        | 3        | 70% |"
        if line_s.startswith("│") and "%" in line_s:
            parts = [p.strip() for p in line_s.split("│") if p.strip()]
            if len(parts) >= 4:
                try:
                    total = int(parts[0])
                    passed = int(parts[1])
                    failed = int(parts[2])
                    pass_rate = parts[3].rstrip("%")
                except (ValueError, IndexError):
                    pass

    # 提取套件名称
    suite_name = "红队测试"
    for line in lines:
        if "套件" in line and "→" in line:
            # "  测试套件: 10 → 15 条"
            pass
        if "目标:" in line and "?" not in line:
            name = line.split(":")[-1].strip()
            if name:
                suite_name = name

    # 提取薄弱层
    weak_layers = []
    for line in lines:
        if "薄弱安全层:" in line:
            parts = line.split(":")
            if len(parts) > 1:
                weak_layers = [l.strip() for l in parts[1].split(",") if l.strip()]

    # 提取失败用例
    failed_cases = []
    in_failed = False
    for line in lines:
        ls = line.strip()
        if ls.startswith("❌ 失败用例:"):
            in_failed = True
            continue
        if in_failed:
            if ls.startswith("- "):
                failed_cases.append(ls[2:].strip())
            elif not ls:
                in_failed = False

    result = {
        "tests_run": total,
        "passed": passed,
        "failed": failed,
        "pass_rate": f"{pass_rate}%" if pass_rate else "N/A",
        "suite_name": suite_name,
        "elapsed_seconds": 0,
        "weak_layers": weak_layers,
        "failed_cases": failed_cases,
    }

    logger.info(
        f"[schedule_redteam] 解析结果: {result['tests_run']} 测试, "
        f"{result['passed']} 通过, {result['failed']} 失败, "
        f"通过率 {result['pass_rate']}"
    )
    return result


def _notify_redteam_failed(error_msg: str) -> dict:
    """红队测试执行失败时发送通知"""
    try:
        from notify.wechat import send_notification
        send_notification(
            title="红队测试执行失败",
            content=f"❌ schedule_redteam 执行异常\n\n**错误**: {error_msg}\n\n**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            level="error",
        )
    except ImportError:
        pass
    except Exception:
        pass

    return {
        "tests_run": 0,
        "passed": 0,
        "failed": -1,
        "pass_rate": "0%",
        "suite_name": "红队测试",
        "error": error_msg,
    }


# ═══════════════════════════════════════════════════════════════════
# 任务 2: 健康检查
# ═══════════════════════════════════════════════════════════════════

# 告警阈值
_HEALTH_WARN_MEMORY_KB = 500_000    # 500 MB RSS
_HEALTH_WARN_EVENTS_BYTES = 5_000_000  # 5 MB events.json
_HEALTH_CRIT_EVENTS_BYTES = 20_000_000 # 20 MB events.json


def schedule_healthcheck() -> dict:
    """执行健康检查，异常时发送微信通知。

    检查项:
        - gunicorn 服务是否运行
        - RSS 内存是否超限
        - events.json 是否过大
        - 脚本是否正常返回 JSON

    Returns:
        {"status": "ok"|"warning"|"critical", "checks": {...}}
    """
    logger.info("[schedule_healthcheck] 启动健康检查...")
    t0 = time.time()

    # ── 执行 healthcheck 脚本 ─────────────────────────────────────
    try:
        result = subprocess.run(
            [str(_HEALTHCHECK_SCRIPT)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        logger.critical("[schedule_healthcheck] 脚本不存在")
        _notify_health_anomaly("脚本不存在", f"{_HEALTHCHECK_SCRIPT} 未安装")
        return {"status": "critical", "error": "脚本不存在"}
    except subprocess.TimeoutExpired:
        logger.error("[schedule_healthcheck] 超时")
        _notify_health_anomaly("健康检查超时", "healthcheck 脚本执行超过 10 秒")
        return {"status": "critical", "error": "超时"}
    except Exception as e:
        logger.error(f"[schedule_healthcheck] 执行异常: {e}")
        _notify_health_anomaly("健康检查执行异常", str(e))
        return {"status": "critical", "error": str(e)}

    # ── 解析 JSON ─────────────────────────────────────────────────
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        logger.error(f"[schedule_healthcheck] JSON 解析失败: {e}")
        _notify_health_anomaly("健康检查输出异常", f"JSON 解析失败: {e}")
        return {"status": "critical", "error": f"JSON 解析失败: {e}"}

    elapsed = time.time() - t0
    logger.info(f"[schedule_healthcheck] 完成 ({elapsed:.1f}s)")

    # ── 逐项检查 ──────────────────────────────────────────────────
    checks = {}
    anomalies = []

    # 1) 服务运行状态
    service_ok = data.get("service", False)
    checks["service_running"] = service_ok
    if not service_ok:
        anomalies.append(("critical", "gunicorn 服务未运行"))

    # 2) 内存使用
    mem_kb = data.get("memory_kb", 0)
    checks["memory_kb"] = mem_kb
    try:
        mem_kb = int(mem_kb)
    except (ValueError, TypeError):
        mem_kb = 0
    if mem_kb > _HEALTH_WARN_MEMORY_KB:
        mem_mb = mem_kb / 1024
        anomalies.append(("warning", f"RSS 内存 {mem_mb:.0f}MB 超过阈值 {_HEALTH_WARN_MEMORY_KB/1024:.0f}MB"))
    if mem_kb > _HEALTH_WARN_MEMORY_KB * 2:
        anomalies.append(("critical", f"RSS 内存 {mem_mb:.0f}MB 严重超限"))

    # 3) events.json 体积
    ev_bytes = data.get("events_json_bytes", 0)
    checks["events_json_bytes"] = ev_bytes
    try:
        ev_bytes = int(ev_bytes)
    except (ValueError, TypeError):
        ev_bytes = 0
    if ev_bytes > _HEALTH_CRIT_EVENTS_BYTES:
        ev_mb = ev_bytes / 1024 / 1024
        anomalies.append(("critical", f"events.json 体积 {ev_mb:.1f}MB 超过严重阈值 {_HEALTH_CRIT_EVENTS_BYTES/1024/1024:.0f}MB"))
    elif ev_bytes > _HEALTH_WARN_EVENTS_BYTES:
        ev_mb = ev_bytes / 1024 / 1024
        anomalies.append(("warning", f"events.json 体积 {ev_mb:.1f}MB 超过警告阈值 {_HEALTH_WARN_EVENTS_BYTES/1024/1024:.0f}MB"))

    # 4) 版本检查
    checks["version"] = data.get("version", "unknown")
    checks["uptime"] = data.get("uptime", "unknown")

    # ── 判断总体状态并通知 ────────────────────────────────────────
    check_results = {"checks": checks, "elapsed_seconds": round(elapsed, 1)}

    if not anomalies:
        logger.info("[schedule_healthcheck] 所有检查通过 ✓")
        check_results["status"] = "ok"
        return check_results

    # 有异常 → 汇总后通知
    max_level = "warning"
    anomaly_lines = []
    for level, msg in anomalies:
        if level == "critical":
            max_level = "critical"
        emoji = "🚨" if level == "critical" else "⚠️"
        anomaly_lines.append(f"- {emoji} {msg}")

    anomaly_text = "\n".join(anomaly_lines)
    summary = f"发现 {len(anomalies)} 个异常"
    check_results["status"] = max_level
    check_results["anomalies"] = anomaly_lines

    logger.warning(f"[schedule_healthcheck] {summary}")

    _notify_health_anomaly(
        anomaly_type=f"健康检查: {summary}",
        description=anomaly_text,
        metrics={
            "service_running": str(service_ok),
            "memory_kb": str(mem_kb),
            "events_json_bytes": str(ev_bytes),
        },
    )

    return check_results


def _notify_health_anomaly(anomaly_type: str, description: str,
                           metrics: dict = None) -> None:
    """健康异常通知"""
    try:
        from notify.wechat import send_env_anomaly
        send_env_anomaly(
            anomaly_type=anomaly_type,
            description=description,
            metrics=metrics,
        )
        logger.info(f"[schedule_healthcheck] 异常通知已发送: {anomaly_type}")
    except ImportError as e:
        logger.warning(f"[schedule_healthcheck] notify.wechat 不可用: {e}")
    except Exception as e:
        logger.warning(f"[schedule_healthcheck] 通知发送失败: {e}")


# ═══════════════════════════════════════════════════════════════════
# 独立入口
# ═══════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    task = sys.argv[1] if len(sys.argv) > 1 else "redteam"
    if task == "redteam":
        schedule_redteam(dry_run="--live" not in sys.argv)
    elif task == "health":
        schedule_healthcheck()
    else:
        print(f"用法: python3 -m scheduler.scheduler [redteam|health]")
        sys.exit(1)
