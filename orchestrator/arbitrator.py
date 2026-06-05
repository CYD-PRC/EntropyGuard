"""Entropy Runtime · 多 Agent 协商仲裁器 [已弃用]
Phase 5 Module 3: 同一任务多 Agent 并行执行 + 评分选优 + 多数决。

⚠️ 已弃用 (v8.0): 新架构中 AutoGPT 负责规划，Hermes 负责执行，
    不再有多 Agent 竞赛。保留代码用于兼容旧版调用。
    等价功能由 PlannerGateway + FeedbackLoop 替代。

流程: arbitrate(task, agents) → ArbitrationResult → 选 winner → 记录到 memory
"""
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult

logger = logging.getLogger("entropyruntime.arbitrator")


class Consensus(Enum):
    HIGH = "high"        # 全部一致
    MEDIUM = "medium"    # 2/3 一致
    LOW = "low"          # 全部不同


@dataclass
class AgentOpinion:
    """单个 Agent 对任务的意见"""
    agent: str
    output: str
    duration: float
    confidence: float = 0.5        # Agent 自报置信度 (0-1)
    completeness: float = 0.0      # 完成度 (0-1)
    consistency: float = 0.0       # 一致性 (0-1)
    efficiency: float = 0.0        # 效率 (0-1)
    composite_score: float = 0.0   # 综合评分


@dataclass
class ArbitrationResult:
    """仲裁结果"""
    task_intent: str
    opinions: list[AgentOpinion]
    winner: str
    winner_score: float
    consensus: Consensus
    average_score: float
    requires_human_review: bool = False


# 评分权重
W_COMPLETENESS = 0.4
W_CONSISTENCY = 0.3
W_EFFICIENCY = 0.2
W_CONFIDENCE = 0.1


class Arbitrator:
    """多 Agent 协商仲裁器。"""

    def __init__(self):
        self._log: list[dict] = []

    def arbitrate(
        self, task_intent: str,
        agent_list: Optional[list[str]] = None,
    ) -> ArbitrationResult:
        """对同一任务并行征求多个 Agent 意见并仲裁。

        Args:
            task_intent: 任务描述
            agent_list: Agent 列表（默认 hermes + pydanticai）

        Returns:
            ArbitrationResult
        """
        if agent_list is None:
            agent_list = ["hermes", "pydanticai"]

        opinions: list[AgentOpinion] = []
        for agent in agent_list:
            opinion = self._query_agent(agent, task_intent)
            opinions.append(opinion)

        # 评分
        for op in opinions:
            op.composite_score = (
                op.completeness * W_COMPLETENESS
                + op.consistency * W_CONSISTENCY
                + op.efficiency * W_EFFICIENCY
                + op.confidence * W_CONFIDENCE
            )

        # 选 winner
        opinions.sort(key=lambda o: o.composite_score, reverse=True)
        winner = opinions[0]
        avg_score = sum(o.composite_score for o in opinions) / max(len(opinions), 1)

        # 一致性判断
        consensus = self._check_consensus(opinions)

        result = ArbitrationResult(
            task_intent=task_intent,
            opinions=opinions,
            winner=winner.agent,
            winner_score=winner.composite_score,
            consensus=consensus,
            average_score=avg_score,
            requires_human_review=(consensus == Consensus.LOW),
        )

        # 记录到日志
        self._log_entry(result)

        # 记录到 memory_store 用于路由优化
        self._record_to_memory(result)

        logger.info(
            "[Arbitrator] %s 仲裁: winner=%s (%.3f), consensus=%s, agents=%s",
            task_intent[:40], winner.agent, winner.composite_score,
            consensus.value, [o.agent for o in opinions],
        )
        return result

    def _query_agent(self, agent: str, intent: str) -> AgentOpinion:
        """向单个 Agent 查询对任务的意见。"""
        t0 = time.time()
        try:
            from orchestrator.execute import execute
            task = AgentTask(
                id=f"arb-{agent}-{int(time.time())}",
                intent=intent, description=intent,
                assigned_agent=agent, gear=3,
            )
            result = execute(task)
            elapsed = time.time() - t0

            opinion = AgentOpinion(
                agent=agent,
                output=result.output or result.error or "",
                duration=elapsed,
            )

            if result.success and result.output:
                opinion.completeness = min(1.0, len(result.output) / 100.0)
                opinion.confidence = 0.8 if len(result.output) > 50 else 0.5
            else:
                opinion.completeness = 0.1
                opinion.confidence = 0.2

            # 一致性：用关键词覆盖率近似
            keywords = set(intent.lower().split())
            output_lower = (result.output or "").lower()
            if keywords:
                hits = sum(1 for kw in keywords if kw in output_lower)
                opinion.consistency = min(1.0, hits / max(len(keywords), 1))
            else:
                opinion.consistency = 0.5

            # 效率：越快越高（5s=1.0, 30s=0.0）
            opinion.efficiency = max(0.0, 1.0 - elapsed / 30.0)

            return opinion

        except Exception as e:
            elapsed = time.time() - t0
            logger.warning("[Arbitrator] %s 查询失败: %s", agent, e)
            return AgentOpinion(
                agent=agent, output=str(e), duration=elapsed,
                completeness=0.0, consistency=0.0,
                efficiency=max(0.0, 1.0 - elapsed / 30.0),
                confidence=0.0,
            )

    def _check_consensus(self, opinions: list[AgentOpinion]) -> Consensus:
        """检查各 Agent 结果的一致性。"""
        if len(opinions) <= 1:
            return Consensus.HIGH

        # 输出文本相似度（简化版 Jaccard）
        outputs = [set(o.output.lower().split()) for o in opinions]
        if len(outputs) < 2:
            return Consensus.HIGH

        # 两两计算 Jaccard 相似度
        similarities = []
        for i in range(len(outputs)):
            for j in range(i + 1, len(outputs)):
                intersection = len(outputs[i] & outputs[j])
                union = len(outputs[i] | outputs[j])
                if union > 0:
                    similarities.append(intersection / union)

        if not similarities:
            return Consensus.LOW

        avg_sim = sum(similarities) / len(similarities)

        if avg_sim > 0.6:
            return Consensus.HIGH
        elif avg_sim > 0.3:
            return Consensus.MEDIUM
        else:
            return Consensus.LOW

    def _log_entry(self, result: ArbitrationResult):
        entry = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "task_intent": result.task_intent[:80],
            "winner": result.winner,
            "winner_score": result.winner_score,
            "consensus": result.consensus.value,
            "opinions": [
                {"agent": o.agent, "score": o.composite_score}
                for o in result.opinions
            ],
        }
        self._log.append(entry)

    def _record_to_memory(self, result: ArbitrationResult):
        """将仲裁结果记录到 memory_store（用于路由优化）。"""
        try:
            from orchestrator.memory_store import MemoryStore
            store = MemoryStore()
            # winner +1
            store.save_episode(
                task_id=f"arb-win-{int(time.time())}",
                agent=result.winner,
                intent=f"arbitration: {result.task_intent[:80]}",
                output=json.dumps({
                    "score": result.winner_score,
                    "consensus": result.consensus.value,
                }, ensure_ascii=False),
                success=True,
                metadata={"arbitration": True},
            )
            store.close()
        except Exception as e:
            logger.warning("[Arbitrator] memory 记录失败: %s", e)

    def get_log(self) -> list[dict]:
        return self._log[-100:]
