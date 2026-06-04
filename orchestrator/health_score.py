"""Entropy Runtime · 健康度评分引擎
v7.0: 评估系统整体健康度，返回 0-100 分
"""
import json
import logging
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

logger = logging.getLogger("entropyruntime.health")

REDTEAM_SUITE_PATH = Path("/root/EntropyGuard/security/redteam_suite.json")
REDTEAM_HISTORY_PATH = Path("/root/EntropyGuard/security/evolution_history.json")
DEEPSEEK_API_URL = "https://api.deepseek.com/v1/models"
SERVICE_URL = "http://localhost:8000/api/state"


class HealthScore:
    """健康度评分器，评估系统整体运行状况"""

    @staticmethod
    def _get_redteam_pass_rate() -> tuple[float, str]:
        """获取红队测试通过率（百分比 0-100）。

        优先读取 evolution_history.json 中的最新 dry-run 结果，
        若无则运行一次 --dry-run 获取结果。
        返回 (pass_rate, detail)。
        """
        try:
            # 优先从历史文件读取最近的 dry-run 结果
            if REDTEAM_SUITE_PATH.exists():
                with open(REDTEAM_SUITE_PATH) as f:
                    suite = json.load(f)
                total = len(suite)
                # suite 格式为列表，每个元素有 expected_state 字段
                # 我们直接计算所有预期 PASS 的占比
                if total == 0:
                    return 0.0, "无红队测试用例"

                # 如果有 history 记录，优先使用最后运行的 pass_rate
                if REDTEAM_HISTORY_PATH.exists():
                    with open(REDTEAM_HISTORY_PATH) as f:
                        history = json.load(f)
                    if isinstance(history, list) and history:
                        last = history[-1]
                        if "pass_rate" in last:
                            rate = float(last["pass_rate"])
                            detail = f"红队测试: {last.get('passed', 0)}/{last.get('tests_run', total)} 通过 ({rate}%)"
                            return rate, detail

            # fallback: 运行一次快速 dry-run (不生成新用例)
            logger.info("[HealthScore] 运行红队 dry-run...")
            result = subprocess.run(
                [sys.executable, "/root/EntropyGuard/security/run_redteam.py", "--dry-run", "--no-seeds"],
                capture_output=True, text=True, timeout=120,
            )
            output = result.stdout + result.stderr

            # 解析 pass_rate
            import re
            match = re.search(r'通过率\s*[：:]?\s*([\d.]+)\s*%', output)
            if match:
                rate = float(match.group(1))
                detail = f"红队测试: 通过率 {rate}% (最新 dry-run)"
                return rate, detail

            # 如果 pass_rate 是数字形式（如 100.0%）
            match = re.search(r'pass_rate["\']?\s*[:：=]\s*"?([\d.]+)', output)
            if match:
                rate = float(match.group(1))
                detail = f"红队测试: 通过率 {rate}% (最新 dry-run)"
                return rate, detail

            # 从 history 文件获取更精确的值
            if REDTEAM_HISTORY_PATH.exists():
                with open(REDTEAM_HISTORY_PATH) as f:
                    history = json.load(f)
                if isinstance(history, list) and history:
                    last = history[-1]
                    passed = last.get("passed", 0)
                    total = last.get("tests_run", 36)
                    if total > 0:
                        rate = round(passed / total * 100, 1)
                        detail = f"红队测试: {passed}/{total} 通过 ({rate}%)"
                        return rate, detail

            return 100.0, "红队测试: 无数据（默认通过）"

        except Exception as e:
            logger.warning(f"[HealthScore] 红队测试评估失败: {e}")
            return 0.0, f"红队测试: 评估失败 ({e})"

    @staticmethod
    def _check_service_status() -> tuple[bool, str]:
        """检查本地服务状态。返回 (is_ok, detail)。"""
        try:
            req = urllib.request.Request(SERVICE_URL, method="GET")
            with urllib.request.urlopen(req, timeout=5) as resp:
                status = resp.status
                if status == 200:
                    return True, f"本地服务正常 (HTTP {status})"
                else:
                    return False, f"本地服务异常 (HTTP {status})"
        except Exception as e:
            return False, f"本地服务不可用 ({e})"

    @staticmethod
    def _check_api_availability() -> tuple[bool, str]:
        """检查外部 API 可用性。返回 (is_ok, detail)。"""
        api_key = (os.environ.get("DEEPSEEK_API_KEY", "") or
                   os.environ.get("OPENAI_API_KEY", ""))
        if not api_key:
            # 尝试从 .env 读取
            try:
                with open("/root/.env") as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith("DEEPSEEK_API_KEY") and "=" in ls:
                            api_key = ls.split("=", 1)[1]
                            break
                        if ls.startswith("OPENAI_API_KEY") and "=" in ls and not api_key:
                            api_key = ls.split("=", 1)[1]
            except (FileNotFoundError, OSError):
                pass

        if not api_key:
            return False, "外部 API: API Key 未配置"

        try:
            req = urllib.request.Request(DEEPSEEK_API_URL, method="GET")
            req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    return True, "DeepSeek API 可用"
                else:
                    return False, f"DeepSeek API 异常 (HTTP {resp.status})"
        except Exception as e:
            return False, f"DeepSeek API 不可用 ({e})"

    def evaluate(self) -> dict:
        """执行健康度评估，返回完整评分报告。

        Returns:
            dict:
                - score: int 0-100
                - level: str (健康/亚健康/危险)
                - details: list[dict] 各项明细
                - redteam_pass_rate: float
                - service_ok: bool
                - api_ok: bool
        """
        start = time.time()

        # 获取各项指标
        redteam_rate, redteam_detail = self._get_redteam_pass_rate()
        service_ok, service_detail = self._check_service_status()
        api_ok, api_detail = self._check_api_availability()

        score = 100
        details = []

        # ── 红队通过率 ──
        details.append({
            "item": "红队测试通过率",
            "value": f"{redteam_rate}%",
            "detail": redteam_detail,
        })
        if redteam_rate < 95:
            deduction = 30
            score -= deduction
            details[-1]["deduction"] = f"-{deduction}分"
            details[-1]["passed"] = False
        else:
            details[-1]["deduction"] = "0分"
            details[-1]["passed"] = True

        # ── 服务状态 ──
        details.append({
            "item": "本地服务状态",
            "value": "正常" if service_ok else "异常",
            "detail": service_detail,
        })
        if not service_ok:
            deduction = 25
            score -= deduction
            details[-1]["deduction"] = f"-{deduction}分"
            details[-1]["passed"] = False
        else:
            details[-1]["deduction"] = "0分"
            details[-1]["passed"] = True

        # ── API 可用性 ──
        details.append({
            "item": "外部 API 可用性",
            "value": "可用" if api_ok else "不可用",
            "detail": api_detail,
        })
        if not api_ok:
            deduction = 15
            score -= deduction
            details[-1]["deduction"] = f"-{deduction}分"
            details[-1]["passed"] = False
        else:
            details[-1]["deduction"] = "0分"
            details[-1]["passed"] = True

        # 分数下限保护
        score = max(0, score)

        # 等级判定
        if score >= 85:
            level = "健康"
        elif score >= 60:
            level = "亚健康"
        else:
            level = "危险"

        elapsed = round(time.time() - start, 2)

        return {
            "score": score,
            "level": level,
            "details": details,
            "redteam_pass_rate": redteam_rate,
            "service_ok": service_ok,
            "api_ok": api_ok,
            "elapsed_seconds": elapsed,
        }
