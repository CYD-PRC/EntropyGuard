"""

EntropyGuard · REST API

所有 HTTP 端点



[Bug fixes applied]

- Bug 1: events API 增加 reload 机制确保读到最新数据

- Bug 2: approve-upgrade 强制使用 intent 检测的 target_gear，不允许降级

- Bug 3: chat 接口增加消息去重保护

- Bug 4: 输出校验不再因存在升级申请而跳过

- Bug 6: reset 接口增加导出提示，确保 auto_summarize 生效

"""

import re

import uuid

import hashlib

import json

import time

import logging

from datetime import datetime



from fastapi import APIRouter, Request

from fastapi.responses import JSONResponse



from config import Config, GEAR_MAP

from audit import state

from security import check_input_intent

from tools import GEAR_TOOLS

from models import MODEL_REGISTRY, gear_aware_call, parse_upgrade_request

from verification import verify_output

from memory import MemoryStore, generate_summary_text, auto_summarize_session

from routes.ws import active_connections



logger = logging.getLogger("entropyguard")



router = APIRouter()




# ========== 只读状态 API ==========



@router.get("/api/state")

async def get_state():

    # [Bug 1 fix] 刷新 state（确保读到最新的 events.json）

    state._load_events()

    # 刷新 autogpt_sc / autogpt_tool_calls 等实例属性
    state.compute_dynamic_sc()

    return {

        "current_gear": state.current_gear,

        "gear_name": GEAR_MAP[state.current_gear]["name"],

        "control_entropy": round(state.control_entropy, 6),

        "temperature_k": Config.TEMPERATURE,

        "downgrade_cost_joules": round(state.downgrade_cost(), 23),

        "event_count": len(state.event_log),

        "uptime_seconds": round(time.time() - state.last_switch_time, 1),

        "tool_calls": sum(1 for e in state.event_log if e.get("event_type") == "TOOL_CALL"),
        "autogpt_sc": round(state.autogpt_sc, 4),
        "autogpt_event_count": state.autogpt_event_count,
        "autogpt_tool_calls": state.autogpt_tool_calls,

    }




@router.get("/api/events")
async def get_events(limit: int = 50):
    # [Bug 1 fix] 每次读取前从文件刷新，解决多进程/gunicorn 下的数据不一致
    state._load_events()
    return {"total_events": len(state.event_log), "events": state.event_log[-limit:]}


@router.post("/api/events")
async def create_event(request: Request):
    """
    接收外部事件（如 AutoGPT 命令审计）并写入审计链。
    调用 state.append_event() 将事件持久化。
    """
    try:
        data = await request.json()

        if not data:
            return JSONResponse({"success": False, "error": "事件数据不能为空"}, status_code=400)

        event_type = data.get("event_type", "TOOL_CALL")
        actor = data.get("actor", "autogpt")
        action = data.get("action", "")
        delta_entropy = data.get("delta_entropy", 0.0)
        success = data.get("success", True)
        details = data.get("details", {})

        event = {
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "event_type": event_type,
            "actor": actor,
            "action": action,
            "delta_entropy": delta_entropy,
            "success": success,
            "gear_name": GEAR_MAP[state.current_gear]["name"],
            "control_entropy": round(state.control_entropy, 6),
            "details": details,
        }

        if "entropy_previous" in data:
            event["entropy_previous"] = data["entropy_previous"]
        if "entropy_new" in data:
            event["entropy_new"] = data["entropy_new"]
        if "entropy_delta" in data:
            event["entropy_delta"] = data["entropy_delta"]

        state.append_event(event)

        logger.info(f"[POST /api/events] Recorded event: {actor} - {action}")

        return JSONResponse({
            "success": True,
            "event_id": len(state.event_log),
            "event": event,
        })

    except Exception as e:
        logger.error(f"[POST /api/events] Error: {e}")
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)



@router.post("/api/autogpt/tool")
async def autogpt_tool_call(request: Request):
    """
    AutoGPT 工具调用端点。
    接收工具调用请求，调用 AutoGPT 的 execute_command，
    将结果记录到审计链并返回。
    """
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
            import subprocess
            import sys

            cmd = [sys.executable, "-c", f"""
import sys
sys.path.insert(0, '/root/AutoGPT/source')
from autogpt.entropy_guard import audit_command

result = audit_command(command='{command}', args={args})
print(result)
"""]

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


