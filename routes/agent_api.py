"""
Entropy Runtime · Agent Adapter REST API
AI 对话、工具调用、自主循环相关端点
（跟 AutoGPT 耦合，框架变更时需重写）
"""
import os
import re
import json
import time
import uuid
import hashlib
import logging
import subprocess
import sys
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from config import Config, GEAR_MAP
from audit import state
from security import check_input_intent
from tools import GEAR_TOOLS
from models import MODEL_REGISTRY, parse_upgrade_request
from verification import verify_output
from memory import MemoryStore, generate_summary_text, auto_summarize_session
from routes.ws import active_connections
from adapters import PydanticAIAdapter
from orchestrator.decompose import decompose

logger = logging.getLogger("entropyruntime")

# ========== 全局 Adapter 实例（PydanticAI — 轻量默认）==========
_adapter=PydanticAIAdapter()

# ========== API Token 认证 ==========
API_TOKEN = os.environ.get("ENTROPY_RUNTIME_API_KEY", "")

async def verify_api_token(request: Request):
    """验证 API Token（从 Authorization header 或 X-API-Key header 读取）"""
    if not API_TOKEN:
        return True
    auth_header = request.headers.get("Authorization", "")
    api_key_header = request.headers.get("X-API-Key", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    elif api_key_header:
        token = api_key_header
    if not token or token != API_TOKEN:
        raise HTTPException(
            status_code=401,
            detail="Unauthorized: 无效或缺失 API Token。请通过 Authorization: Bearer *** 或 X-API-Key: <token> 传递。",
        )
    return True

router = APIRouter(dependencies=[Depends(verify_api_token)])


# ========== 内部辅助函数 ==========

def _log_intent_block(gear: int, intent: dict, actor: str = "human"):
    gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
    intent_event = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "actor": actor,
        "old_gear": gear, "new_gear": intent["target_gear"],
        "gear_name": GEAR_MAP.get(intent["target_gear"], {}).get("name", "UNKNOWN"),
        "entropy_previous": round(state.control_entropy, 6),
        "entropy_new": round(state.calculate_entropy(intent["target_gear"]), 6),
        "entropy_delta": round(state.calculate_entropy(intent["target_gear"]) - state.control_entropy, 6),
        "direction": "up", "landauer_cost_joules": 0.0,
        "event_type": "INTENT_BLOCK",
        "action": f"{GEAR_MAP[gear]['name']} -> INTENT_BLOCK -> {gear_name_map.get(intent['target_gear'], 'UNKNOWN')}",
        "trigger_chain": ["user_message", f"intent_detected({intent['matched_signal']})", f"BLOCKED(need_gear_{intent['target_gear']})"],
        "intent_reason": intent["reason"], "matched_signal": intent["matched_signal"],
    }
    state.append_event(intent_event)


def _log_violation(gear: int, verification: dict, reply: str, actor: str = "human"):
    violation_event = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "actor": actor,
        "old_gear": gear, "new_gear": verification["target_gear"],
        "gear_name": GEAR_MAP.get(verification["target_gear"], {}).get("name", "UNKNOWN"),
        "entropy_previous": round(state.control_entropy, 6),
        "entropy_new": round(state.calculate_entropy(verification["target_gear"]), 6),
        "entropy_delta": round(state.calculate_entropy(verification["target_gear"]) - state.control_entropy, 6),
        "direction": "up", "landauer_cost_joules": 0.0,
        "event_type": "OUTPUT_VIOLATION",
        "action": f"{GEAR_MAP[gear]['name']} -> VIOLATION -> {GEAR_MAP[verification['target_gear']]['name']}",
        "trigger_chain": ["ai_output", f"signal({verification['reason']})", f"BLOCKED(need_gear_{verification['target_gear']})"],
        "violation_reason": verification["reason"], "blocked_reply_preview": reply[:200],
    }
    state.append_event(violation_event)


# ========== 核心交互 API ==========

