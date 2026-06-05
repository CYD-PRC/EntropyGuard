"""Entropy Runtime · 元认知与自省模块
v7.2 Phase 3.2: 让系统能反思自己的行为。

核心能力:
  1. 红旗检测 (RedFlagDetector)   — 卡死/死循环/偏离
  2. 卡死预警 (StallDetector)     — 连续无进展
  3. 偏离度计算 (DriftCalculator) — 输出 vs 意图语义匹配
  4. 自省检查 (self_check)        — NORMAL / WARNING / CRITICAL
"""
import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult

logger = logging.getLogger("entropyruntime.metacognition")


# ========== 常量 ==========

DEFAULT_TIMEOUT_SECONDS = 120        # 常规子任务超时阈值
STALL_THRESHOLD = 3                  # 连续 N 个子任务无进展 → 卡死
DRIFT_THRESHOLD = 0.7                # 偏离度 > 0.7 → 高偏离
LONG_TASK_MULTIPLIER = 3             # 超过预估时间的 N 倍 → 疑似卡死
MAX_RETRIES_BEFORE_DEADLOCK = 3      # 同一子任务重试 > N 次 → 疑似死循环


# ========== 枚举 ==========

class CheckStatus(Enum):
    NORMAL = "NORMAL"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class Suggestion(Enum):
    CONTINUE = "continue"
    RETRY = "retry"
    DEGRADE = "degrade"        # 降级 Agent
    ESCALATE = "escalate"      # 请求人工介入
    ABORT = "abort"             # 中止执行


# ========== 数据模型 ==========

@dataclass
class RedFlag:
    """单个红旗告警"""
    flag_type: str          # "STALL" / "DEADLOCK" / "DRIFT" / "LONG_RUNNING"
    task_id: str
    severity: str           # "WARNING" / "CRITICAL"
    message: str
    details: dict = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


@dataclass
class CheckResult:
    """自省检查结果"""
    status: CheckStatus
    flags: list[RedFlag] = field(default_factory=list)
    suggestion: Suggestion = Suggestion.CONTINUE
    summary: str = ""
    drift_score: float = 0.0
    consecutive_stall: int = 0
    context: dict = field(default_factory=dict)
    timestamp: float = 0.0


# ========== 偏离度计算 ==========

class DriftCalculator:
    """计算子任务输出与预期意图的偏离程度。

    使用关键词重叠 + 结构模式匹配（无外部 AI 依赖）。
    """

    @staticmethod
    def compute(intent: str, output: str) -> float:
        """计算偏离度 0.0 ~ 1.0。

        0.0 = 完全匹配，1.0 = 完全偏离
        """
        if not intent or not output:
            return 1.0

        intent_lower = intent.lower()
        output_lower = output.lower()[:2000]  # 限制长度

        # 1. 关键词覆盖度 (weight: 0.6)
        keywords = DriftCalculator._extract_keywords(intent_lower)
        keyword_hits = sum(1 for kw in keywords if kw in output_lower)
        kw_score = 1.0 - (keyword_hits / max(len(keywords), 1))
        kw_score = max(0.0, min(1.0, kw_score))

        # 2. 通用错误信号 (weight: 0.3)
        error_signals = [
            "error", "exception", "traceback", "failed", "failure",
            "timeout", "超时", "错误", "异常", "失败",
            "not found", "无法访问", "拒绝连接",
            "module not found", "import error", "syntax error",
        ]
        error_hits = sum(1 for sig in error_signals if sig in output_lower)
        error_score = min(1.0, error_hits / 3.0)  # 3个错误信号=满分

        # 3. 输出空或极短 (weight: 0.1)
        empty_score = 0.0
        if len(output.strip()) < 10:
            empty_score = 0.8
        elif len(output.strip()) < 50:
            empty_score = 0.3

        final_score = kw_score * 0.6 + error_score * 0.3 + empty_score * 0.1
        return round(max(0.0, min(1.0, final_score)), 4)

    @staticmethod
    def _extract_keywords(text: str) -> list[str]:
        """从文本中提取有意义的特征关键词。"""
        # 移除常见无意义词
        stopwords = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不", "人",
            "都", "一", "一个", "上", "也", "很", "到", "说", "要", "去",
            "你", "会", "着", "没有", "看", "好", "自己", "这", "他", "她",
            "它", "们", "那", "些", "为", "所", "以", "能", "及", "与",
            "the", "a", "an", "is", "are", "was", "were", "be", "been",
            "to", "of", "in", "for", "on", "with", "at", "by", "from",
            "do", "does", "did", "doe", "have", "has", "had", "not",
            "and", "or", "but", "so", "if", "as", "an",
        }
        tokens = re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', text.lower())
        return [t for t in tokens if t not in stopwords and (len(t) > 1 or '\u4e00' <= t <= '\u9fff')]