@router.get("/api/messages")

async def get_messages(limit: int = 50):

    messages = MemoryStore.get_recent(limit=limit)

    return {

        "total": len(messages),

        "messages": [

            {

                "id": m.get("id", ""), "timestamp": m.get("timestamp", ""),

                "model": m.get("model", "unknown"), "gear": m.get("gear_name", ""),

                "content": m.get("content", ""),

            }

            for m in messages

        ],

    }




@router.get("/api/dynamic-sc")

async def get_dynamic_sc():

    result = state.compute_dynamic_sc()

    sc = result["dynamic_sc"]

    gear = state.current_gear



    # Sc 上界检查：基于约束前的原始 Sc 值来判断是否需要提案

    raw_sc = result.get("raw_sc", sc)

    if result.get("sc_overflow"):

        gear_max = {1: 0.0, 2: 0.5, 3: 1.0, 4: float("inf")}

        new_gear = gear + 1

        while new_gear < 4 and raw_sc > gear_max.get(new_gear, float("inf")):

            new_gear += 1

        result["auto_propose"] = {

            "direction": "up", "from": gear, "to": new_gear,

            "reason": result.get("overflow_message", f"Sc 超过档位{gear}上界，建议升级到 {GEAR_MAP[new_gear]['name']}"),

        }

    else:

        gear_max = {1: 0.0, 2: 0.5, 3: 1.0, 4: float("inf")}

        if sc > gear_max.get(gear, float("inf")) and gear < 4:

            new_gear = gear + 1

            while new_gear < 4 and sc > gear_max.get(new_gear, float("inf")):

                new_gear += 1

            result["auto_propose"] = {

                "direction": "up", "from": gear, "to": new_gear,

                "reason": f"Sc {sc:.4f} 超过档位{gear}上界，建议升级到 {GEAR_MAP[new_gear]['name']}",

            }



    gear_min = {1: 0.0, 2: 0.0, 3: 0.5, 4: 1.0}

    if sc < gear_min.get(gear, 0.0) and gear > 1:

        new_gear = gear - 1

        while new_gear > 1 and sc < gear_min.get(new_gear, 0.0):

            new_gear -= 1

        result["auto_propose"] = {

            "direction": "down", "from": gear, "to": new_gear,

            "reason": f"Sc {sc:.4f} 低于档位{gear}下界，建议降级到 {GEAR_MAP[new_gear]['name']}",

        }




    result["dynamic_sc"] = sc

    return result




@router.get("/api/stats")

