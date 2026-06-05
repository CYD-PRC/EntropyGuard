"""
Entropy Runtime · Planner Gateway (AutoGPT 规划层)
v8.0: AutoGPT 只负责思考和规划，不执行任何操作。
      通过 MessageBoard 与 Hermes Executor 通信。

4 个核心方法:
  1. analyze_goal(user_input)      → GoalAnalysis
  2. decompose_task(goal_analysis) → TaskGraph
  3. route_plan(task_graph)        → RouteTable
  4. generate_execution_plan       → ExecutionPlan

AutoGPT 完全不接触实际执行，只通过这个网关输出规划。
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
from orchestrator.cosmic_lense import CosmicLense, ValueTier, PriorityResult, ConflictResult
from orchestrator.metacognition import Metacognition
from orchestrator.checkpoint import save_checkpoint, clear_checkpoint

logger = logging.getLogger("entropyruntime.planner_gateway")

# ========== 枚举 ==========

class ToolType(Enum):
    """工具类型 — Hermes 可调用的工具清单"""
    NMAP_SCAN = "nmap_scan"
    BANDIT_SCAN = "bandit_scan"
    CURL_REQUEST = "curl_request"
    SAFETY_CHECK = "safety_check"
    DIRECTORY_SCAN = "directory_scan"
    PYDANTICAI_EXTRACT = "pydanticai_extract"
    SHELL_COMMAND = "shell_command"
    FILE_ANALYSIS = "file_analysis"
    REPORT_GEN = "report_gen"
    SANDBOX_EXEC = "sandbox_exec"


# ========== 数据模型 ==========

@dataclass
class GoalAnalysis:
    """AutoGPT 对用户输入的分析结果"""
    raw_input: str
    cleaned_intent: str
    cosmic_tier: ValueTier
    cosmic_score: int
    constraint: str = ""          # 约束条件描述
    risk_level: str = "low"       # low / medium / high / critical
    requires_approval: bool = False
    reasoning: str = ""
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "raw_input": self.raw_input[:100],
            "cleaned_intent": self.cleaned_intent,
            "cosmic_tier": str(self.cosmic_tier),
            "cosmic_score": self.cosmic_score,
            "constraint": self.constraint,
            "risk_level": self.risk_level,
            "requires_approval": self.requires_approval,
            "reasoning": self.reasoning[:200],
            "timestamp": self.timestamp,
        }


@dataclass
class ToolAssignment:
    """子任务的工具路由"""
    task_id: str
    intent: str
    tool: ToolType
    tool_params: dict = field(default_factory=dict)
    priority: int = 5
    dependencies: list[str] = field(default_factory=list)
    fallback_tool: Optional[ToolType] = None
    acceptance_criteria: list[str] = field(default_factory=list)


@dataclass
class TaskGraph:
    """DAG 子任务依赖图"""
    goal: str
    nodes: list[ToolAssignment] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from, to)
    compute_order: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "goal": self.goal[:100],
            "nodes": [{"id": n.task_id, "tool": n.tool.value, "deps": n.dependencies[:3]}
                      for n in self.nodes],
            "edges": self.edges,
            "compute_order": self.compute_order,
        }


@dataclass
class RouteTable:
    """工具路由表 — 每个子任务对应一个工具"""
    assignments: list[ToolAssignment] = field(default_factory=list)
    execution_strategy: str = "sequential"  # sequential / parallel_batches
    fallback_plan: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return {
            "assignments": [
                {"id": a.task_id, "tool": a.tool.value,
                 "fallback": a.fallback_tool.value if a.fallback_tool else None}
                for a in self.assignments
            ],
            "strategy": self.execution_strategy,
            "fallback": self.fallback_plan[:200],
        }


@dataclass
class ExecutionPlan:
    """可执行的执行计划书"""
    plan_id: str
    goal: str
    route_table: RouteTable
    acceptance_criteria: list[str] = field(default_factory=list)
    max_iterations: int = 3
    checkpoints_enabled: bool = True
    security_level: str = "standard"
    created_at: float = field(default_factory=time.time)


# ========== 规划网关 ==========

PLANNING_SYSTEM_PROMPT = """你是一个高级安全任务规划专家（AutoGPT Planner）。
你的职责：分析用户输入，拆解为有序的任务序列，为每个任务选择最佳工具。

