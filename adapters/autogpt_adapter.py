"""
Entropy Runtime · AutoGPT Adapter

包装 gear_aware_call 和 dispatch_tool 为 AgentAdapter 标准接口。
框架变更时，只需替换此文件中的实现逻辑，interface 和 router 代码不变。
"""
import logging
from typing import Any, Optional

from interfaces.agent_adapter import AgentAdapter, TaskResult, ToolResult, AgentState
from models import MODEL_REGISTRY, gear_aware_call
from tools import TOOL_DEFINITIONS, GEAR_TOOLS, dispatch_tool
from audit import state
from config import GEAR_MAP

logger = logging.getLogger("entropyruntime")


class AutoGPTAdapter(AgentAdapter):
    """
    基于 gear_aware_call 的 AutoGPT 适配器。

    包装 Entropy Runtime 现有的四档权限模型调用引擎，
    对外暴露标准 AgentAdapter 接口。
    """

    def __init__(self):
        self._agent_id = "autogpt-v0.4.7"

    async def run(self, task: str, context: dict) -> TaskResult:
        """
        通过 gear_aware_call 执行 AI 任务。

        context 支持以下字段:
            model_id (str): 模型标识，默认 "kimi"
            gear (int): 档位，默认 state.current_gear
            upgrade_retry (bool): 升级后重试标志
            memory_context (str): 历史记忆
            actor (str): 执行者标识
            session_id (str): 会话 ID（透传，暂未使用）
        """
        model_id = context.get("model_id", "kimi")
        gear = context.get("gear", state.current_gear)
        upgrade_retry = context.get("upgrade_retry", False)
        memory_context = context.get("memory_context")
        actor = context.get("actor", "human")

        result = await gear_aware_call(
            model_id=model_id,
            message=task,
            gear=gear,
            upgrade_retry=upgrade_retry,
            memory_context=memory_context,
            actor=actor,
        )

        return TaskResult(
            success=result.get("success", False),
            output=result.get("reply", ""),
            tool_calls=result.get("tool_calls") or [],
            error=result.get("error"),
        )

    async def invoke_tool(self, tool_name: str, tool_args: dict, context: dict) -> ToolResult:
        """
        调用单个工具。

        Args:
            tool_name: 工具名（如 run_shell, http_request）
            tool_args: 工具参数
            context: 调用上下文（含 gear 用于权限检查）
        """
        gear = context.get("gear", state.current_gear)
        gear_name = GEAR_MAP.get(gear, {}).get("name", "UNKNOWN")
        allowed_tool_names = GEAR_TOOLS.get(gear, [])

        if tool_name not in allowed_tool_names:
            return ToolResult(
                success=False,
                error=f"工具 {tool_name} 在 {gear_name} 档位不可用",
                risk_level="medium",
            )

        try:
            raw_result = await dispatch_tool(tool_name, tool_args)
            success = raw_result.get("success", False)

            # 评估风险等级
            risk = "low"
            if not success:
                risk = "medium"
            if tool_name == "run_shell" and any(
                kw in str(tool_args.get("command", "")).lower()
                for kw in ["rm ", "chmod ", "dd ", "mkfs", "> /dev/"]
            ):
                risk = "high"

            return ToolResult(
                success=success,
                output=raw_result.get("stdout", raw_result.get("output", str(raw_result))),
                error=raw_result.get("error") or raw_result.get("stderr"),
                risk_level=risk,
            )
        except Exception as e:
            logger.error(f"[AutoGPTAdapter] invoke_tool failed: {e}")
            return ToolResult(success=False, error=str(e), risk_level="medium")

    def get_state(self) -> AgentState:
        """从审计状态机获取当前 Agent 状态"""
        return AgentState(
            agent_id=self._agent_id,
            status="busy" if getattr(state, "batch_approved", False) else "idle",
            current_task=None,  # 不追踪当前任务
            memory_size=len(state.event_log),
        )

    def supports_streaming(self) -> bool:
        """当前 gear_aware_call 不支持流式输出"""
        return False