async def get_stats():

    events = state.event_log

    total_events = len(events)

    tool_events = [e for e in events if e.get("event_type") == "TOOL_CALL"]



    gear_distribution = {1: 0, 2: 0, 3: 0, 4: 0}

    for e in events:

        if e.get("event_type") == "GEAR_SWITCH":

            gear_distribution[e.get("new_gear", 4)] = gear_distribution.get(e.get("new_gear", 4), 0) + 1

    if sum(gear_distribution.values()) == 0:

        gear_distribution[state.current_gear] = total_events



    tool_stats = {}

    for e in tool_events:

        tn = e.get("tool_name", "?")

        tool_stats[tn] = tool_stats.get(tn, 0) + 1



    model_stats = {}

    for e in events:

        model = e.get("model_id") or e.get("model") or "unknown"

        model_stats[model] = model_stats.get(model, 0) + 1



    risk_count = sum(1 for e in events if e.get("event_type") in ["OUTPUT_VIOLATION", "INTENT_BLOCK"])




    sc_trend = []

    for i, e in enumerate(events[-100:]):

        sc = e.get("entropy_after") or e.get("entropy_new") or state.control_entropy

        sc_trend.append({"id": i + 1, "sc": round(sc, 2), "type": e.get("event_type", "")})




    response_times = [e.get("response_time", 0) for e in events if e.get("response_time")]

    avg_response_time = sum(response_times) / len(response_times) if response_times else 0




    return {

        "total_events": total_events,

        "total_tool_calls": len(tool_events),

        "current_gear": state.current_gear,

        "gear_name": GEAR_MAP[state.current_gear]["name"],

        "control_entropy": round(state.control_entropy, 4),

        "gear_distribution": {

            "EMBRACE": gear_distribution.get(1, 0), "EXPLORE": gear_distribution.get(2, 0),

            "ADAPT": gear_distribution.get(3, 0), "LET_GO": gear_distribution.get(4, 0),

        },

        "tool_stats": tool_stats,

        "model_stats": model_stats,

        "risk_events_count": risk_count,

        "sc_trend": sc_trend[-50:],

        "uptime_hours": round((time.time() - state.last_switch_time) / 3600, 1),

        "avg_response_time_ms": round(avg_response_time * 1000, 2) if avg_response_time else 0,

    }




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

                    # [Bug 2 fix] 标记这是意图预检要求的最低档位，不允许降级

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

    result = await gear_aware_call(model_id, user_message, gear, upgrade_retry, memory_context, actor)



    if not result["success"]:

        return JSONResponse(result)



    reply = result.get("reply", "")

    upgrade_request = None  # 初始化，防止 upgrade_retry 分支跳过赋值导致 UnboundLocalError



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

        # 先校验原始回复内容（已去除 UPGRADE_REQUEST 标签）

        verification = verify_output(reply, gear)

        if not verification["allowed"]:

            _log_violation(gear, verification, reply, actor)

            gear_name_map = {1: "EMBRACE", 2: "EXPLORE", 3: "ADAPT", 4: "LET_GO"}

            result["reply"] = f"[输出校验拦截] {verification['reason']}。系统已自动生成升级申请。"

            # [Bug 2 fix] 输出校验要求的档位不低于意图预检要求的档位

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




    # [Post-success guard] 如果有工具调用说明 AI 已在执行，忽略升级申请

    if upgrade_request and result.get("tool_calls"):

        logger.info(

            f"[Post-success guard] 忽略升级申请：本轮有 {len(result['tool_calls'])} 次工具调用，"

            f"原申请 target_gear={upgrade_request['target_gear']}"

        )

        upgrade_request = None

        result["upgrade_request"] = None




    return JSONResponse(result)




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



    plan_result = await gear_aware_call(model_id, plan_prompt, 1, False)

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

        step_result = await gear_aware_call(model_id, step_desc, auto_gear, False)

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




# ========== 档位管理 API ==========



@router.post("/api/switch")

async def switch_gear(data: dict):

    try:

        new_gear = int(data.get("gear"))

        direction = data.get("direction", "up")

        source = data.get("source", "api")

        event = state.switch_gear(new_gear, direction, source)

        if event is None:

            return {"success": True, "event": None, "note": "already at this gear"}

        for ws in active_connections:

            try:

                await ws.send_json({"type": "state_update", "data": event})

            except Exception:

                pass

        return {"success": True, "event": event}

    except Exception as e:

        return {"success": False, "error": str(e)}




@router.post("/api/approve-upgrade")

async def approve_upgrade(data: dict):

    try:

        target_gear = int(data.get("target_gear"))

        # [Bug 2 fix] 如果前端传了 min_required_gear，确保最终档位不低于该值

        min_required = int(data.get("min_required_gear", 0))

        if min_required > 0 and target_gear < min_required:

            logger.warning(

                f"[Bug 2 guard] approve target_gear={target_gear} < min_required_gear={min_required}, "

                f"auto-correcting to {min_required}"

            )

            target_gear = min_required



        if target_gear not in GEAR_MAP:

            return JSONResponse({"success": False, "error": "无效档位"}, status_code=400)

        direction = "up" if target_gear >= state.current_gear else "down"

        old_gear = state.current_gear

        event = state.switch_gear(target_gear, direction, "ai_upgrade_request")



        approve_event = {

            "timestamp": datetime.utcnow().isoformat() + "Z",

            "actor": "user_approve",

            "old_gear": old_gear, "new_gear": target_gear,

            "gear_name": GEAR_MAP[target_gear]["name"],

            "entropy_previous": round(state.control_entropy, 6),

            "entropy_new": round(state.control_entropy, 6),

            "entropy_delta": 0.0, "direction": "approve",

            "landauer_cost_joules": 0.0,

            "event_type": "UPGRADE_APPROVE",

            "action": f"{GEAR_MAP[old_gear]['name']} -> UPGRADE_APPROVE -> {GEAR_MAP[target_gear]['name']}",

            "trigger_chain": ["user_approve", f"from_gear_{old_gear}", f"to_gear_{target_gear}"],

        }

        state.append_event(approve_event)



        # Clear dedup cache so user can resend the blocked message

        state.clear_dedup_cache()



        for ws in active_connections:

            try:

                await ws.send_json({"type": "state_update", "data": event})

                await ws.send_json({"type": "audit_event", "data": approve_event})

            except Exception:

                pass



        return JSONResponse({"success": True, "gear": target_gear, "gear_name": GEAR_MAP[target_gear]["name"]})

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.post("/api/reject-upgrade")

