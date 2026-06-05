"""Entropy Runtime · 路由策略动态调权
v7.2 Phase 4.3: 根据历史延迟、成本、成功率动态调整 Agent 选择权重。

核心:
  1. RouteMetrics — 记录每次路由结果
  2. AgentScore — 综合评分: 成功率×0.5 + 速度×0.3 + 成本×0.2
  3. RouteOptimizer — 动态推荐 + 权重更新

Agent 可选值: "hermes", "pydanticai", "autogpt"
"""
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger("entropyruntime.route_optimizer")

# ========== 常量 ==========

ROUTE_DB = "/var/lib/entropyguard/route_metrics.json"
DEFAULT_AGENTS = ["hermes", "pydanticai", "autogpt"]

# 评分权重
W_SUCCESS = 0.5
W_SPEED = 0.3
W_COST = 0.2

# 贝叶斯平滑参数
BAYESIAN_PRIOR_RATE = 0.5      # 先验成功率
BAYESIAN_PRIOR_COUNT = 2        # 先验样本数（弱先验）
BAYESIAN_PRIOR_DURATION = 15.0  # 先验平均耗时
BAYESIAN_PRIOR_COST = 1000      # 先验 token 消耗

# 推荐配置
MIN_SAMPLES_FOR_RECOMMEND = 3   # 最少 3 条记录才参与推荐
RECENT_WINDOW = 100             # 只考虑最近 100 条记录


# ========== 数据模型 ==========

@dataclass
class RouteRecord:
    """单次路由调用记录"""
    intent: str
    intent_type: str              # 推断的任务类型
    agent: str
    success: bool
    duration: float = 0.0
    token_cost: int = 0
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "intent": self.intent[:100],
            "intent_type": self.intent_type,
            "agent": self.agent,
            "success": self.success,
            "duration": self.duration,
            "token_cost": self.token_cost,
            "timestamp": self.timestamp,
        }


@dataclass
class AgentScore:
    """Agent 在当前意图类型上的综合评分"""
    agent: str
    success_rate: float
    avg_duration: float
    avg_cost: float
    total_calls: int
    composite_score: float = 0.0  # 综合评分

    def compute(self):
        """计算综合评分。"""
        # 速度评分：耗时越低越高（15s = 0.5, 5s = 0.83, 30s = 0.17）
        speed_score = 1.0 - min(self.avg_duration / 30.0, 1.0) * 0.5
        # 成本评分：token 越少越高
        cost_score = 1.0 - min(self.avg_cost / 2000.0, 1.0) * 0.5
        self.composite_score = round(
            self.success_rate * W_SUCCESS
            + speed_score * W_SPEED
            + cost_score * W_COST,
            4,
        )
        return self.composite_score


@dataclass
class AgentRecommendation:
    """Agent 推荐结果"""
    agent: str
    composite_score: float
    success_rate: float
    avg_duration: float
    total_samples: int
    alternative: Optional[str] = None   # 备选 Agent


# ========== 路由指标存储 ==========

