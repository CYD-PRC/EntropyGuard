"""Entropy Runtime · 宇宙透镜优先级引擎
v7.2 Phase 3.1: 价值驱动的优先级判断系统。

核心哲学:
  安全漏洞比功能需求更紧急。
  系统会说"不"的能力和说"是"同样重要。

价值分级:
  生存 > 进化 > 和谐 > 表达

  生存级 (Survival):    安全漏洞、数据泄露、系统崩溃、红队通过率<80%
  进化级 (Evolution):   红队发现的缺陷、性能瓶颈、合规要求
  和谐级 (Harmony):     架构优化、技术债清理、可观测性增强
  表达级 (Expression):  新功能、UI美化、文档、重构

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

from orchestrator.task_model import AgentTask

logger = logging.getLogger("entropyruntime.cosmic_lense")

# ========== 常量 ==========

REDTEAM_SUITE_PATH = Path("/root/EntropyGuard/security/redteam_suite.json")


class ValueTier(Enum):
    """价值层级（数字越小越紧急）"""
    SURVIVAL = 1     # 生存级
    EVOLUTION = 2    # 进化级
    HARMONY = 3      # 和谐级
    EXPRESSION = 4   # 表达级

    def __str__(self) -> str:
        names = {1: "生存", 2: "进化", 3: "和谐", 4: "表达"}
        return names.get(self.value, f"未知({self.value})")

    @property
    def priority_score(self) -> int:
        """数值化优先级（越小越紧急）"""
        return self.value


# ========== 关键词分级 ==========

SURVIVAL_KEYWORDS = [
    # 安全漏洞
    "安全漏洞", "security vulnerability", "vuln", "cve-", "cwe-",
    "数据泄露", "data breach", "数据泄漏", "信息泄露",
    "系统崩溃", "crash", "panic", "kernel panic", "oom",
    "rce", "远程代码执行", "代码注入", "命令注入", "sql注入",
    "csrf", "ssrf", "xss", "缓冲区溢出", "buffer overflow",
    "提权", "privilege escalation", "权限提升",
    "绕过", "bypass", "逃逸", "escape",
    "认证绕过", "auth bypass", "未授权访问",
    "勒索", "ransomware", "挖矿", "cryptominer",
    "shell注入", "path traversal", "路径遍历",
    # 红队通过率过低
    "红队通过率", "redteam pass rate", "安全评分过低",
    # 紧急
    "紧急", "urgent", "critical", "p0", "p1", "hotfix", "热修复",
]

EVOLUTION_KEYWORDS = [
    # 红队缺陷
    "红队缺陷", "redteam finding", "红队发现", "测试用例",
    "性能瓶颈", "性能优化", "performance bottleneck", "慢查询", "slow query",
    "高延迟", "high latency", "延迟优化",
    "合规", "compliance", "合规要求", "法规", "regulation",
    "降级", "degradation", "degrade", "资源耗尽",
    "内存泄漏", "memory leak", "句柄泄漏", "handle leak",
    "连接池", "connection pool", "线程泄漏",
    "审计遗留", "audit finding", "审计问题",
]

HARMONY_KEYWORDS = [
    # 架构
    "架构优化", "architecture", "重构", "refactor", "redesign",
    "技术债", "tech debt", "技术债务",
    "可观测性", "observability", "监控", "monitoring", "告警", "alert",
    "日志", "logging", "可追溯", "traceability",
    "扩展性", "scalability", "可扩展", "弹性",
    "容错", "fault tolerance", "高可用", "ha",
    "代码质量", "code quality", "代码审查", "code review",
    "安全性加固", "security hardening", "安全改进",
    "持续集成", "ci/cd", "自动化部署", "部署流水线",
    "数据一致性", "data consistency",
]

EXPRESSION_KEYWORDS = [
    # 新功能
    "新功能", "feature", "新增", "添加", "add", "开发",
    "ui", "界面", "前端", "frontend", "样式", "style",
    "文档", "doc", "readme", "注释", "comment",
    "单元测试", "unit test", "集成测试", "integration test",
    "美化", "beautify", "增强", "enhance", "提升", "improve",
    "体验", "ux", "用户体验", "dashboard", "仪表盘",
    "示例", "example", "demo", "教程", "tutorial",
    "blog", "博客", "网站", "website",
]


# ========== 数据模型 ==========

@dataclass
class PriorityResult:
    """宇宙透镜优先级评估结果"""
    tier: ValueTier
    tier_name_cn: str
    priority_score: int      # 1-100, 越小越紧急
    reason: str
    system_state: dict = field(default_factory=dict)
    overridden: bool = False
    original_tier: Optional[ValueTier] = None


@dataclass
class ConflictResult:
    """冲突检测结果"""
    has_conflict: bool
    severity: str              # "CRITICAL" / "WARNING" / "NONE"
    conflict_reason: str = ""
    suggested_action: str = ""
    blocking_tier: Optional[ValueTier] = None


# ========== 核心实现 ==========

class CosmicLense:
    """宇宙透镜 — 从系统全局视角评估任务优先级。

    特性:
      1. 价值分级: 生存 > 进化 > 和谐 > 表达
      2. 冲突检测: 新任务与系统当前状态的兼容性
      3. 自适应: 红队通过率、系统健康度自动影响阈值
    """

    def __init__(self):
        self._last_redteam_check: float = 0
        self._cached_redteam_rate: Optional[float] = None

    # ---- 公共 API ----

    def evaluate(self, task: AgentTask) -> PriorityResult:
        """评估单个子任务的宇宙优先级。

        Args:
            task: 待评估的子任务

        Returns:
            PriorityResult 包含价值层级、分数和理由
        """
        # 1. 识别价值层级
        tier = self._classify(task.intent, task.description, task.payload)
        tier_name_cn = str(tier)
        priority_score = self._compute_score(tier)

        # 2. 收集系统状态
        system_state = self._get_system_state()

        reason = self._build_reason(tier, system_state)

        # 3. 特殊情况覆盖
        overridden = False
        original_tier = None

        # 3a. 红队通过率 < 80% → 非生存级自动降一级
        redteam_rate = system_state.get("redteam_pass_rate", 100)
        if redteam_rate < 80 and tier.value > ValueTier.SURVIVAL.value:
            original_tier = tier
            downgraded_value = min(tier.value + 1, ValueTier.EXPRESSION.value)
            tier = ValueTier(downgraded_value)
            overridden = True
            reason += (
                f"\n⚠ 红队通过率 {redteam_rate:.0f}% < 80%，"
                f"{tier_name_cn}级自动降为{str(tier)}级"
            )
            tier_name_cn = str(tier)
            priority_score = self._compute_score(tier)

        # 3b. 系统健康度 < 50 → 和谐/表达级降为表达
        health = system_state.get("health_score", 100)
        if health < 50 and tier.value >= ValueTier.HARMONY.value:
            if not overridden:
                original_tier = tier
            if tier != ValueTier.EXPRESSION:
                tier = ValueTier.EXPRESSION
                overridden = True
                reason += (
                    f"\n⚠ 系统健康度 {health} < 50，"
                    f"{tier_name_cn}级降为表达级"
                )
                tier_name_cn = str(tier)
                priority_score = self._compute_score(tier)

        # --- v7.2 Phase 3.2: 元认知信号融合 ---
        # 如果任务携带 metacognition CRITICAL 标记，自动提升到生存级
        meta = task.payload.get("metacognition", {})
        if isinstance(meta, dict) and meta.get("status") == "CRITICAL":
            if tier != ValueTier.SURVIVAL:
                if not overridden:
                    original_tier = tier
                tier = ValueTier.SURVIVAL
                tier_name_cn = "生存"
                priority_score = self._compute_score(tier)
                overridden = True
                reason += (
                    f"\n🚨 元认知检测到严重问题 (drift={meta.get('drift_score',0):.2f})，"
                    f"自动提升到生存级"
                )

        return PriorityResult(
            tier=tier,
            tier_name_cn=tier_name_cn,
            priority_score=priority_score,
            reason=reason.strip(),
            system_state=system_state,
            overridden=overridden,
            original_tier=original_tier,
        )

    def evaluate_description(self, description: str) -> PriorityResult:
        """根据任务描述字符串评估优先级。"""
        dummy = AgentTask(
            id="dummy",
            intent=description,
            description=description,
        )
        return self.evaluate(dummy)

    def conflict_detect(
        self,
        incoming_task: AgentTask,
        current_state: Optional[dict] = None,
    ) -> ConflictResult:
        """检测新任务是否与系统当前状态冲突。

        Args:
            incoming_task: 新到达的任务
            current_state: 当前系统状态（自动获取 if None）

        Returns:
            ConflictResult
        """
        state = current_state or self._get_system_state()
        tier = self._classify(
            incoming_task.intent, incoming_task.description, incoming_task.payload
        )

        redteam_rate = state.get("redteam_pass_rate", 100)
        health_score = state.get("health_score", 100)
        pending_security = state.get("pending_security_tasks", 0)

        # 冲突 1: 红队通过率 < 80% 时，新功能请求降级
        if redteam_rate < 80 and tier == ValueTier.EXPRESSION:
            return ConflictResult(
                has_conflict=True,
                severity="WARNING",
                conflict_reason=(
                    f"红队通过率 {redteam_rate:.0f}% < 80%，"
                    f"表达级任务应推迟至安全修复完成后"
                ),
                suggested_action="defer_to_survival",
                blocking_tier=ValueTier.SURVIVAL,
            )

        # 冲突 2: 待处理的安全任务 > 3 个时，禁止表达级
        if pending_security > 3 and tier == ValueTier.EXPRESSION:
            return ConflictResult(
                has_conflict=True,
                severity="WARNING",
                conflict_reason=(
                    f"有待处理安全任务 {pending_security} 个，"
                    f"请先处理安全问题"
                ),
                suggested_action="resolve_security_first",
                blocking_tier=ValueTier.SURVIVAL,
            )

        # 冲突 3: 健康度过低，非生存级警告
        if health_score < 30 and tier != ValueTier.SURVIVAL:
            return ConflictResult(
                has_conflict=True,
                severity="CRITICAL",
                conflict_reason=(
                    f"系统健康度 {health_score} < 30，"
                    f"仅接受生存级任务"
                ),
                suggested_action="reject_until_recovery",
                blocking_tier=ValueTier.SURVIVAL,
            )

        # 无冲突
        return ConflictResult(
            has_conflict=False,
            severity="NONE",
        )

    # ---- 内部方法 ----

    def _classify(
        self,
        intent: str,
        description: str,
        payload: dict,
    ) -> ValueTier:
        """将任务分类到价值层级。"""
        combined = f"{intent} {description}".lower()

        # 检查负载中的显式指定
        explicit_tier = payload.get("cosmic_tier", "")
        if explicit_tier:
            tier_map = {
                "survival": ValueTier.SURVIVAL,
                "evolution": ValueTier.EVOLUTION,
                "harmony": ValueTier.HARMONY,
                "expression": ValueTier.EXPRESSION,
            }
            if explicit_tier.lower() in tier_map:
                return tier_map[explicit_tier.lower()]

        # 关键词匹配（从最高级向下匹配）
        if any(kw.lower() in combined for kw in SURVIVAL_KEYWORDS):
            return ValueTier.SURVIVAL
        if any(kw.lower() in combined for kw in EVOLUTION_KEYWORDS):
            return ValueTier.EVOLUTION
        if any(kw.lower() in combined for kw in HARMONY_KEYWORDS):
            return ValueTier.HARMONY

        # 兜底：表达级
        return ValueTier.EXPRESSION

    def _compute_score(self, tier: ValueTier) -> int:
        """将层级转换为 1-100 的数值评分。"""
        mapping = {
            ValueTier.SURVIVAL: (1, 25),
            ValueTier.EVOLUTION: (26, 50),
            ValueTier.HARMONY: (51, 75),
            ValueTier.EXPRESSION: (76, 100),
        }
        lo, hi = mapping.get(tier, (76, 100))
        # 层级内取中值
        return (lo + hi) // 2

    def _get_system_state(self) -> dict:
        """采集当前系统状态。"""
        state = {
            "redteam_pass_rate": self._get_redteam_pass_rate(),
            "health_score": self._get_health_score(),
            "pending_security_tasks": self._count_pending_security(),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        return state

    def _get_redteam_pass_rate(self) -> float:
        """读取红队测试通过率。"""
        # 缓存 60 秒
        now = time.time()
        if (self._cached_redteam_rate is not None
                and now - self._last_redteam_check < 60):
            return self._cached_redteam_rate

        try:
            if REDTEAM_SUITE_PATH.exists():
                with open(REDTEAM_SUITE_PATH) as f:
                    suite = json.load(f)
                tests = suite.get("tests", suite) if isinstance(suite, dict) else suite
                if not tests:
                    self._cached_redteam_rate = 100.0
                    return 100.0

                total = len(tests)
                passed = sum(
                    1 for t in tests
                    if t.get("last_result") == "passed"
                    or t.get("passed", False)
                )
                rate = (passed / total) * 100 if total else 100
                self._cached_redteam_rate = rate
                self._last_redteam_check = now

                # 也尝试读取 regression report
                report_path = REDTEAM_SUITE_PATH.parent / "regression_report.json"
                if report_path.exists():
                    with open(report_path) as f:
                        report = json.load(f)
                    if report.get("pass_rate"):
                        rate = float(report["pass_rate"])
                        self._cached_redteam_rate = rate

                return rate
        except Exception as e:
            logger.warning("[CosmicLense] 读取红队通过率失败: %s", e)

        return 100.0  # 默认乐观

    def _get_health_score(self) -> int:
        """获取系统健康度评分。"""
        try:
            from orchestrator.health_score import HealthScore
            health = HealthScore().evaluate()
            score = health.get("score", 100)
            if isinstance(score, (int, float)):
                return min(100, max(0, int(score)))
        except Exception as e:
            logger.warning("[CosmicLense] 获取健康度失败: %s", e)
        return 100  # 默认乐观

    def _count_pending_security(self) -> int:
        """统计待处理的安全任务数量。"""
        try:
            events_path = Path("/root/EntropyGuard/events.json")
            if events_path.exists():
                with open(events_path) as f:
                    events = json.load(f)
                if isinstance(events, list):
                    security_events = [
                        e for e in events
                        if any(kw in str(e).lower() for kw in
                               ["security", "vuln", "漏洞", "cve", "blocked"])
                    ]
                    return len(security_events)
        except Exception:
            pass
        return 0

    def _build_reason(self, tier: ValueTier, state: dict) -> str:
        """生成可读的优先级判定理由。"""
        redteam = state.get("redteam_pass_rate", "?")
        health = state.get("health_score", "?")
        return (
            f"宇宙透镜判定: {str(tier)}级任务"
            f" | 红队通过率 {redteam:.0f}%"
            f" | 系统健康度 {health}"
            f" | 价值排序: 生存 > 进化 > 和谐 > 表达"
        )