async def reject_upgrade(data: dict):

    try:

        current_gear = int(data.get("current_gear", state.current_gear))

        rejected_gear = int(data.get("rejected_gear", 0))



        reject_event = {

            "timestamp": datetime.utcnow().isoformat() + "Z",

            "actor": "user_reject",

            "old_gear": current_gear, "new_gear": current_gear,

            "gear_name": GEAR_MAP[current_gear]["name"],

            "entropy_previous": round(state.control_entropy, 6),

            "entropy_new": round(state.control_entropy, 6),

            "entropy_delta": 0.0, "direction": "reject",

            "landauer_cost_joules": 0.0,

            "event_type": "UPGRADE_REJECT",

            "action": f"{GEAR_MAP[current_gear]['name']} -> REJECT -> {GEAR_MAP.get(rejected_gear, {}).get('name', 'UNKNOWN')}",

            "trigger_chain": ["user_reject", f"from_gear_{current_gear}", f"rejected_gear_{rejected_gear}", "LOCKED"],

            "rejected_gear": rejected_gear,

            "reject_reason": data.get("reason", "用户拒绝"),

        }

        state.append_event(reject_event)

        return JSONResponse({"success": True, "message": "拒绝事件已记录"})

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.post("/api/reset")

async def reset_state(request: Request):

    try:

        data = await request.json() if await request.body() else {}

    except Exception:

        data = {}

    auto_summarize = data.get("auto_summarize", True)

    session_id = data.get("session_id", "unknown")



    # [Bug 6 fix] 重置前检查是否有事件，如果有则自动生成摘要并返回提示

    event_count = len(state.event_log)

    warning_msg = ""



    if event_count > 0:

        if auto_summarize:

            try:

                auto_summarize_session(state, session_id)

                warning_msg = f"已自动将会话摘要保存到 memory.json（共 {event_count} 个事件）。"

            except Exception as e:

                warning_msg = f"自动摘要失败: {e}。建议手动导出审计日志。"

        else:

            warning_msg = f"重置将清空 {event_count} 个审计事件。建议先通过 /api/events 导出。"



    state.event_log = []

    state.current_gear = 1

    state.control_entropy = 0.0

    state.last_switch_time = time.time()

    state._save_events()



    # 清空去重缓存

    state._recent_messages = {}



    for ws in active_connections:

        try:

            await ws.send_json({"type": "state_update", "data": {

                "current_gear": 1, "gear_name": "EMBRACE",

                "control_entropy": 0.0, "event_type": "RESET",

            }})

        except Exception:

            pass



    resp = {"success": True, "message": "状态已重置", "current_gear": 1}

    if warning_msg:

        resp["warning"] = warning_msg

    return JSONResponse(resp)




# ========== 记忆 API ==========



@router.get("/api/memory/recent")

async def get_recent_memories(limit: int = 5, msg_type: str = None):

    try:

        memories = MemoryStore.get_recent(limit=limit, msg_type=msg_type)

        return JSONResponse({"success": True, "memories": memories, "total": len(memories)})

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.post("/api/memory/add")