class RouteMetrics:
    """持久化存储路由指标。"""

    def __init__(self, db_path: str = ROUTE_DB):
        self.db_path = db_path
        self._records: list[dict] = []
        self._load()

    def _load(self):
        if os.path.exists(self.db_path):
            try:
                with open(self.db_path) as f:
                    self._records = json.load(f)
            except (json.JSONDecodeError, OSError):
                self._records = []
        # 只保留最近 RECENT_WINDOW 条
        if len(self._records) > RECENT_WINDOW:
            self._records = self._records[-RECENT_WINDOW:]

    def _save(self):
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        # 只保留最近 RECENT_WINDOW 条
        recent = self._records[-RECENT_WINDOW:] if len(self._records) > RECENT_WINDOW else self._records
        with open(self.db_path, "w") as f:
            json.dump(recent, f, indent=2)

    def record(self, record: RouteRecord):
        """记录一次路由结果。"""
        self._records.append(record.to_dict())
        self._save()
        logger.debug(
            "[RouteMetrics] %s → %s: %s (%.1fs)",
            record.intent_type, record.agent,
            "✅" if record.success else "❌", record.duration,
        )

    def record_route(self, intent: str, agent: str, success: bool,
                     duration: float = 0.0, token_cost: int = 0,
                     intent_type: Optional[str] = None):
        """便捷方法记录路由。"""
        if not intent_type:
            intent_type = self._infer_intent_type(intent)
        self.record(RouteRecord(
            intent=intent, intent_type=intent_type,
            agent=agent, success=success,
            duration=duration, token_cost=token_cost,
        ))

    def get_records(self, intent_type: Optional[str] = None,
                    limit: int = RECENT_WINDOW) -> list[dict]:
        """获取路由记录。"""
        if intent_type:
            filtered = [r for r in self._records
                       if r.get("intent_type") == intent_type]
        else:
            filtered = list(self._records)
        return filtered[-limit:]

    def _infer_intent_type(self, intent: str) -> str:
        """从 intent 推断任务类型。"""
        intent_lower = intent.lower()
        patterns = [
            (["scan", "扫描", "port", "端口", "nmap"], "端口扫描"),
            (["security", "安全", "vuln", "漏洞", "cve"], "安全扫描"),
            (["code", "代码", "review", "审查"], "代码分析"),
            (["file", "文件", "cat", "ls", "read"], "文件操作"),
            (["shell", "bash", "命令", "execute"], "Shell执行"),
            (["dependency", "依赖", "pip", "safety"], "依赖检查"),
            (["audit", "审计", "log", "日志"], "审计检查"),
            (["performance", "性能"], "性能分析"),
            (["report", "报告", "summary"], "报告生成"),
            (["test", "测试"], "测试执行"),
        ]
        for keywords, ttype in patterns:
            if any(kw in intent_lower for kw in keywords):
                return ttype
        return "通用任务"


# ========== 动态权重引擎 ==========