@router.post("/api/chat")
async def ai_chat(request: Request):
    data = await request.json()
    user_message = data.get("message", "")
    model_id = data.get("model_id", data.get("model", "kimi"))
    gear = data.get("gear", state.current_gear)
    upgrade_retry = data.get("upgrade_retry", False)
    session_id = data.get("session_id")
    memory_context = data.get("memory_context")
    actor = data.get("actor", "human")
    autogpt_mode = data.get("autogpt_mode", False)

    if not user_message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    # [Bug 3 fix] 消息去重保护
    if not upgrade_retry and state.is_duplicate_message(user_message):
        return JSONResponse({
            "error": "duplicate_message",
            "message": "该消息已在短时间内处理过，请勿重复发送",
        }, status_code=429)

    state.last_activity_time = time.time()

    # Layer 0: 输入意图预检
    if not upgrade_retry:
        intent = check_input_intent(user_message, gear)
        if intent["needs_upgrade"]:
            gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
            _log_intent_block(gear, intent, actor)
            return {
                "success": True,
                "reply": f"[输入意图预检拦截] {intent['reason']}。系统已自动生成升级申请。",
                "model": MODEL_REGISTRY.get(model_id, {}).get("name", model_id),
                "gear": gear, "gear_name": GEAR_MAP[gear]["name"],
                "upgrade_request": {
                    "target_gear": intent["target_gear"],
                    "target_gear_name": gear_name_map.get(intent["target_gear"], "UNKNOWN"),
                    "reason": intent["reason"], "risk_level": "medium",
                    "min_required_gear": intent["target_gear"],
                },
                "validation_status": "blocked_by_intent_verifier",
            }

    # AutoGPT 模式审计
    if autogpt_mode:
        state.append_event({
            "event_type": "AUTO_GPT_ACTIVATED",
            "actor": "human",
            "action": f"AutoGPT mode enabled for: {user_message[:50]}",
            "delta_entropy": 0,
            "success": True,
        })
        logger.info(f"[AutoGPT] Mode enabled, message: {user_message[:50]}")

    # ===== Orchestrator 路由：复杂任务走 decompose + execute_plan =====
    COMPLEX_KEYWORDS = {"并", "然后", "之后", "同时", "多个", "并且", "以及",
                        "分别", "逐一", "依次", "再", "先", "后", "全面",
                        "安全检查", "审计", "审查", "分析报告", "评估"}
    is_complex = (len(user_message) > 100 or
                  any(kw in user_message for kw in COMPLEX_KEYWORDS))
    if is_complex:
        logger.info(f"[Orchestrator] 复杂任务检测触发 (len={len(user_message)}, "
                     f"keywords matched), 准备拆解: {user_message[:80]}...")
        state.append_event({
            "event_type": "ORCHESTRATOR_FALLBACK",
            "actor": actor,
            "action": f"complex_task_routed_to_orchestrator: {user_message[:80]}",
            "delta_entropy": 0.05,
            "success": True,
        })
        try:
            subtasks = decompose(user_message)
            if subtasks and len(subtasks) > 0 and subtasks[0].id != "task-rejected":
                logger.info(f"[Orchestrator] 拆解出 {len(subtasks)} 个子任务: "
                            f"{[t.id for t in subtasks]}")
                # 直接使用 PydanticAI Adapter 执行每个子任务（avoid HTTP callback）
                orch_results = []
                total_start = time.time()
                for i, task in enumerate(subtasks):
                    logger.info(f"[Orchestrator] 执行子任务 {i+1}/{len(subtasks)}: {task.id} — {task.description[:60]}")
                    try:
                        t0 = time.time()
                        task_result = await _adapter.run(task.intent, {
                            "model_id": task.model_id or model_id,
                            "gear": task.gear or gear,
                            "actor": f"orchestrator:{task.id}",
                            "memory_context": memory_context,
                        })
                        elapsed = time.time() - t0
                        orch_results.append({
                            "task_id": task.id,
                            "description": task.description,
                            "success": task_result.success,
                            "output": task_result.output,
                            "error": task_result.error,
                            "elapsed": round(elapsed, 2),
                        })
                        logger.info(f"[Orchestrator] {task.id}: {'✅' if task_result.success else '❌'} ({elapsed:.1f}s)")
                    except Exception as e:
                        logger.error(f"[Orchestrator] {task.id} 执行异常: {e}")
                        orch_results.append({
                            "task_id": task.id,
                            "description": task.description,
                            "success": False,
                            "output": "",
                            "error": str(e),
                            "elapsed": 0.0,
                        })

                total_time = round(time.time() - total_start, 2)
                success_count = sum(1 for r in orch_results if r["success"])
                total_count = len(orch_results)

                # 构建汇总回复
                summary_lines = [f"Orchestrator 执行完成: {success_count}/{total_count} 子任务成功"]
                detail_lines = []
                for r in orch_results:
                    s = "✅" if r["success"] else "❌"
                    snippet = (r["output"] or r["error"] or "(空)")[:200]
                    detail_lines.append(f"  {s} [{r['task_id']}] {snippet}")
                reply = "\n".join(summary_lines)
                reply += "\n\n--- 详细结果 ---\n" + "\n".join(detail_lines)
                reply += f"\n\n⏱ 总耗时: {total_time}s | 子任务: {total_count} | 成功: {success_count}/{total_count}"

                result = {
                    "success": success_count > 0,
                    "reply": reply,
                    "tool_calls": None,
                    "error": None,
                    "orchestrator": True,
                    "subtask_count": total_count,
                    "total_time": total_time,
                }
                # 输出校验（保持原有安全层）
                upgrade_request = None
                verification = verify_output(reply, gear)
                if not verification["allowed"]:
                    _log_violation(gear, verification, reply, actor)
                    gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
                    result["reply"] = f"[输出校验拦截] {verification['reason']}"
                    result["upgrade_request"] = {
                        "target_gear": verification["target_gear"],
                        "target_gear_name": gear_name_map.get(verification["target_gear"], "UNKNOWN"),
                        "reason": verification["reason"],
                        "risk_level": "medium" if verification["target_gear"] <= 3 else "high",
                        "min_required_gear": verification["target_gear"],
                    }
                    result["validation_status"] = "blocked_by_verifier"
                else:
                    result["validation_status"] = "none"
                    result["upgrade_request"] = None

                if gear <= 2:
                    result["confirmation_mode"] = "step_by_step"
                elif gear == 3:
                    result["confirmation_mode"] = "batch"
                else:
                    result["confirmation_mode"] = "notify_on_error"
                return JSONResponse(result)
            else:
                reason = subtasks[0].description if subtasks else "拆解失败"
                logger.warning(f"[Orchestrator] 拆解返回空/被拒绝: {reason}")
                # 降级到 PydanticAI
        except Exception as e:
            logger.error(f"[Orchestrator] 拆解/执行异常: {e}", exc_info=True)
            # 异常时降级到 PydanticAI

    result_obj = await _adapter.run(user_message, {
        "model_id": model_id,
        "gear": gear,
        "upgrade_retry": upgrade_retry,
        "memory_context": memory_context,
        "actor": actor,
    })
    # 将 TaskResult 转为 dict 以兼容下游处理逻辑
    result = {
        "success": result_obj.success,
        "reply": result_obj.output,
        "tool_calls": result_obj.tool_calls or None,
        "error": result_obj.error,
    }

    if not result["success"]:
        return JSONResponse(result)

    # [P1 fix] 对 AI 生成的 tool_calls 执行 shell 命令二次校验
    # 防止 AI 在 ADAPT/LET_GO 档位下发出危险 shell 命令
    if result.get("tool_calls"):
        from security import validate_command
        for tc in result["tool_calls"]:
            tc_name = tc.get("name", tc.get("function", tc.get("tool", "")))
            if tc_name in ("run_shell", "execute_shell", "bash", "shell"):
                args = tc.get("arguments") or tc.get("args") or tc.get("input", {})
                if isinstance(args, dict):
                    cmd = args.get("command", args.get("cmd", str(args)))
                elif isinstance(args, str):
                    cmd = args
                else:
                    cmd = str(args)
                allowed, reason = validate_command(cmd, gear)
                if not allowed:
                    logger.warning(f"[P1 shell guard] AI tool call blocked: {reason}")
                    result_obj = None  # allow GC
                    return JSONResponse({
                        "success": False,
                        "error": f"[安全拦截] AI 试图执行的命令 '{cmd[:80]}' 已被拦截: {reason}",
                        "validation_status": "blocked_by_shell_validator",
                    }, status_code=403)

    reply = result.get("reply", "")
    upgrade_request = None

    if upgrade_retry:
        reply = re.sub(r'\[UPGRADE_REQUEST\].*?\[/UPGRADE_REQUEST\]', '', reply, flags=re.DOTALL).strip()
        result["reply"] = reply
        result["upgrade_request"] = None
    else:
        upgrade_request = parse_upgrade_request(reply)
        if upgrade_request:
            reply = re.sub(r'\[UPGRADE_REQUEST\].*?\[/UPGRADE_REQUEST\]', '', reply, flags=re.DOTALL).strip()
            result["reply"] = reply
        result["upgrade_request"] = upgrade_request

        # [Bug 4 fix] 无论是否有升级申请，都先对 reply 执行输出校验
        verification = verify_output(reply, gear)
        if not verification["allowed"]:
            _log_violation(gear, verification, reply, actor)
            gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
            result["reply"] = f"[输出校验拦截] {verification['reason']}。系统已自动生成升级申请。"
            final_target = verification["target_gear"]
            if upgrade_request and upgrade_request.get("target_gear", 0) > final_target:
                final_target = upgrade_request["target_gear"]
            result["upgrade_request"] = {
                "target_gear": final_target,
                "target_gear_name": gear_name_map.get(final_target, "UNKNOWN"),
                "reason": verification["reason"],
                "risk_level": "medium" if final_target <= 3 else "high",
                "min_required_gear": final_target,
            }
            result["validation_status"] = "blocked_by_verifier"
            result["verification"] = verification
        else:
            result["validation_status"] = "none"

    # [Post-success guard] 如果有工具调用说明 AI 已在执行，忽略升级申请
    if upgrade_request and result.get("tool_calls"):
        logger.info(
            f"[Post-success guard] 忽略升级申请：本轮有 {len(result['tool_calls'])} 次工具调用，"
            f"原申请 target_gear={upgrade_request['target_gear']}"
        )
        upgrade_request = None
        result["upgrade_request"] = None

    # 确认模式
    if gear <= 2:
        confirmation_mode = "step_by_step"
    elif gear == 3:
        confirmation_mode = "batch"
    else:
        confirmation_mode = "notify_on_error"
    result["confirmation_mode"] = confirmation_mode

    if gear == 3:
        tool_call_count = sum(1 for e in state.event_log if e.get("event_type") == "TOOL_CALL")
        steps_since = tool_call_count % 5
        result["batch_info"] = {
            "steps_since_last_confirm": steps_since,
            "next_confirm_at": 5 - steps_since,
        }

    return JSONResponse(result)


