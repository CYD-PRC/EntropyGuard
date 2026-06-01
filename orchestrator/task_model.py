"""
Entropy Runtime Orchestrator · 任务模型
定义 Agent 任务和 Orchestrator 结果的标准数据模型。
"""
from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field


class AgentTask(BaseModel):
    """Orchestrator 拆解出的单个子任务"""
    id: str = Field(..., description="任务唯一标识，如 task-001")
    source: str = Field(default="orchestrator", description="任务来源标识")
    intent: str = Field(..., description="任务意图描述（自然语言）")
    payload: dict[str, Any] = Field(default_factory=dict, description="任务负载参数")
    requires_approval: bool = Field(default=False, description="是否需要人工审批")
    parent_task_id: Optional[str] = Field(default=None, description="父任务 ID（用于嵌套分解）")
    priority: int = Field(default=5, ge=1, le=10, description="优先级 1-10，数字越小优先级越高")
    assigned_agent: Optional[str] = Field(default=None, description="路由分配的 Agent 名称")
    gear: int = Field(default=3, ge=1, le=4, description="执行档位")
    model_id: str = Field(default="kimi", description="指定模型 ID")
    created_at: str = Field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")


class TaskResult(BaseModel):
    """子任务执行结果（与 interfaces/agent_adapter.py 的 TaskResult 对齐）"""
    task_id: str = Field(..., description="对应 AgentTask.id")
    success: bool = Field(default=False)
    output: str = Field(default="")
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    error: Optional[str] = Field(default=None)
    agent: Optional[str] = Field(default=None, description="实际执行的 Agent 名称")
    gear: int = Field(default=3)
    validation_status: Optional[str] = Field(default=None)
    elapsed_seconds: float = Field(default=0.0)
    audit_event_id: Optional[int] = Field(default=None, description="审计链事件 ID")


class OrchestratorResult(BaseModel):
    """Orchestrator 完整执行结果"""
    goal: str = Field(..., description="原始用户目标")
    tasks: list[AgentTask] = Field(default_factory=list, description="拆解出的子任务列表")
    results: list[TaskResult] = Field(default_factory=list, description="各子任务执行结果")
    summary: str = Field(default="", description="汇总摘要（由 merge 生成）")
    total_time: float = Field(default=0.0, description="总耗时（秒）")
    success: bool = Field(default=False, description="全部成功为 True，部分失败为 False")
    conflict_resolved: list[str] = Field(default_factory=list, description="已解决的冲突记录")