class RouteOptimizer:
    """路由优化器 — 基于历史数据动态调整 Agent 选择权重。"""

    def __init__(self, metrics: Optional[RouteMetrics] = None):
        self.metrics = metrics or RouteMetrics()

    def get_best_agent(self, intent: str,
                       available_agents: list[str] = None) -> Optional[AgentRecommendation]:
        """根据历史数据推荐最佳 Agent。

        Args:
            intent: 任务 intent
            available_agents: 可选 Agent 列表（默认全部）

        Returns:
            AgentRecommendation 或 None（无数据时）
        """
        if available_agents is None:
            available_agents = DEFAULT_AGENTS

        intent_type = self.metrics._infer_intent_type(intent)
        records = self.metrics.get_records(intent_type)

        if not records:
            # 尝试全局数据
            records = self.metrics.get_records()

        if not records:
            return None

        # 按 Agent 分组统计
        agent_stats: dict[str, dict] = {}
        for r in records:
            agent = r.get("agent", "")
            if agent not in available_agents:
                continue
            stats = agent_stats.setdefault(agent, {
                "total": 0, "success": 0,
                "durations": [], "costs": [],
            })
            stats["total"] += 1
            if r.get("success", False):
                stats["success"] += 1
            stats["durations"].append(r.get("duration", 0))
            stats["costs"].append(r.get("token_cost", 0))

        if not agent_stats:
            return None

        # 计算贝叶斯平滑评分
        scores = []
        for agent, stats in agent_stats.items():
            total = stats["total"]
            successes = stats["success"]

            # 贝叶斯平均
            bayes_rate = (successes + BAYESIAN_PRIOR_RATE * BAYESIAN_PRIOR_COUNT) / (
                total + BAYESIAN_PRIOR_COUNT
            )
            avg_dur = (
                sum(stats["durations"]) + BAYESIAN_PRIOR_DURATION * BAYESIAN_PRIOR_COUNT
            ) / (total + BAYESIAN_PRIOR_COUNT)
            avg_cost = (
                sum(stats["costs"]) + BAYESIAN_PRIOR_COST * BAYESIAN_PRIOR_COUNT
            ) / (total + BAYESIAN_PRIOR_COUNT)

            as_ = AgentScore(
                agent=agent,
                success_rate=bayes_rate,
                avg_duration=avg_dur,
                avg_cost=avg_cost,
                total_calls=total,
            )
            as_.compute()
            scores.append(as_)

        if not scores:
            return None

        # 综合评分排序
        scores.sort(key=lambda s: s.composite_score, reverse=True)

        best = scores[0]
        alt = scores[1] if len(scores) > 1 else None

        logger.info(
            "[RouteOptimizer] 推荐 %s (%.3f) 基于 %d 条记录, 备选 %s",
            best.agent, best.composite_score, best.total_calls,
            alt.agent if alt else "无",
        )

        return AgentRecommendation(
            agent=best.agent,
            composite_score=best.composite_score,
            success_rate=best.success_rate,
            avg_duration=best.avg_duration,
            total_samples=best.total_calls,
            alternative=alt.agent if alt else None,
        )

    def update_weights(self) -> dict[str, float]:
        """基于最近 100 条记录重新计算各 Agent 权重。

        Returns:
            {agent: weight} 权重映射
        """
        records = self.metrics.get_records()
        if not records:
            return {a: 1.0 / len(DEFAULT_AGENTS) for a in DEFAULT_AGENTS}

        agent_stats: dict[str, dict] = {}
        for r in records:
            agent = r.get("agent", "")
            stats = agent_stats.setdefault(agent, {
                "total": 0, "success": 0, "durations": [], "costs": [],
            })
            stats["total"] += 1
            if r.get("success", False):
                stats["success"] += 1
            stats["durations"].append(r.get("duration", 0))
            stats["costs"].append(r.get("token_cost", 0))

        scores = []
        for agent, stats in agent_stats.items():
            bayes_rate = (stats["success"] + BAYESIAN_PRIOR_RATE * BAYESIAN_PRIOR_COUNT) / (
                stats["total"] + BAYESIAN_PRIOR_COUNT
            )
            avg_dur = (
                sum(stats["durations"]) + BAYESIAN_PRIOR_DURATION * BAYESIAN_PRIOR_COUNT
            ) / (stats["total"] + BAYESIAN_PRIOR_COUNT)
            avg_cost = (
                sum(stats["costs"]) + BAYESIAN_PRIOR_COST * BAYESIAN_PRIOR_COUNT
            ) / (stats["total"] + BAYESIAN_PRIOR_COUNT)

            speed_score = 1.0 - min(avg_dur / 30.0, 1.0) * 0.5
            cost_score = 1.0 - min(avg_cost / 2000.0, 1.0) * 0.5
            composite = round(
                bayes_rate * W_SUCCESS + speed_score * W_SPEED + cost_score * W_COST, 4
            )
            scores.append((agent, composite))

        # 归一化权重
        total_score = sum(s for _, s in scores)
        weights = {a: round(s / total_score, 4) for a, s in scores} if total_score > 0 else {}

        # 补全未出现过的 Agent（最小权重）
        for a in DEFAULT_AGENTS:
            if a not in weights:
                weights[a] = 0.05

        # 重新归一化（补全后总和可能 > 1）
        total_w = sum(weights.values())
        if total_w > 0:
            weights = {a: round(w / total_w, 4) for a, w in weights.items()}

        logger.info(
            "[RouteOptimizer] 权重更新: %s (基于 %d 条记录)",
            {k: round(v, 3) for k, v in weights.items()},
            len(records),
        )
        return weights

    def get_stats(self) -> dict:
        """获取路由统计概览。"""
        records = self.metrics.get_records()
        agent_counts = {}
        for r in records:
            agent = r.get("agent", "?")
            agent_counts[agent] = agent_counts.get(agent, 0) + 1
        return {
            "total_records": len(records),
            "agent_distribution": agent_counts,
            "weights": self.update_weights(),
        }