# ========== 流式对话 API（SSE） ==========

async def _stream_events(user_message: str, gear: int, model_id: str,
                         actor: str, session_id: str, memory_context: str):
    """
    流式事件生成器，将 PydanticAI 的 token 流转换为 SSE 事件。
    集成安全层：意图预检（已在调用前执行）+ 输出校验（流完成后执行）。
    """
    accumulated_reply = ""
    try:
        async for event in _adapter.run_stream(user_message, {
            "model_id": model_id, "gear": gear,
            "actor": actor, "memory_context": memory_context,
        }):
            if event["type"] == "token":
                accumulated_reply += event["text"]
                yield f"data: {json.dumps({'type': 'token', 'text': event['text']})}\n\n"
            elif event["type"] == "error":
                yield f"data: {json.dumps({'type': 'error', 'message': event['message']})}\n\n"
                yield "data: [DONE]\n\n"
                return
            elif event["type"] == "done":
                # Layer 2: 输出校验
                verification = verify_output(accumulated_reply, gear)
                if not verification["allowed"]:
                    _log_violation(gear, verification, accumulated_reply, actor)
                    gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
                    violation = {
                        "type": "violation",
                        "reply": f"[输出校验拦截] {verification['reason']}。系统已自动生成升级申请。",
                        "upgrade_request": {
                            "target_gear": verification["target_gear"],
                            "target_gear_name": gear_name_map.get(verification["target_gear"], "UNKNOWN"),
                            "reason": verification["reason"],
                            "risk_level": "medium" if verification["target_gear"] <= 3 else "high",
                            "min_required_gear": verification["target_gear"],
                        },
                        "validation_status": "blocked_by_verifier",
                    }
                    yield f"data: {json.dumps(violation)}\n\n"
                    yield "data: [DONE]\n\n"
                    return

                # 正常完成
                gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
                done_event = {
                    "type": "done",
                    "reply": accumulated_reply,
                    "gear": gear,
                    "gear_name": GEAR_MAP.get(gear, {}).get("name", "UNKNOWN"),
                    "model": MODEL_REGISTRY.get(model_id, {}).get("name", model_id),
                    "confirmation_mode": (
                        "step_by_step" if gear <= 2
                        else ("batch" if gear == 3 else "notify_on_error")
                    ),
                    "validation_status": "none",
                }
                yield f"data: {json.dumps(done_event)}\n\n"
                yield "data: [DONE]\n\n"

    except Exception as e:
        logger.error(f"[SSE] stream error: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"


@router.post("/api/chat/stream")
async def ai_chat_stream(request: Request):
    """
    SSE 流式对话端点。

    输入格式同 /api/chat，返回 text/event-stream。
    安全层完整保留：意图预检 (Layer 0) + 输出校验 (Layer 2) + 审计链。
    """
    data = await request.json()
    user_message = data.get("message", "")
    model_id = data.get("model_id", data.get("model", "kimi"))
    gear = data.get("gear", state.current_gear)
    upgrade_retry = data.get("upgrade_retry", False)
    session_id = data.get("session_id")
    memory_context = data.get("memory_context")
    actor = data.get("actor", "human")

    if not user_message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    state.last_activity_time = time.time()

    # Layer 0: 输入意图预检
    if not upgrade_retry:
        intent = check_input_intent(user_message, gear)
        if intent["needs_upgrade"]:
            _log_intent_block(gear, intent, actor)
            gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}
            return JSONResponse({
                "success": True,
                "reply": f"[输入意图预检拦截] {intent['reason']}。系统已自动生成升级申请。",
                "model": MODEL_REGISTRY.get(model_id, {}).get("name", model_id),
                "gear": gear, "gear_name": GEAR_MAP[gear]["name"],
                "upgrade_request": {
                    "target_gear": intent["target_gear"],
                    "target_gear_name": gear_name_map.get(intent["target_gear"], "UNKNOWN"),
                    "reason": intent["reason"], "risk_level": "medium",
                    "min_required_gear": intent["target_gear"],
                },
                "validation_status": "blocked_by_intent_verifier",
            })

    return StreamingResponse(
        _stream_events(user_message, gear, model_id, actor, session_id, memory_context),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ========== AutoGPT 工具审计 ==========

@router.post("/api/autogpt/tool")
async def autogpt_tool_call(request: Request):
    """AutoGPT 工具调用审计端点"""
    try:
        data = await request.json()
        command = data.get("command", "")
        args = data.get("args", {})

        if not command:
            return JSONResponse({"success": False, "error": "命令不能为空"}, status_code=400)

        logger.info(f"[AutoGPT Tool] 收到工具调用: {command} args={args}")

        result = None
        error = None

        try:
            safe_payload = json.dumps({"command": command, "args": args})
            cmd = [sys.executable, "-c", """
import sys, json
sys.path.insert(0, "/root/AutoGPT/source")
from autogpt.entropy_guard import audit_command

payload = json.loads(sys.argv[1])
result = audit_command(command=payload["command"], args=payload["args"])
print(result)
""", safe_payload]

            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if proc.returncode == 0:
                result = proc.stdout.strip()
            else:
                error = proc.stderr.strip() or f"Exit code: {proc.returncode}"
        except subprocess.TimeoutExpired:
            error = "命令执行超时 (30秒)"
        except Exception as exec_err:
            error = str(exec_err)

        success = error is None

        tool_event = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": "TOOL_CALL",
            "actor": "autogpt",
            "action": f"execute_command:{command}",
            "delta_entropy": 0.03 if success else 0.0,
            "success": success,
            "gear_name": GEAR_MAP[state.current_gear]["name"],
            "control_entropy": round(state.control_entropy, 6),
            "details": {
                "command": command,
                "args": args,
                "source": "autogpt_v0.4.7",
                "result": result[:500] if result else None,
                "error": error,
            },
        }

        state.append_event(tool_event)
        logger.info(f"[AutoGPT Tool] 工具调用完成: success={success}")

        return JSONResponse({
            "success": True,
            "command": command,
            "args": args,
            "result": result,
            "error": error,
            "event_id": len(state.event_log),
        })
    except Exception as e:
        logger.error(f"[POST /api/autogpt/tool] Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


# ========== 自主循环 ==========

@router.post("/api/autonomy")
async def autonomy_loop(request: Request):
    data = await request.json()
    user_message = data.get("message", "")
    model_id = data.get("model_id", data.get("model", "kimi"))
    max_steps = data.get("max_steps", 10)

    if not user_message:
        return JSONResponse({"error": "消息不能为空"}, status_code=400)

    plan_prompt = f"""请将以下任务拆解为可执行的步骤（最多{max_steps}步）。
返回JSON数组，每个元素包含 step 和 gear。
只返回JSON数组。任务：{user_message}"""

    plan_obj = await _adapter.run(plan_prompt, {
        "model_id": model_id, "gear": 1, "upgrade_retry": False,
    })
    plan_result = {"success": plan_obj.success, "reply": plan_obj.output, "error": plan_obj.error}
    if not plan_result.get("success"):
        return JSONResponse({"success": False, "error": "任务拆解失败: " + str(plan_result.get("error", ""))})

    reply = plan_result.get("reply", "")
    try:
        json_match = re.search(r'\[.*\]', reply, re.DOTALL)
        steps = json.loads(json_match.group()) if json_match else [{"step": user_message, "gear": 3}]
    except Exception:
        steps = [{"step": user_message, "gear": 3}]

    results = []
    for i, step_info in enumerate(steps[:max_steps]):
        step_desc = step_info.get("step", "")
        auto_gear = min(max(step_info.get("gear", 3), 1), 4)
        step_obj = await _adapter.run(step_desc, {
            "model_id": model_id, "gear": auto_gear, "upgrade_retry": False,
        })
        step_result = {
            "success": step_obj.success,
            "reply": step_obj.output,
            "tool_calls": step_obj.tool_calls,
        }
        results.append({
            "step": i + 1, "description": step_desc, "gear": auto_gear,
            "gear_name": GEAR_MAP.get(auto_gear, {}).get("name", "UNKNOWN"),
            "success": step_result.get("success", False),
            "reply": step_result.get("reply", "")[:200],
            "tool_calls": step_result.get("tool_calls"),
        })
        if not step_result.get("success"):
            break

    return JSONResponse({
        "success": True, "plan": steps, "results": results,
        "total_steps": len(results),
        "model": MODEL_REGISTRY.get(model_id, {}).get("name", model_id),
    })


# ========== 多智能体协同 API ==========

@router.post("/api/multi-agent")
async def multi_agent_task(request: Request):
    """多智能体协同接口"""
    data = await request.json()
    task = data.get("message", "")
    agent_a = data.get("agent_a", "kimi")
    agent_b = data.get("agent_b", "mimo")
    gear = int(data.get("gear", state.current_gear))

    if not task:
        return JSONResponse({"success": False, "error": "任务描述不能为空"}, status_code=400)

    if gear not in [3, 4]:
        return JSONResponse({
            "success": False,
            "error": "多智能体协同需要 ADAPT (3) 或 LET_GO (4) 档位"
        }, status_code=400)

    session_id = str(uuid.uuid4())[:8]
    results = {"session_id": session_id, "task": task, "agents": [], "steps": []}

    # Step 1: Agent A 分析任务
    prompt_a = f"""你是 Agent A ({agent_a})。你的任务是：{task}

请执行分析（可以使用 run_shell 工具检查服务器状态），并将分析结果写入留言板。

留言板消息格式（必须使用 write_board 工具写入）：
{{
  "from": "{agent_a}",
  "to": "{agent_b}",
  "type": "analysis_result",
  "content": "你的分析结果内容...",
  "timestamp": "<ISO时间戳>"
}}

请分析完成后，使用 write_board 工具将结构化的分析结果写入留言板，然后回复用户（我）已完成分析。"""

    result_a_obj = await _adapter.run(prompt_a, {
        "model_id": agent_a, "gear": gear, "upgrade_retry": False, "actor": f"multi-agent:{agent_a}",
    })
    result_a = {"success": result_a_obj.success, "reply": result_a_obj.output, "tool_calls": result_a_obj.tool_calls}

    results["agents"].append({
        "agent": agent_a, "role": "analyzer",
        "success": result_a.get("success", False),
        "reply": result_a.get("reply", ""),
        "tool_calls": result_a.get("tool_calls") or [],
        "tool_call_count": len(result_a.get("tool_calls") or []),
    })

    # Step 2: 读取留言板
    board_memories = MemoryStore.get_recent(limit=20)
    board_content = "\n".join(
        f"[{m.get('timestamp','?')}] {m.get('content','')}"
        for m in board_memories[-10:]
    )
    results["steps"].append({
        "step": "read_board",
        "board_messages_count": len(board_memories),
        "content_preview": board_content[-1500:] if board_content else "(empty)",
    })

    # Step 3: Agent B 生成建议
    prompt_b = f"""你是 Agent B ({agent_b})。Agent A ({agent_a}) 已经完成了分析并将结果写入了留言板。

留言板最近内容：
{board_content[-2000:] if board_content else '(empty)'}

请阅读留言板内容，生成针对任务「{task}」的改进建议。回复中请包含：
1. 当前状况总结
2. 潜在风险点
3. 具体改进建议（优先级排序）

如果有具体可执行的建议，可以使用 run_shell 工具验证或执行。"""

    result_b_obj = await _adapter.run(prompt_b, {
        "model_id": agent_b, "gear": gear, "upgrade_retry": False, "actor": f"multi-agent:{agent_b}",
    })
    result_b = {"success": result_b_obj.success, "reply": result_b_obj.output, "tool_calls": result_b_obj.tool_calls}

    results["agents"].append({
        "agent": agent_b, "role": "advisor",
        "success": result_b.get("success", False),
        "reply": result_b.get("reply", ""),
        "tool_calls": result_b.get("tool_calls") or [],
        "tool_call_count": len(result_b.get("tool_calls") or []),
    })

    results["success"] = True
    results["summary"] = {
        "agent_a_output": result_a.get("reply", "")[:500],
        "agent_b_output": result_b.get("reply", "")[:500],
        "total_tool_calls": (
            len(result_a.get("tool_calls") or []) +
            len(result_b.get("tool_calls") or [])
        ),
    }

    return JSONResponse(results)


# ========== 工具调用拦截 API（四档确认体系）==========

@router.post("/api/tool-call")
async def tool_call_request(request: Request):
    """AutoGPT 提交工具调用请求"""
    data = await request.json()
    tool_name = data.get("tool", "unknown")
    tool_args = data.get("args", "")
    context = data.get("context", "")
    caller = data.get("caller", "autogpt")

    call_id = str(uuid.uuid4())[:8]
    gear = state.current_gear
    timestamp = time.time()

    state.append_event({
        "event_type": "TOOL_CALL_REQUEST",
        "actor": caller,
        "action": f"{tool_name}({tool_args[:100]})",
        "proposer": caller,
    })

    if gear <= 2:
        state.pending_tool_calls[call_id] = {
            "id": call_id, "tool": tool_name, "args": tool_args,
            "context": context, "status": "pending",
            "timestamp": timestamp, "gear": gear,
        }
        return JSONResponse({
            "status": "pending", "id": call_id,
            "message": f"等待人类确认: {tool_name}", "gear": gear,
        })
    elif gear == 3:
        state.batch_queue.append({
            "id": call_id, "tool": tool_name, "args": tool_args, "timestamp": timestamp,
        })
        needs_review = len(state.batch_queue) >= state.batch_threshold
        state.append_event({
            "event_type": "TOOL_CALL_AUTO_ALLOW", "actor": caller,
            "action": f"{tool_name}({tool_args[:100]})", "proposer": "system",
        })
        response = {
            "status": "allow", "id": call_id,
            "message": f"ADAPT 自动放行: {tool_name}",
            "batch_count": len(state.batch_queue),
        }
        if needs_review:
            response["batch_review_needed"] = True
            response["batch_summary"] = [
                {"tool": item["tool"], "args": item["args"][:80]}
                for item in state.batch_queue
            ]
        return JSONResponse(response)
    else:
        state.append_event({
            "event_type": "TOOL_CALL_AUTO_ALLOW", "actor": caller,
            "action": f"{tool_name}({tool_args[:100]})", "proposer": "system",
        })
        return JSONResponse({
            "status": "allow", "id": call_id,
            "message": f"LET_GO 自动放行: {tool_name}",
        })


@router.get("/api/tool-call/{call_id}/status")
async def tool_call_status(call_id: str):
    """AutoGPT 轮询工具调用的审批状态"""
    tc = state.pending_tool_calls.get(call_id)
    if not tc:
        return JSONResponse({"status": "not_found"}, status_code=404)
    return JSONResponse({
        "id": call_id, "status": tc["status"],
        "tool": tc["tool"], "args": tc["args"][:100],
    })


@router.post("/api/tool-call/{call_id}/approve")
async def tool_call_approve(call_id: str, request: Request):
    """人类在前端批准/拒绝工具调用"""
    data = await request.json()
    approved = data.get("approved", True)
    reason = data.get("reason", "")

    tc = state.pending_tool_calls.get(call_id)
    if not tc:
        return JSONResponse({"error": "not found"}, status_code=404)

    if approved:
        tc["status"] = "approved"
        state.append_event({
            "event_type": "TOOL_CALL_APPROVED", "actor": "human",
            "action": f"Approved {tc['tool']}({tc['args'][:100]})", "proposer": "human",
        })
    else:
        tc["status"] = "denied"
        tc["deny_reason"] = reason
        state.append_event({
            "event_type": "TOOL_CALL_DENIED", "actor": "human",
            "action": f"Denied {tc['tool']}: {reason}", "proposer": "human",
        })

    return JSONResponse({"id": call_id, "status": tc["status"]})


@router.post("/api/batch-review")
async def batch_review(request: Request):
    """ADAPT 档位的批量审核"""
    data = await request.json()
    approved = data.get("approved", True)

    if approved:
        state.append_event({
            "event_type": "BATCH_APPROVED", "actor": "human",
            "action": f"Approved {len(state.batch_queue)} tool calls", "proposer": "human",
        })
        state.batch_queue.clear()
        return JSONResponse({"status": "approved", "message": "批量确认成功"})
    else:
        state.append_event({
            "event_type": "BATCH_DENIED", "actor": "human",
            "action": "Denied batch, switching to EMBRACE", "proposer": "human",
        })
        state.batch_queue.clear()
        state.switch_gear(1, "down", "api")
        return JSONResponse({"status": "denied", "message": "已切换到 EMBRACE 档"})


@router.get("/api/pending-tool-calls")
async def pending_tool_calls_endpoint(request: Request):
    """前端获取所有待审批的工具调用"""
    pending = [
        {
            "id": tc["id"], "tool": tc["tool"],
            "args": tc["args"][:200], "context": tc.get("context", "")[:200],
            "timestamp": tc["timestamp"], "gear": tc["gear"],
        }
        for tc in state.pending_tool_calls.values()
        if tc["status"] == "pending"
    ]
    batch = [
        {"tool": item["tool"], "args": item["args"][:200], "timestamp": item["timestamp"]}
        for item in state.batch_queue
    ]
    return JSONResponse({
        "pending": pending,
        "batch_queue": batch,
        "confirmation_mode": (
            "step_by_step" if state.current_gear <= 2
            else ("batch" if state.current_gear == 3 else "notify_on_error")
        ),
    })
