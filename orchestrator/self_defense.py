"""Entropy Runtime · 自主安全加固
Phase 5 Module 4: 持续监控 + 配置加固 + 攻击面扫描。

流程:
  continuous_monitor() → 每小时自动运行
  harden_config() → 检查安全配置
  attack_surface_scan() → 扫描攻击面
"""
import json
import logging
import os
import re
import socket
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("entropyruntime.self_defense")

REPO_DIR = "/root/EntropyGuard"
REGRESSION_REPORT = Path(REPO_DIR) / "security" / "regression_report.json"


@dataclass
class ConfigCheck:
    """配置检查结果"""
    check_name: str
    passed: bool
    detail: str
    severity: str = "info"  # "critical" / "warning" / "info"


@dataclass
class AttackSurface:
    """攻击面扫描结果"""
    open_ports: list[int] = field(default_factory=list)
    unprotected_endpoints: list[str] = field(default_factory=list)
    hardcoded_keys: list[str] = field(default_factory=list)
    total_issues: int = 0
    risk_level: str = "low"


@dataclass
class MonitorReport:
    """监控周期报告"""
    cycle_id: int
    timestamp: float
    regression_passed: int = 0
    regression_total: int = 0
    vulnerabilities_found: int = 0
    patches_applied: int = 0
    patches_failed: int = 0
    config_issues: list[ConfigCheck] = field(default_factory=list)
    attack_surface: Optional[AttackSurface] = None
    escalated: list[str] = field(default_factory=list)