核心原则：
1. 你只负责规划，绝不执行任何操作
2. 你的输出是结构化的任务清单和工具路由
3. 严格执行安全分级（生存 > 进化 > 和谐 > 表达）

可用工具清单：
- nmap_scan: 端口扫描和服务探测
- bandit_scan: Python 代码安全扫描
- curl_request: HTTP 请求测试
- safety_check: Python 依赖安全检查
- directory_scan: 目录结构分析和文件浏览
- pydanticai_extract: 结构化数据提取和文本分析
- shell_command: Shell 命令执行
- file_analysis: 文件内容分析
- report_gen: 报告生成
- sandbox_exec: 沙箱隔离执行（危险操作）

输出格式：JSON 数组，每个元素：
{"task_id": "唯一ID", "intent": "指令", "tool": "tool_name",
 "dependencies": [], "priority": 1-10}
"""


class PlannerGateway:
    """AutoGPT 规划网关 — 对接 LLM 作为规划引擎，不执行任何操作。"""

    def __init__(self, use_autogpt_daemon: bool = False):
        self.lense = CosmicLense()
        self.meta = Metacognition()
        self._plan_history: list[dict] = []
        # 如果 use_autogpt_daemon=True, 尝试通过 AutoGPT 容器 API 规划
        # 默认使用 Direct DeepSeek API 规划（AutoGPT 角色由 LLM 扮演）
        self.autogpt_api = None
        if use_autogpt_daemon:
            self.autogpt_api = "http://127.0.0.1:5000/api/planning"

    # ===== 1. 意图分析 =====

    def analyze_goal(self, user_input: str) -> GoalAnalysis:
        """分析用户输入：调用宇宙透镜评估优先级 + 安全约束检测。"""
        t0 = time.time()
        logger.info("[PlannerGateway] 分析目标: %s", user_input[:80])

        # 用宇宙透镜评估
        dummy_task = AgentTask(
            id="dummy-goal",
            intent=user_input,
            description=user_input[:80],
        )
        priority: PriorityResult = self.lense.evaluate(dummy_task)

        # 冲突检测
        conflict: ConflictResult = self.lense.conflict_detect(dummy_task)

        # 安全约束判断
        requires_approval = False
        constraint = ""
        risk_level = priority.tier_name_cn
        destructive_keywords = [
            "rm -rf", "删除文件", "格式化", "kill -9", "dd if=",
            "DROP TABLE", "TRUNCATE", "覆盖写入",
        ]
        for kw in destructive_keywords:
            if kw.lower() in user_input.lower():
                requires_approval = True
                constraint = f"检测到破坏性关键词 '{kw}'，需要审批"
                break

        # 构建推理链
        reasoning = (
            f"宇宙透镜判定: {priority.tier_name_cn}级 "
            f"(score={priority.priority_score})\n"
            f"{priority.reason[:100]}"
        )
        if conflict.has_conflict:
            reasoning += f"\n冲突警告: {conflict.conflict_reason[:80]}"

        analysis = GoalAnalysis(
            raw_input=user_input,
            cleaned_intent=user_input,
            cosmic_tier=priority.tier,
            cosmic_score=priority.priority_score,
            constraint=constraint,
            risk_level=risk_level,
            requires_approval=requires_approval,
            reasoning=reasoning,
        )

        logger.info(
            "[PlannerGateway] 分析完成: %s级, %s, %.2fs",
            analysis.cosmic_tier, "需审批" if requires_approval else "无需审批",
            time.time() - t0,
        )
        return analysis

    # ===== 2. 任务拆解 =====

    def decompose_task(self, analysis: GoalAnalysis) -> TaskGraph:
        """将目标拆解为 DAG 任务图。"""
        t0 = time.time()
        logger.info("[PlannerGateway] 拆解目标: %s", analysis.cleaned_intent[:60])

        # 复用 decompose.py 的四层降级拆解
        from orchestrator.decompose import decompose

        raw_tasks = decompose(analysis.cleaned_intent)
        if not raw_tasks:
            logger.warning("[PlannerGateway] decompose 返回空，使用本地规则")
            raw_tasks = self._fallback_decompose(analysis)

        # 将 AgentTask 的 assigned_agent 映射为 ToolType
        nodes = []
        for t in raw_tasks:
            tool = self._agent_to_tool(t.assigned_agent or "hermes", t.intent)
            params = {"gear": t.gear, "model_id": t.model_id}
            if t.payload.get("target_path"):
                params["target_path"] = t.payload["target_path"]
            if t.payload.get("urls"):
                params["urls"] = t.payload["urls"]

            # 生成验收标准
            criteria = self._generate_acceptance_criteria(tool, t.intent)

            assignment = ToolAssignment(
                task_id=t.id,
                intent=t.intent,
                tool=tool,
                tool_params=params,
                priority=t.priority,
                dependencies=t.dependencies,
                fallback_tool=self._get_fallback(tool),
                acceptance_criteria=criteria,
            )
            nodes.append(assignment)

        # 构建 edges
        edges = []
        for n in nodes:
            for d in n.dependencies:
                edges.append((d, n.task_id))

        # 计算执行顺序（拓扑排序）
        order = self._topological_sort(nodes)

        graph = TaskGraph(
            goal=analysis.cleaned_intent,
            nodes=nodes,
            edges=edges,
            compute_order=order,
        )

        logger.info(
            "[PlannerGateway] 拆解完成: %d 个子任务, %.2fs",
            len(nodes), time.time() - t0,
        )
        return graph

    # ===== 3. 工具路由 =====

    def route_plan(self, graph: TaskGraph) -> RouteTable:
        """为 TaskGraph 中的每个节点分配工具和执行策略。"""
        t0 = time.time()
        logger.info("[PlannerGateway] 路由规划: %s", graph.goal[:60])

        # 宇宙透镜重新排序
        prioritized = self._cosmic_sort(graph.nodes)

        # 判断执行策略
        strategy = self._decide_strategy(prioritized)

        # 构建回退方案
        fallback = self._build_fallback(prioritized)

        table = RouteTable(
            assignments=prioritized,
            execution_strategy=strategy,
            fallback_plan=fallback,
        )

        logger.info(
            "[PlannerGateway] 路由完成: %s策略, %.2fs",
            strategy, time.time() - t0,
        )
        return table

    # ===== 4. 执行计划生成 =====

    def generate_execution_plan(self, route: RouteTable, goal: str) -> ExecutionPlan:
        """生成最终执行计划书，含 checkpoint 设置和验收标准。"""
        plan_id = f"plan-{int(time.time())}"

        # 全局验收标准
        global_criteria = [
            "所有子任务执行无错误",
            "安全校验全部通过",
            "结果写入 MessageBoard",
        ]

        plan = ExecutionPlan(
            plan_id=plan_id,
            goal=goal,
            route_table=route,
            acceptance_criteria=global_criteria,
            max_iterations=3,
            checkpoints_enabled=True,
        )

        logger.info("[PlannerGateway] 计划已生成: %s (%d 任务)", plan_id, len(route.assignments))
        return plan

    # ===== 内部方法 =====

    def _agent_to_tool(self, agent: str, intent: str) -> ToolType:
        """将旧版 Agent 名映射为新版 ToolType。"""
        intent_lower = intent.lower()
        agent_lower = agent.lower()

        # 由旧 agent 名推断
        if "hermes" in agent_lower:
            # hermes 可以执行多种工具，根据关键词选择
            if any(kw in intent_lower for kw in ["nmap", "port", "端口", "扫描"]):
                return ToolType.NMAP_SCAN
            elif any(kw in intent_lower for kw in ["bandit", "安全扫描", "静态分析"]):
                return ToolType.BANDIT_SCAN
            elif any(kw in intent_lower for kw in ["curl", "http", "请求", "端点"]):
                return ToolType.CURL_REQUEST
            elif any(kw in intent_lower for kw in ["safety", "pip", "依赖"]):
                return ToolType.SAFETY_CHECK
            elif any(kw in intent_lower for kw in ["shell", "bash", "命令", "执行"]):
                return ToolType.SHELL_COMMAND
            elif any(kw in intent_lower for kw in ["sandbox", "沙箱", "隔离"]):
                return ToolType.SANDBOX_EXEC
            else:
                return ToolType.DIRECTORY_SCAN
        elif "pydanticai" in agent_lower:
            return ToolType.PYDANTICAI_EXTRACT
        elif "autogpt" in agent_lower:
            # AutoGPT 不再执行，分配给她分析或报告
            return ToolType.FILE_ANALYSIS
        else:
            return ToolType.DIRECTORY_SCAN

    def _get_fallback(self, tool: ToolType) -> Optional[ToolType]:
        """为工具类型提供回退方案。"""
        fallback_map = {
            ToolType.NMAP_SCAN: ToolType.DIRECTORY_SCAN,
            ToolType.BANDIT_SCAN: ToolType.FILE_ANALYSIS,
            ToolType.CURL_REQUEST: ToolType.DIRECTORY_SCAN,
            ToolType.SAFETY_CHECK: ToolType.SHELL_COMMAND,
            ToolType.PYDANTICAI_EXTRACT: ToolType.DIRECTORY_SCAN,
            ToolType.SANDBOX_EXEC: ToolType.SHELL_COMMAND,
            ToolType.SHELL_COMMAND: ToolType.DIRECTORY_SCAN,
            ToolType.FILE_ANALYSIS: ToolType.DIRECTORY_SCAN,
            ToolType.REPORT_GEN: ToolType.PYDANTICAI_EXTRACT,
            ToolType.DIRECTORY_SCAN: None,
        }
        return fallback_map.get(tool)

    def _generate_acceptance_criteria(self, tool: ToolType, intent: str) -> list[str]:
        """为工具任务生成验收标准。"""
        base = ["任务执行无错误", "输出非空"]
        tool_specific = {
            ToolType.NMAP_SCAN: ["端口列表已输出", "服务版本信息包含在内"],
            ToolType.BANDIT_SCAN: ["安全扫描报告已生成", "漏洞等级已标记"],
            ToolType.CURL_REQUEST: ["HTTP 响应已收到", "状态码已输出"],
            ToolType.SAFETY_CHECK: ["依赖列表已输出", "已知漏洞已标记"],
            ToolType.PYDANTICAI_EXTRACT: ["结构化数据已提取", "结果格式为 JSON"],
            ToolType.DIRECTORY_SCAN: ["目录结构已列出", "目标文件已显示"],
            ToolType.SHELL_COMMAND: ["命令已执行", "有输出内容"],
            ToolType.SANDBOX_EXEC: ["沙箱内执行成功", "结果已回传"],
            ToolType.FILE_ANALYSIS: ["文件内容已分析", "关键信息已提取"],
            ToolType.REPORT_GEN: ["报告已生成", "包含所有关键发现"],
        }
        return base + tool_specific.get(tool, [])

    def _topological_sort(self, nodes: list[ToolAssignment]) -> list[str]:
        """拓扑排序计算执行顺序。"""
        node_map = {n.task_id: n for n in nodes}
        visited = set()
        result = []

        def visit(tid: str):
            if tid in visited:
                return
            visited.add(tid)
            node = node_map.get(tid)
            if node:
                for dep in node.dependencies:
                    visit(dep)
                result.append(tid)

        for n in nodes:
            visit(n.task_id)
        return result

    def _cosmic_sort(self, nodes: list[ToolAssignment]) -> list[ToolAssignment]:
        """按优先级排序，高优先级先执行。"""
        return sorted(nodes, key=lambda n: n.priority)

    def _decide_strategy(self, nodes: list[ToolAssignment]) -> str:
        """决定执行策略。"""
        # 如果有依赖关系，用 sequential
        if any(n.dependencies for n in nodes):
            return "sequential"
        return "parallel_batches"

    def _build_fallback(self, nodes: list[ToolAssignment]) -> str:
        """构建全局回退方案。"""
        critical = [n for n in nodes if n.priority <= 3]
        if critical:
            return (
                f"关键任务 {[n.task_id for n in critical[:3]]} 失败时 "
                f"尝试回退工具后上报"
            )
        return "全部任务均可跳过或回退"

    def _fallback_decompose(self, analysis: GoalAnalysis) -> list[AgentTask]:
        """兜底拆解（decompose 失败时使用）。"""
        # 也尝试本地规则
        from orchestrator.decompose import local_rule_decompose
        tasks = local_rule_decompose(analysis.cleaned_intent)
        if tasks:
            return tasks

        # 最终兜底：单一任务
        return [AgentTask(
            id="task-001",
            description=f"(降级) {analysis.cleaned_intent[:60]}",
            intent=analysis.cleaned_intent,
            assigned_agent="hermes",
            gear=3,
        )]

    def get_plan_history(self) -> list[dict]:
        return list(self._plan_history)

    def reset(self):
        self._plan_history.clear()
        logger.info("[PlannerGateway] 状态已重置")