async def add_memory(request: Request):

    try:

        data = await request.json()

        content = data.get("content", "")

        if not content:

            return JSONResponse({"success": False, "error": "内容不能为空"}, status_code=400)

        result = MemoryStore.add(

            content=content, msg_type=data.get("type", "manual_note"),

            session_id=data.get("session_id"), model=data.get("model"), gear=data.get("gear"),

        )

        return JSONResponse(result)

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.post("/api/memory/summarize")

async def summarize_session(request: Request):

    try:

        data = await request.json()

        session_id = data.get("session_id", "manual")

        conversation = data.get("conversation", [])

        if not conversation:

            return JSONResponse({"success": False, "error": "对话内容不能为空"}, status_code=400)

        summary = generate_summary_text(conversation)

        result = MemoryStore.add(content=summary, msg_type="summary", session_id=session_id,

                                 metadata={"conversation_length": len(conversation)})

        return JSONResponse(result)

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.get("/api/memory/by-session/{session_id}")

async def get_session_memories(session_id: str):

    try:

        memories = MemoryStore.get_by_session(session_id)

        return JSONResponse({"success": True, "session_id": session_id, "memories": memories, "total": len(memories)})

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




@router.get("/api/memory/context")

async def get_memory_context(limit: int = 5):

    try:

        summaries = MemoryStore.get_recent(limit=limit, msg_type="summary")

        if not summaries:

            return JSONResponse({"success": True, "context": "", "memories": []})

        context_parts = []

        for m in summaries:

            gear_info = f"[{m.get('gear_name', 'N/A')}]" if m.get("gear_name") else ""

            context_parts.append(f"{m['timestamp'][:10]} {gear_info}: {m['content']}")

        context = "\n\n---\n".join(context_parts)

        return JSONResponse({

            "success": True,

            "context": f"【历史会话摘要】\n{context}\n\n请结合以上摘要保持对话连贯性。",

            "memories": summaries,

        })

    except Exception as e:

        return JSONResponse({"success": False, "error": str(e)}, status_code=500)




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




# ========== 多智能体协同 API ==========



@router.post("/api/multi-agent")

async def multi_agent_task(request: Request):

    """

    多智能体协同接口。

    协调两个 Agent 通过留言板（MemoryStore）完成协作任务。

    Agent A 分析并写留言板 → Agent B 读取并生成建议。

    """

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

    results = {

        "session_id": session_id,

        "task": task,

        "agents": [],

        "steps": [],

    }



    # ---------- Step 1: Agent A 分析任务并写入留言板 ----------

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



请分析完成后，使用 write_board 工具将结构化的分析结果写入留言板，然后回复用户（我）已完成分析。

"""



    result_a = await gear_aware_call(

        model_id=agent_a,

        message=prompt_a,

        gear=gear,

        upgrade_retry=False,

        memory_context=None,

        actor=f"multi-agent:{agent_a}",

    )



    results["agents"].append({

        "agent": agent_a,

        "role": "analyzer",

        "success": result_a.get("success", False),

        "reply": result_a.get("reply", ""),

        "tool_calls": result_a.get("tool_calls") or [],

        "tool_call_count": len(result_a.get("tool_calls") or []),

    })



    # ---------- Step 2: 读取留言板内容 ----------

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



    # ---------- Step 3: Agent B 基于留言板生成建议 ----------

    prompt_b = f"""你是 Agent B ({agent_b})。Agent A ({agent_a}) 已经完成了分析并将结果写入了留言板。



留言板最近内容：

{board_content[-2000:] if board_content else '(empty)'}



请阅读留言板内容，生成针对任务「{task}」的改进建议。回复中请包含：

1. 当前状况总结

2. 潜在风险点

3. 具体改进建议（优先级排序）



如果有具体可执行的建议，可以使用 run_shell 工具验证或执行。

"""



    result_b = await gear_aware_call(

        model_id=agent_b,

        message=prompt_b,

        gear=gear,

        upgrade_retry=False,

        memory_context=None,

        actor=f"multi-agent:{agent_b}",

    )



    results["agents"].append({

        "agent": agent_b,

        "role": "advisor",

        "success": result_b.get("success", False),

        "reply": result_b.get("reply", ""),

        "tool_calls": result_b.get("tool_calls") or [],

        "tool_call_count": len(result_b.get("tool_calls") or []),

    })



    # ---------- 最终结果汇总 ----------

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