class SelfDefense:
    """自主安全加固引擎。"""

    def __init__(self):
        self._cycle = 0
        self._reports: list[MonitorReport] = []

    # ---- 持续监控 ----

    def continuous_monitor(self) -> MonitorReport:
        """执行一次完整的监控周期。"""
        self._cycle += 1
        t0 = time.time()
        report = MonitorReport(
            cycle_id=self._cycle,
            timestamp=time.time(),
        )
        logger.info("[SelfDefense] === 监控周期 #%d ===", self._cycle)

        # 1. 配置检查
        report.config_issues = self.harden_config()

        # 2. 攻击面扫描
        report.attack_surface = self.attack_surface_scan()

        # 3. 红队回归测试
        self._run_regression(report)

        # 4. 自动修复
        self._auto_fix(report)

        # 5. 记录
        self._reports.append(report)
        self._save_report(report)

        logger.info(
            "[SelfDefense] 周期 #%d 完成: %d/%d passed, %d patches, %.1fs",
            self._cycle, report.regression_passed, report.regression_total,
            report.patches_applied, time.time() - t0,
        )
        return report

    def _run_regression(self, report: MonitorReport):
        """运行红队回归测试。"""
        try:
            import subprocess
            r = subprocess.run(
                ["python3", str(Path(REPO_DIR) / "opt" / "scripts" / "run_redteam_regression.py")],
                capture_output=True, text=True, timeout=600,
                cwd=REPO_DIR,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            logger.warning("[SelfDefense] 回归测试执行失败（脚本可能不存在）")
            # 尝试读取已有报告
            if REGRESSION_REPORT.exists():
                try:
                    with open(REGRESSION_REPORT) as f:
                        data = json.load(f)
                    report.regression_total = data.get("total", 0)
                    report.regression_passed = data.get("passed", 0)
                except Exception:
                    pass
            return

        if REGRESSION_REPORT.exists():
            try:
                with open(REGRESSION_REPORT) as f:
                    data = json.load(f)
                report.regression_total = data.get("total", 0)
                report.regression_passed = data.get("passed", 0)
                # 统计失败用例
                for res in data.get("results", []):
                    if not res.get("passed") and res.get("actual") != "0":
                        report.vulnerabilities_found += 1
            except Exception:
                pass

    def _auto_fix(self, report: MonitorReport):
        """自动修复发现的漏洞。"""
        try:
            sys.path.insert(0, REPO_DIR)
            from security.auto_patcher import AutoPatcher
            patcher = AutoPatcher()
            patcher.reset_merge_counter()

            # 读取回归报告中的失败用例
            if REGRESSION_REPORT.exists():
                with open(REGRESSION_REPORT) as f:
                    data = json.load(f)

                # 过滤出实际失败的用例（不包括超时）
                failures = [
                    r for r in data.get("results", [])
                    if not r.get("passed") and r.get("actual") not in ("0", "")
                ]

                if failures:
                    results = patcher.run_auto_fix_cycle(failures)
                    for res in results:
                        if res.get("status") == "merged":
                            report.patches_applied += 1
                        elif res.get("status") == "sandbox_failed":
                            report.patches_failed += 1
                        elif res.get("status") in ("skipped",):
                            report.escalated.append(
                                f"{res.get('vulnerability_id', '?')}: {res.get('reason', '')}"
                            )

        except Exception as e:
            logger.warning("[SelfDefense] 自动修复异常: %s", e)

    # ---- 配置加固 ----

    def harden_config(self) -> list[ConfigCheck]:
        """检查安全配置并返回问题列表。"""
        checks: list[ConfigCheck] = []

        # 1. API 认证
        try:
            api_path = Path(REPO_DIR) / "routes" / "api.py"
            if api_path.exists():
                content = api_path.read_text()
                has_auth = "verify_api_token" in content
                checks.append(ConfigCheck(
                    "API 认证", has_auth,
                    "API Token 认证已启用" if has_auth else "API 认证缺失！",
                    "critical" if not has_auth else "info",
                ))
        except Exception as e:
            checks.append(ConfigCheck("API 认证", False, str(e), "critical"))

        # 2. 文件白名单
        try:
            from security.auto_patcher import ALLOWED_PATCH_DIRS
            checks.append(ConfigCheck(
                "补丁白名单", len(ALLOWED_PATCH_DIRS) >= 5,
                f"{len(ALLOWED_PATCH_DIRS)} 条规则",
                "info",
            ))
        except Exception:
            checks.append(ConfigCheck("补丁白名单", False, "未配置", "warning"))

        # 3. .env 权限
        env_path = Path("/root/.env")
        if env_path.exists():
            mode = oct(env_path.stat().st_mode)[-3:]
            is_600 = mode == "600"
            checks.append(ConfigCheck(
                ".env 权限", is_600,
                f"权限 {mode}" if is_600 else f"权限 {mode}，应为 600",
                "critical" if not is_600 else "info",
            ))

        # 4. Shell 注入拦截
        try:
            sec_path = Path(REPO_DIR) / "security.py"
            if sec_path.exists():
                content = sec_path.read_text()
                has_blocked = "BLOCKED_PATTERNS" in content
                checks.append(ConfigCheck(
                    "命令拦截规则", has_blocked,
                    "BLOCKED_PATTERNS 已配置" if has_blocked else "未配置命令拦截！",
                    "critical" if not has_blocked else "info",
                ))
        except Exception as e:
            checks.append(ConfigCheck("命令拦截规则", False, str(e), "critical"))

        return checks

    # ---- 攻击面扫描 ----

    def attack_surface_scan(self) -> AttackSurface:
        """扫描系统攻击面。"""
        surface = AttackSurface()

        # 1. 扫描开放端口
        for port in [22, 80, 443, 5000, 8000, 8080, 9090]:
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2)
                result = sock.connect_ex(("127.0.0.1", port))
                if result == 0:
                    surface.open_ports.append(port)
                sock.close()
            except Exception:
                pass

        # 2. 检查高风险端点是否有认证
        sensitive_endpoints = [
            "/api/events", "/api/state", "/api/stats",
            "/api/dashboard-data", "/api/chat",
        ]
        for ep in sensitive_endpoints:
            try:
                import urllib.request
                req = urllib.request.Request(f"http://127.0.0.1:5000{ep}")
                resp = urllib.request.urlopen(req, timeout=3)
                if resp.status == 200:
                    surface.unprotected_endpoints.append(ep)
            except Exception:
                pass  # 返回 401/403 表示有认证，这是好的

        # 3. 检查硬编码密钥
        py_files = list(Path(REPO_DIR).rglob("*.py"))
        key_patterns = [
            r'(?i)(api.?key|secret|password|token)\s*=\s*["\'][a-zA-Z0-9_\-]{20,}["\']',
        ]
        for pyf in py_files:
            if "test" in pyf.name or "venv" in str(pyf):
                continue
            try:
                text = pyf.read_text()
                for pat in key_patterns:
                    for match in re.finditer(pat, text):
                        surface.hardcoded_keys.append(f"{pyf.name}:{match.group(0)[:40]}")
            except Exception:
                pass

        surface.total_issues = (
            len(surface.open_ports)
            + len(surface.unprotected_endpoints)
            + len(surface.hardcoded_keys)
        )
        surface.risk_level = (
            "high" if surface.total_issues > 5
            else "medium" if surface.total_issues > 2
            else "low"
        )
        return surface

    def _save_report(self, report: MonitorReport):
        """保存监控报告。"""
        path = Path(REPO_DIR) / "security" / "self_defense_log.json"
        try:
            data = []
            if path.exists():
                with open(path) as f:
                    data = json.load(f)
            data.append({
                "cycle": report.cycle_id,
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(report.timestamp)
                ),
                "regression": f"{report.regression_passed}/{report.regression_total}",
                "patches_applied": report.patches_applied,
                "patches_failed": report.patches_failed,
                "escalated": report.escalated,
                "attack_surface_ports": report.attack_surface.open_ports if report.attack_surface else [],
            })
            with open(path, "w") as f:
                json.dump(data[-50:], f, indent=2)
        except Exception as e:
            logger.warning("[SelfDefense] 保存报告失败: %s", e)

    def get_status(self) -> dict:
        latest = self._reports[-1] if self._reports else None
        return {
            "total_cycles": self._cycle,
            "latest_report": {
                "cycle": latest.cycle_id if latest else 0,
                "regression": f"{latest.regression_passed}/{latest.regression_total}" if latest else "N/A",
                "patches_applied": latest.patches_applied if latest else 0,
                "config_issues": len(latest.config_issues) if latest else 0,
                "escalated": latest.escalated if latest else [],
            } if latest else {},
            "config_checks": latest.config_issues if latest else [],
            "attack_surface": latest.attack_surface.__dict__ if latest and latest.attack_surface else {},
        }
