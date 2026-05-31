"""
Entropy Runtime · Agent Adapter 标准接口层

定义 Agent 适配器的抽象接口，所有 Agent 框架（AutoGPT, Claude Code, Codex 等）
通过实现此接口接入 Entropy Runtime 的权限审计体系。
"""
from abc import ABC, abstractmethod
from typing import Any, Optional
from pydantic import BaseModel


# ========== 标准返回类型 ==========

class TaskResult(BaseModel):
    """AI 任务执行结果"""
    success: bool
    output: str
    tool_calls: list[dict[str, Any]] = []
    error: Optional[str] = None


class ToolResult(BaseModel):
    """工具调用结果"""
    success: bool
    output: str = ""
    error: Optional[str] = None
    risk_level: str = "low"  # low / medium / high / critical


class AgentState(BaseModel):
    """Agent 运行时状态"""
    agent_id: str
    status: str  # idle / busy / error / blocked
    current_task: Optional[str] = None
    memory_size: int = 0


# ========== 抽象基类 ==========

class AgentAdapter(ABC):
    """
    Agent 适配器标准接口。

    所有 Agent 实现必须继承此类，实现以下四个核心方法。
    Entropy Runtime 的 Agent 端点（/api/chat, /api/autonomy, /api/multi-agent 等）
    统一通过此接口调用，不直接访问具体的 Agent 实现。
    """

    @abstractmethod
    async def run(self, task: str, context: dict) -> TaskResult:
        """
        执行一个 AI 任务。

        Args:
            task: 任务描述（自然语言）
            context: 上下文信息，包含：
                - model_id: 模型标识
                - gear: 档位等级 (1-4)
                - upgrade_retry: 是否升级后重试
                - memory_context: 历史记忆
                - actor: 执行者标识
                - session_id: 会话 ID

        Returns:
            TaskResult: 任务执行结果
        """
        ...

    @abstractmethod
    async def invoke_tool(self, tool_name: str, tool_args: dict, context: dict) -> ToolResult:
        """
        调用一个工具。

        Args:
            tool_name: 工具名称（如 run_shell, http_request）
            tool_args: 工具参数
            context: 调用上下文

        Returns:
            ToolResult: 工具执行结果
        """
        ...

    @abstractmethod
    def get_state(self) -> AgentState:
        """
        获取 Agent 当前状态。

        Returns:
            AgentState: Agent 运行时状态快照
        """
        ...

    @abstractmethod
    def supports_streaming(self) -> bool:
        """
        是否支持流式输出。

        Returns:
            bool: True 表示支持 SSE/WebSocket 流式回复
        """
        ...