# ========== 红旗检测 ==========

class RedFlagDetector:
    """检测子任务执行中的异常模式。"""

    @staticmethod
    def check_long_running(
        task: AgentTask,
        result: TaskResult,
        expected_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> Optional[RedFlag]:
        """检测子任务执行时间是否超过阈值 3 倍。"""
        if not result.elapsed_seconds:
            return None
        threshold = expected_timeout * LONG_TASK_MULTIPLIER
        if result.elapsed_seconds > threshold:
            return RedFlag(
                flag_type="LONG_RUNNING",
                task_id=task.id,
                severity="WARNING",
                message=(
                    f"子任务执行耗时 {result.elapsed_seconds:.0f}s，"
                    f"超过阈值 {threshold}s 的 {LONG_TASK_MULTIPLIER} 倍"
                ),
                details={
                    "elapsed": result.elapsed_seconds,
                    "threshold": threshold,
                    "expected_timeout": expected_timeout,
                },
            )
        return None

    @staticmethod
    def check_deadlock(
        task: AgentTask,
        result: TaskResult,
        retry_history: list[dict],
    ) -> Optional[RedFlag]:
        """检测同一子任务重复重试（疑似死循环）。"""
        attempts = len(retry_history)
        if attempts > MAX_RETRIES_BEFORE_DEADLOCK:
            return RedFlag(
                flag_type="DEADLOCK",
                task_id=task.id,
                severity="CRITICAL",
                message=(
                    f"子任务重试 {attempts} 次超过阈值 "
                    f"{MAX_RETRIES_BEFORE_DEADLOCK}，疑似死循环"
                ),
                details={
                    "retry_count": attempts,
                    "threshold": MAX_RETRIES_BEFORE_DEADLOCK,
                    "retry_history": retry_history,
                },
            )
        return None

    @staticmethod
    def check_drift(
        task: AgentTask,
        result: TaskResult,
        drift_score: float,
    ) -> Optional[RedFlag]:
        """检测子任务输出偏离意图。"""
        if drift_score > DRIFT_THRESHOLD:
            return RedFlag(
                flag_type="DRIFT",
                task_id=task.id,
                severity="WARNING" if drift_score < 0.9 else "CRITICAL",
                message=(
                    f"子任务输出偏离度 {drift_score:.2f} "
                    f"超过阈值 {DRIFT_THRESHOLD}"
                ),
                details={
                    "drift_score": drift_score,
                    "threshold": DRIFT_THRESHOLD,
                    "intent_preview": task.intent[:100],
                    "output_preview": (result.output or "")[:100],
                },
            )
        return None


# ========== 卡死预警 ==========

class StallDetector:
    """检测连续子任务无进展。"""

    def __init__(self):
        self._consecutive_stall = 0
        self._last_progress_time = time.time()
        self._stall_threshold = STALL_THRESHOLD

    def check(self, task: AgentTask, result: TaskResult) -> Optional[RedFlag]:
        """检查是否有进展。"""
        if result.success and result.output and len(result.output.strip()) > 20:
            # 有实质进展 → 重置计数
            self._consecutive_stall = 0
            self._last_progress_time = time.time()
            return None

        # 无进展
        self._consecutive_stall += 1
        if self._consecutive_stall >= self._stall_threshold:
            return RedFlag(
                flag_type="STALL",
                task_id=task.id,
                severity="CRITICAL",
                message=(
                    f"连续 {self._consecutive_stall} 个子任务无实质进展"
                ),
                details={
                    "consecutive_stall": self._consecutive_stall,
                    "threshold": self._stall_threshold,
                    "last_progress_seconds_ago": round(
                        time.time() - self._last_progress_time, 1
                    ),
                },
            )
        return None

    @property
    def consecutive_stall(self) -> int:
        return self._consecutive_stall


# ========== 自省检查主入口 ==========

class Metacognition:
    """元认知模块 — 执行自省检查的主入口。"""

    def __init__(self):
        self.stall_detector = StallDetector()
        self.drift_calculator = DriftCalculator()
        self._check_history: list[CheckResult] = []

    @property
    def check_history(self) -> list[dict]:
        """返回最近的自检记录（最多 50 条）。"""
        return [
            {
                "timestamp": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime(c.timestamp)
                ) if c.timestamp else "",
                "status": c.status.value,
                "flags": [
                    {"type": f.flag_type, "severity": f.severity, "message": f.message}
                    for f in (c.flags or [])
                ],
                "suggestion": c.suggestion.value,
                "summary": c.summary,
                "drift_score": c.drift_score,
            }
            for c in self._check_history[-50:]
        ]

    def self_check(
        self,
        task: AgentTask,
        result: TaskResult,
        retry_history: list[dict],
        expected_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> CheckResult:
        """对单个子任务的执行结果执行自省检查。

        Args:
            task: 已执行的子任务
            result: 子任务执行结果
            retry_history: 重试历史（来自 execute()）
            expected_timeout: 预期超时时间

        Returns:
            CheckResult
        """
        flags: list[RedFlag] = []

        # 1. 红旗检测
        long_flag = RedFlagDetector.check_long_running(
            task, result, expected_timeout
        )
        if long_flag:
            flags.append(long_flag)

        deadlock_flag = RedFlagDetector.check_deadlock(
            task, result, retry_history
        )
        if deadlock_flag:
            flags.append(deadlock_flag)

        # 2. 偏离度计算
        drift_score = DriftCalculator.compute(
            task.intent, result.output or ""
        )
        drift_flag = RedFlagDetector.check_drift(
            task, result, drift_score
        )
        if drift_flag:
            flags.append(drift_flag)

        # 3. 卡死检测
        stall_flag = self.stall_detector.check(task, result)
        if stall_flag:
            flags.append(stall_flag)

        # 4. 综合判定
        critical_flags = [f for f in flags if f.severity == "CRITICAL"]
        warning_flags = [f for f in flags if f.severity == "WARNING"]

        if critical_flags:
            status = CheckStatus.CRITICAL
            suggestion = self._suggest_for_critical(critical_flags)
            summary = (
                f"发现 {len(critical_flags)} 个严重红旗: "
                + "; ".join(f.message for f in critical_flags[:2])
            )
        elif warning_flags:
            status = CheckStatus.WARNING
            suggestion = Suggestion.RETRY
            summary = (
                f"发现 {len(warning_flags)} 个警告: "
                + "; ".join(f.message for f in warning_flags[:2])
            )
        else:
            status = CheckStatus.NORMAL
            suggestion = Suggestion.CONTINUE
            summary = "执行正常"

        cr = CheckResult(
            status=status,
            flags=flags,
            suggestion=suggestion,
            summary=summary,
            drift_score=drift_score,
            consecutive_stall=self.stall_detector.consecutive_stall,
            context={
                "task_id": task.id,
                "elapsed": result.elapsed_seconds,
                "success": result.success,
            },
        )
        # 记录时间戳便于 history 格式化
        cr.timestamp = time.time()
        self._check_history.append(cr)

        # 日志
        log_fn = logger.warning if status != CheckStatus.NORMAL else logger.info
        log_fn(
            "[Metacognition] %s: %s (drift=%.2f, stall=%d, flags=%d)",
            status.value, summary, drift_score,
            self.stall_detector.consecutive_stall, len(flags),
        )

        return cr

    def _suggest_for_critical(self, critical_flags: list[RedFlag]) -> Suggestion:
        """根据严重红旗类型给出建议。"""
        flag_types = [f.flag_type for f in critical_flags]

        if "DEADLOCK" in flag_types:
            return Suggestion.ESCALATE  # 死循环 → 请求人工介入
        if "STALL" in flag_types:
            return Suggestion.DEGRADE   # 卡死 → 降级 Agent
        if "DRIFT" in flag_types:
            return Suggestion.RETRY     # 偏离 → 重试
        return Suggestion.ABORT         # 兜底 → 中止

    def reset(self):
        """重置元认知状态（新计划开始时调用）。"""
        self.stall_detector = StallDetector()
        self._check_history.clear()
        logger.info("[Metacognition] 状态已重置")
