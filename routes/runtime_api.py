"""
Entropy Runtime · Runtime REST API
状态查询、审计、配置、安全相关端点
（不随 Agent 框架变动）
"""
import os
import json
import time
import logging
import subprocess
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from config import Config, GEAR_MAP
from audit import state
from tools import GEAR_TOOLS
from models import MODEL_REGISTRY, gear_aware_call, parse_upgrade_request
from memory import MemoryStore, generate_summary_text, auto_summarize_session
from routes.ws import active_connections

logger = logging.getLogger("entropyruntime")

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
            detail="Unauthorized: 无效或缺失 API Token。请通过 Authorization: Bearer <token> 或 X-API-Key: <token> 传递。",
        )
    return True

router = APIRouter(dependencies=[Depends(verify_api_token)])


# ========== 只读状态 API ==========

@router.get("/api/state")
async def get_state():
    state._load_events()
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
        "last_termination_reason": next(
            (e.get("termination_reason") for e in reversed(state.event_log) if e.get("termination_reason")),
            None
        ),
        "confirmation_mode": "step_by_step" if state.current_gear <= 2
        else ("batch" if state.current_gear == 3 else "notify_on_error"),
    }


@router.get("/api/events")
async def get_events(limit: int = 50):
    state._load_events()
    return {"total_events": len(state.event_log), "events": state.event_log[-limit:]}


@router.post("/api/events")
async def create_event(request: Request):
    """接收外部事件（如 AutoGPT 命令审计）并写入审计链"""
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
        return JSONResponse({"success": True, "event_id": len(state.event_log), "event": event})
    except Exception as e:
        logger.error(f"[POST /api/events] Error: {e}")
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
            "EMBRACE": gear_distribution.get(1, 0),
            "EXPLORE": gear_distribution.get(2, 0),
            "ADAPT": gear_distribution.get(3, 0),
            "LET_GO": gear_distribution.get(4, 0),
        },
        "tool_stats": tool_stats,
        "model_stats": model_stats,
        "risk_events_count": risk_count,
        "sc_trend": sc_trend[-50:],
        "uptime_hours": round((time.time() - state.last_switch_time) / 3600, 1),
        "avg_response_time_ms": round(avg_response_time * 1000, 2) if avg_response_time else 0,
    }


# ========== 确认粒度 API ==========

@router.post("/api/batch-approve")
async def batch_approve(request: Request):
    data = await request.json()
    approved = data.get("approved", True)
    if approved:
        state.batch_approved = True
        return JSONResponse({"success": True, "message": "批量确认成功", "steps_confirmed": 5})
    else:
        state.batch_approved = False
        return JSONResponse({"success": True, "message": "已拒绝，切换到 EMBRACE 档位"})


@router.get("/api/notifications")
async def get_notifications(request: Request):
    limit = int(request.query_params.get("limit", 5))
    notifications = []
    for event in reversed(state.event_log[-50:]):
        if event.get("event_type") in ["CIRCUIT_BREAKER", "ERROR", "SC_OVERFLOW", "TOOL_MISUSE"]:
            notifications.append({
                "timestamp": event.get("timestamp"),
                "type": event.get("event_type"),
                "action": event.get("action"),
                "severity": "high" if event.get("event_type") == "CIRCUIT_BREAKER" else "medium",
            })
        if len(notifications) >= limit:
            break
    return JSONResponse({"notifications": notifications, "count": len(notifications)})


@router.get("/api/events/authorized-actions")
async def get_authorized_actions(request: Request):
    limit = int(request.query_params.get("limit", 50))
    authorized = []
    for event in reversed(state.event_log[-limit:]):
        if event.get("actor") == "human" and event.get("proposer") == "human":
            authorized.append({
                "timestamp": event.get("timestamp"),
                "action": event.get("action"),
                "event_type": event.get("event_type"),
            })
        elif event.get("actor") == "autogpt":
            authorized.append({
                "timestamp": event.get("timestamp"),
                "action": event.get("action"),
                "event_type": event.get("event_type"),
                "note": "AutoGPT autonomous action (audit tracked)",
            })
    return JSONResponse({
        "authorized_actions": authorized,
        "count": len(authorized),
        "context": "These actions were performed by the system administrator or authorized AI agents. They are NOT indicators of compromise.",
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


# ========== 健康检查 ==========

@router.get("/api/health")
async def health_check(request: Request):
    """Pure deterministic health indicators, no LLM dependency"""
    try:
        result = subprocess.run(
            ["/usr/local/bin/entropyruntime-healthcheck.sh"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip() or "healthcheck failed"}, status_code=500)
        data = json.loads(result.stdout)
        return JSONResponse(data)
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid healthcheck output"}, status_code=500)
    except FileNotFoundError:
        return JSONResponse({"error": "healthcheck script not found"}, status_code=500)
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "healthcheck timed out"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
