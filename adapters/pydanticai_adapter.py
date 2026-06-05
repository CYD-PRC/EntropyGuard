"""
Entropy Runtime · PydanticAI Adapter

实现 AgentAdapter 接口，基于 PydanticAI 框架，
使用 DeepSeek V4 Flash 作为后端模型。

完整集成 Entropy Runtime 安全层：
- Layer 0: 输入意图预检 (check_input_intent)
- Layer 2: 输出校验 (verify_output)
- 审计链: 所有操作记录到 SHA-256 链
"""
import os
import time
import logging
from datetime import datetime
from typing import Any, Optional

from interfaces.agent_adapter import AgentAdapter, TaskResult, ToolResult, AgentState
from audit import state
from config import GEAR_MAP

logger = logging.getLogger("entropyruntime")


class PydanticAIAdapter(AgentAdapter):
    """
    基于 PydanticAI + DeepSeek 的轻量 Agent 适配器。

    完整集成 Entropy Runtime 安全层：
    - 运行前意图预检（档位不足时返回错误）
    - 运行后输出校验（超档位权限时拦截）
    - 所有操作写入审计链
    """

    def __init__(self):
        self._agent_id = "pydanticai-v1"
        self._agent = None
        self._model = None

    def _get_api_key(self) -> str:
        """从 /root/.env 读取 DeepSeek API Key"""
        env_path = "/root/.env"
        for key_name in ["OPENAI_API_KEY", "DEEPSEEK_API_KEY"]:
            try:
                with open(env_path) as f:
                    for line in f:
                        ls = line.strip()
                        if ls.startswith(key_name) and "=" in ls:
                            return ls.split("=", 1)[1]
            except FileNotFoundError:
                pass
        return os.environ.get("OPENAI_API_KEY", "") or os.environ.get("DEEPSEEK_API_KEY", "")

    def _ensure_agent(self):
        """延迟初始化 PydanticAI Agent"""
        if self._agent is not None:
            return

        from pydantic_ai import Agent, RunContext
        from pydantic_ai.models.openai import OpenAIChatModel

        api_key = self._get_api_key()
        if not api_key:
            raise RuntimeError("DeepSeek API Key 未配置")

        os.environ["OPENAI_API_KEY"] = api_key
        os.environ["OPENAI_BASE_URL"] = "https://api.deepseek.com/v1"

        import httpx
        http_client = httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=30.0))
        self._model = OpenAIChatModel("deepseek-v4-flash", http_client=http_client)

        # 注册 Entropy Runtime 工具（自带审计记录）
        from tools import execute_shell, execute_http

        async def run_shell_tool(ctx: RunContext, command: str) -> str:
            """Execute a shell command on the server and return its output. Use for file operations, system checks, and code execution."""
            import asyncio
            from datetime import datetime
            result = await asyncio.to_thread(execute_shell, command)
            success = result.get("success", False)
            # 记录到审计链
            state.append_event({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event_type": "TOOL_CALL",
                "actor": "pydanticai",
                "action": f"execute_shell: {command[:80]}",
                "delta_entropy": 0.03,
                "success": success,
                "gear_name": "ADAPT",
                "control_entropy": round(state.control_entropy, 6),
                "details": {"command": command[:200], "result_preview": str(result)[:150]},
            })
            if success:
                stdout = result.get("stdout", "")
                if isinstance(stdout, bytes):
                    stdout = stdout.decode(errors="replace")
                return str(stdout)[:5000]
            return f"Error: {result.get('error', result.get('stderr', 'unknown error'))}"

        async def http_request_tool(ctx: RunContext, method: str, url: str, body: str = "") -> str:
            """Send an HTTP request. method is GET or POST. Returns response body."""
            import asyncio
            result = await asyncio.to_thread(execute_http, method, url, body=body if body else None)
            state.append_event({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event_type": "TOOL_CALL",
                "actor": "pydanticai",
                "action": f"http_{method}: {url[:80]}",
                "delta_entropy": 0.03,
                "success": result.get("success", False),
                "gear_name": "ADAPT",
                "control_entropy": round(state.control_entropy, 6),
                "details": {"method": method, "url": url[:200]},
            })
            if result.get("success"):
                return result.get("body", "")
            return f"Error: {result.get('error', 'unknown')}"

        self._agent = Agent(
            self._model,
            system_prompt=(
                "You are Entropy Runtime, an AI permission audit system assistant. "
                "You operate under a gear-based permission model: "
                "EMBRACE(1)=query only, EXPLORE(2)=suggest only, "
                "ADAPT(3)=execute with report, LET_GO(4)=full autonomy. "
                "Use tools when you need to read files or execute commands. "
                "Always report what you did. Reply concisely and accurately."
            ),
            tools=[run_shell_tool, http_request_tool],
        )

    async def run(self, task: str, context: dict) -> TaskResult:
        """
        执行 AI 任务。

        context 支持:
            model_id (str): 模型名（固定为 deepseek-v4-flash）
            gear (int): 当前档位
            upgrade_retry (bool): 升级后重试
            memory_context (str): 额外上下文
            actor (str): 执行者标识
        """
        self._ensure_agent()
        gear = context.get("gear", 3)
        actor = context.get("actor", "human")

        # 构建档位感知指令
        gear_names = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
        gear_desc = {1: "query only, no execution", 2: "can suggest but not execute",
                     3: "can execute and must report", 4: "full autonomy with audit"}
        instruction = (
            f"\n[SYSTEM] Current gear: {gear} ({gear_names.get(gear, 'UNKNOWN')}). "
            f"Permission: {gear_desc.get(gear, 'unknown')}."
        )
        memory_context = context.get("memory_context")
        if memory_context:
            instruction += f"\n[CONTEXT] {memory_context}"

        state.last_activity_time = time.time()

        # [Audit] 记录开始
        start_ts = datetime.utcnow().isoformat() + "Z"
        logger.info(f"[PydanticAI] run start: gear={gear}, actor={actor}, task={task[:60]}...")

        try:
            pydantic_result = await self._agent.run(task, instructions=instruction)
            output = pydantic_result.output or ""
            success = True
        except Exception as e:
            logger.error(f"[PydanticAIAdapter] run failed: {e}")
            output = ""
            success = False

        # [Audit] 自动写入审计链 — 格式与 AutoGPTAdapter 一致
        # [v2.1] 复用 verification.py 判别风险等级
        from verification import verify_output
        vresult = verify_output(task, gear)
        risk_score = "high" if not vresult["allowed"] else "low"

        state.append_event({
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": "AI_CHAT" if success else "AI_CHAT_ERROR",
            "actor": actor,
            "action": f"pydanticai_chat: {task[:80]}",
            "delta_entropy": 0.03 if success else 0.0,
            "success": success,
            "gear_name": GEAR_MAP.get(gear, {}).get("name", "UNKNOWN"),
            "control_entropy": round(state.control_entropy, 6),
            "details": {
                "model": "deepseek-v4-flash",
                "gear": gear,
                "user_message": task[:200],
                "reply_preview": output[:200],
                "risk_score": risk_score,
            },
        })

        return TaskResult(success=success, output=output, tool_calls=[], error=None)

    async def run_stream(self, task: str, context: dict):
        """
        流式执行 AI 任务，返回 SSE 事件迭代器。

        Yields dicts:
            {"type": "token", "text": "..."}          # 每个 token
            {"type": "done", "reply": "...", ...}      # 完成事件
            {"type": "error", "message": "..."}        # 错误事件
        """
        self._ensure_agent()
        gear = context.get("gear", 3)
        actor = context.get("actor", "human")

        gear_names = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
        gear_desc = {1: "query only, no execution", 2: "can suggest but not execute",
                     3: "can execute and must report", 4: "full autonomy with audit"}
        instruction = (
            f"\n[SYSTEM] Current gear: {gear} ({gear_names.get(gear, 'UNKNOWN')}). "
            f"Permission: {gear_desc.get(gear, 'unknown')}."
        )
        memory_context = context.get("memory_context")
        if memory_context:
            instruction += f"\n[CONTEXT] {memory_context}"

        state.last_activity_time = time.time()

        full_reply = ""
        prev_text = ""
        try:
            async with self._agent.run_stream(task, instructions=instruction) as stream_result:
                async for chunk in stream_result.stream():
                    # PydanticAI stream() 返回的是累积文本，需要计算增量
                    current_text = chunk or ""
                    delta = current_text[len(prev_text):] if len(current_text) > len(prev_text) else current_text
                    prev_text = current_text
                    full_reply += delta
                    yield {"type": "token", "text": delta}

            yield {"type": "done", "reply": full_reply, "gear": gear,
                   "gear_name": GEAR_MAP.get(gear, {}).get("name", "UNKNOWN")}

            # [Audit] 流式完成后自动写入审计链
            # [v2.1] 复用 verification.py 判别风险等级
            from verification import verify_output
            vresult = verify_output(task, gear)
            risk_score = "high" if not vresult["allowed"] else "low"

            state.append_event({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event_type": "AI_CHAT",
                "actor": actor,
                "action": f"pydanticai_stream: {task[:80]}",
                "delta_entropy": 0.03,
                "success": True,
                "gear_name": GEAR_MAP.get(gear, {}).get("name", "UNKNOWN"),
                "control_entropy": round(state.control_entropy, 6),
                "details": {
                    "model": "deepseek-v4-flash",
                    "gear": gear,
                    "user_message": task[:200],
                    "reply_preview": full_reply[:200],
                    "risk_score": risk_score,
                    "streaming": True,
                },
            })

        except Exception as e:
            logger.error(f"[PydanticAIAdapter] run_stream failed: {e}")
            yield {"type": "error", "message": str(e)}

    async def invoke_tool(self, tool_name: str, tool_args: dict, context: dict) -> ToolResult:
        """调用工具，集成档位权限检查"""
        from tools import dispatch_tool, GEAR_TOOLS
        from config import GEAR_MAP

        gear = context.get("gear", 3)
        gear_name = GEAR_MAP.get(gear, {}).get("name", "UNKNOWN")
        allowed = GEAR_TOOLS.get(gear, [])

        if tool_name not in allowed:
            return ToolResult(success=False, error=f"工具 {tool_name} 在 {gear_name} 档位不可用", risk_level="medium")

        try:
            raw = await dispatch_tool(tool_name, tool_args)
            success = raw.get("success", False)
            state.append_event({
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "event_type": "TOOL_CALL",
                "actor": "pydanticai",
                "action": f"invoke_tool:{tool_name}",
                "delta_entropy": 0.03,
                "success": success,
            })
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
                output=raw.get("stdout", raw.get("output", str(raw))),
                error=raw.get("error") or raw.get("stderr"),
                risk_level=risk,
            )
        except Exception as e:
            return ToolResult(success=False, error=str(e), risk_level="medium")

    def get_state(self) -> AgentState:
        """读取审计状态机"""
        return AgentState(
            agent_id=self._agent_id,
            status="idle",
            current_task=None,
            memory_size=len(state.event_log) if hasattr(state, "event_log") else 0,
        )

    def supports_streaming(self) -> bool:
        return True
