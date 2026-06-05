"""Entropy Runtime · 自主目标设定引擎
Phase 5 Module 1: 环境感知 + 目标推导 + 优先级排序。

流程: scan_environment() → derive_goals() → prioritize_goals() → /api/goals
"""
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from orchestrator.cosmic_lense import CosmicLense, ValueTier

logger = logging.getLogger("entropyruntime.goal_engine")

MEMORY_DB = "/var/lib/entropyguard/memory.db"


@dataclass
class EnvironmentState:
    """系统环境快照"""
    health_score: int = 100
    redteam_pass_rate: float = 100.0
    disk_used_pct: int = 0
    memory_used_pct: int = 0
    docker_healthy: int = 0
    total_security_events: int = 0
    pending_vulnerabilities: int = 0
    days_since_last_evolution: float = 999
    high_skill_available: bool = False
    high_skill_days_unused: float = 999
    last_regression_pass_rate: float = 100.0
    timestamp: float = field(default_factory=time.time)


@dataclass
class Goal:
    """自动推导的系统目标"""
    id: str
    description: str
    intent: str
    priority: ValueTier
    reason: str
    auto_generated: bool = True
    completed: bool = False
    created_at: float = field(default_factory=time.time)
    source_metric: str = ""


class GoalEngine:
    """自主目标设定引擎 — 感知环境 → 推导目标 → 优先级排序。"""

    def __init__(self):
        self._lense = CosmicLense()
        self._goal_history: list[Goal] = []

    def scan_environment(self) -> EnvironmentState:
        """采集系统环境状态。"""
        state = EnvironmentState()

        # 1. 健康度
        try:
            from orchestrator.health_score import HealthScore
            health = HealthScore().evaluate()
            state.health_score = health.get("score", 100)
        except Exception as e:
            logger.warning("[GoalEngine] 健康度采集失败: %s", e)

        # 2. 红队通过率
        try:
            rate = self._lense._get_redteam_pass_rate()
            state.redteam_pass_rate = rate
        except Exception as e:
            logger.warning("[GoalEngine] 红队通过率采集失败: %s", e)

        # 3. 磁盘使用率
        try:
            import shutil
            usage = shutil.disk_usage("/")
            state.disk_used_pct = int(usage.used / usage.total * 100)
        except Exception:
            pass

        # 4. 内存使用率
        try:
            with open("/proc/meminfo") as f:
                mem = {}
                for line in f:
                    parts = line.split()
                    if parts:
                        mem[parts[0].rstrip(":")] = int(parts[1])
            total = mem.get("MemTotal", 1)
            avail = mem.get("MemAvailable", total)
            state.memory_used_pct = int((total - avail) / total * 100)
        except Exception:
            pass

        # 5. Docker 健康容器
        try:
            import subprocess
            r = subprocess.run(
                ["docker", "ps", "--filter", "health=healthy", "-q"],
                capture_output=True, text=True, timeout=5,
            )
            state.docker_healthy = len(r.stdout.strip().splitlines()) if r.stdout.strip() else 0
        except Exception:
            pass

        # 6. 红队进化历史时间
        try:
            history_path = Path("/root/EntropyGuard/security/evolution_history.json")
            if history_path.exists():
                with open(history_path) as f:
                    history = json.load(f)
                if isinstance(history, list) and history:
                    last = history[-1].get("timestamp", "")
                    if last:
                        last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                        state.days_since_last_evolution = (
                            datetime.now(timezone.utc) - last_dt
                        ).total_seconds() / 86400
        except Exception:
            pass

        # 7. 高成功率技能
        try:
            from orchestrator.memory_store import MemoryStore
            store = MemoryStore()
            skills = store.query_skills(min_rate=0.9)
            if skills:
                state.high_skill_available = True
                newest = max(s.last_used for s in skills)
                state.high_skill_days_unused = (time.time() - newest) / 86400
            store.close()
        except Exception:
            pass

        # 8. 回归测试通过率
        try:
            report_path = Path("/root/EntropyGuard/security/regression_report.json")
            if report_path.exists():
                with open(report_path) as f:
                    report = json.load(f)
                state.last_regression_pass_rate = report.get("pass_rate", 100.0)
        except Exception:
            pass

        logger.info(
            "[GoalEngine] 环境扫描完成: health=%d, redteam=%.0f%%, disk=%d%%, mem=%d%%",
            state.health_score, state.redteam_pass_rate,
            state.disk_used_pct, state.memory_used_pct,
        )
        return state

    def derive_goals(self, state: Optional[EnvironmentState] = None) -> list[Goal]:
        """根据环境状态自动生成目标。"""
        if state is None:
            state = self.scan_environment()

        goals: list[Goal] = []
        seen_intents: set[str] = set()

        def _add(desc: str, intent: str, priority: ValueTier,
                 reason: str, metric: str = ""):
            if intent not in seen_intents:
                seen_intents.add(intent)
                gid = f"goal-{int(time.time())}-{len(goals)}"
                goals.append(Goal(
                    id=gid, description=desc, intent=intent,
                    priority=priority, reason=reason, source_metric=metric,
                ))

        # 规则 1: 红队通过率 < 80%
        if state.redteam_pass_rate < 80:
            _add(
                "红队通过率低于80%，需修复安全漏洞",
                f"修复安全漏洞: 红队通过率 {state.redteam_pass_rate:.0f}% 低于 80%",
                ValueTier.SURVIVAL,
                f"红队通过率 {state.redteam_pass_rate:.0f}% < 80%",
                metric="redteam_pass_rate",
            )

        # 规则 2: 磁盘 > 85%
        if state.disk_used_pct > 85:
            _add(
                f"磁盘使用率 {state.disk_used_pct}%，需要清理",
                f"清理磁盘空间: 当前使用率 {state.disk_used_pct}%",
                ValueTier.HARMONY,
                f"磁盘使用率 {state.disk_used_pct}% > 85%",
                metric="disk_used_pct",
            )

        # 规则 3: 内存 > 90%
        if state.memory_used_pct > 90:
            _add(
                f"内存使用率 {state.memory_used_pct}%，需优化",
                f"优化内存使用: 当前使用率 {state.memory_used_pct}%",
                ValueTier.SURVIVAL,
                f"内存使用率 {state.memory_used_pct}% > 90%",
                metric="memory_used_pct",
            )

        # 规则 4: 连续 3 天无红队进化
        if state.days_since_last_evolution > 3:
            _add(
                f"已 {state.days_since_last_evolution:.0f} 天未运行红队进化",
                "运行红队进化测试: 生成新攻击用例并验证现有防御",
                ValueTier.EVOLUTION,
                f"连续 {state.days_since_last_evolution:.0f} 天无红队进化",
                metric="days_since_last_evolution",
            )

        # 规则 5: 高成功率技能 7 天未使用
        if state.high_skill_available and state.high_skill_days_unused > 7:
            _add(
                "有高成功率技能超过7天未使用",
                "复用高成功率技能: 检查历史成功经验并应用到当前任务",
                ValueTier.HARMONY,
                f"高成功率技能 {state.high_skill_days_unused:.0f} 天未使用",
                metric="high_skill_days_unused",
            )

        # 规则 6: 回归通过率 < 80%（与 redteam 不同来源）
        if (state.last_regression_pass_rate < 80
                and state.redteam_pass_rate >= 80):
            _add(
                f"回归测试通过率 {state.last_regression_pass_rate:.0f}%，需排查",
                f"排查回归测试失败: 通过率 {state.last_regression_pass_rate:.0f}%",
                ValueTier.EVOLUTION,
                f"回归通过率 {state.last_regression_pass_rate:.0f}% < 80%",
                metric="last_regression_pass_rate",
            )

        # 规则 7: 健康状况良好 → 表达级目标
        if not goals:
            _add(
                "系统状态良好，暂无目标",
                "系统状态良好，暂无自动目标",
                ValueTier.EXPRESSION,
                "所有健康指标正常",
                metric="all_healthy",
            )

        self._goal_history = goals
        logger.info(
            "[GoalEngine] 推导出 %d 个目标: %s",
            len(goals), [g.description[:30] for g in goals],
        )
        return goals

    def prioritize_goals(self, goals: list[Goal]) -> list[Goal]:
        """用宇宙透镜排序 + 去重。"""
        if not goals:
            return goals
        goals.sort(key=lambda g: (g.priority.value, g.created_at))
        return goals

    def get_active_goals(self) -> list[Goal]:
        """获取当前活跃目标列表。"""
        return self.prioritize_goals(self._goal_history)

    def get_stats(self) -> dict:
        return {
            "total_goals_generated": len(self._goal_history),
            "active_goals": len([g for g in self._goal_history if not g.completed]),
            "environment": self.scan_environment().__dict__,
        }
