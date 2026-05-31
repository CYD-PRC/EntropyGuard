"""
Entropy Runtime · MessageBoard REST API
多智能体消息系统的 HTTP 接口层
"""
import logging
from typing import Optional

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

logger = logging.getLogger("entropyruntime.messageboard_api")

# —— 模块级 board 引用，由 main.py 在 startup 时注入 ——
_board = None
_ws_connections: list = []


def set_board(board):
    global _board
    _board = board


def get_board():
    return _board


router = APIRouter(prefix="/api/messageboard", tags=["messageboard"])


# ========== 发送消息 ==========

@router.post("/send")
async def mb_send(request: Request):
    """向 MessageBoard 发送一条消息"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    from_agent = data.get("from_agent", "anonymous")
    to_agent = data.get("to_agent", "broadcast")
    message_type = data.get("message_type", data.get("type", "notification"))
    content = data.get("content", "")
    priority = int(data.get("priority", 5))
    ttl = int(data.get("ttl", 300))
    reply_to = data.get("reply_to")

    # content 包装为 dict（MessageBoard 期望 Dict[str, Any]）
    if isinstance(content, str):
        content = {"text": content}
    elif not isinstance(content, dict):
        content = {"data": content}

    try:
        msg_id = _board.send(
            from_agent=from_agent,
            to_agent=to_agent,
            message_type=message_type,
            content=content,
            priority=priority,
            ttl=ttl,
            reply_to=reply_to,
        )

        await _broadcast({
            "type": "messageboard_new",
            "data": {
                "id": msg_id,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "message_type": message_type,
                "priority": priority,
            },
        })

        return JSONResponse({"success": True, "message_id": msg_id})

    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


# ========== 回复消息 ==========

@router.post("/reply")
async def mb_reply(request: Request):
    """回复一条已有消息"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    reply_to_msg_id = data.get("reply_to_msg_id")
    content = data.get("content", "")
    from_agent = data.get("from_agent")

    if not reply_to_msg_id:
        return JSONResponse({"success": False, "error": "reply_to_msg_id 不能为空"}, status_code=400)

    if isinstance(content, str):
        content = {"text": content}
    elif not isinstance(content, dict):
        content = {"data": content}

    try:
        msg_id = _board.send_reply(
            reply_to_msg_id=reply_to_msg_id,
            content=content,
            from_agent=from_agent,
        )
        return JSONResponse({"success": True, "message_id": msg_id})
    except ValueError as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=400)


# ========== 收件箱 ==========

@router.get("/inbox/{agent_name}")
async def mb_inbox(agent_name: str, limit: Optional[int] = None):
    """获取指定 Agent 的收件箱"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    messages = _board.get_inbox(agent_name=agent_name, limit=limit)

    return JSONResponse({
        "success": True,
        "agent": agent_name,
        "count": len(messages),
        "messages": messages,
    })


# ========== 获取单条消息 ==========

@router.get("/message/{message_id}")
async def mb_get_message(message_id: str):
    """获取单条消息详情"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    msg = _board.get_message(message_id)
    if msg:
        return JSONResponse({"success": True, "message": msg})
    return JSONResponse({"success": False, "error": "消息不存在或已过期"}, status_code=404)


# ========== 标记已读 ==========

@router.post("/received/{message_id}")
async def mb_mark_received(message_id: str, request: Request):
    """标记消息为已接收"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    agent_name = data.get("agent_name", "unknown")

    success = _board.mark_received(message_id, agent_name)
    return JSONResponse({"success": success, "message_id": message_id})


# ========== ACK 确认 ==========

@router.post("/ack/{message_id}")
async def mb_ack(message_id: str, request: Request):
    """确认（ACK）一条消息"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    agent_name = data.get("agent_name", "unknown")

    success = _board.ack(message_id, agent_name)
    if success:
        await _broadcast({
            "type": "messageboard_ack",
            "data": {"message_id": message_id, "agent_name": agent_name},
        })
        return JSONResponse({"success": True, "message_id": message_id})
    else:
        return JSONResponse({"success": False, "error": f"ACK 失败"}, status_code=404)


# ========== 拒绝消息 ==========

@router.post("/reject/{message_id}")
async def mb_reject(message_id: str, request: Request):
    """拒绝一条消息"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    agent_name = data.get("agent_name", "unknown")
    reason = data.get("reason", "")

    success = _board.reject(message_id, agent_name, reason)
    if success:
        await _broadcast({
            "type": "messageboard_rejected",
            "data": {"message_id": message_id, "agent_name": agent_name, "reason": reason},
        })
        return JSONResponse({"success": True, "message_id": message_id})
    return JSONResponse({"success": False, "error": "拒绝失败"}, status_code=404)


# ========== 会话线程 ==========

@router.get("/conversation/{root_msg_id}")
async def mb_conversation(root_msg_id: str):
    """获取指定消息的完整会话线程"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    messages = _board.get_conversation(root_msg_id)
    return JSONResponse({
        "success": True,
        "root_msg_id": root_msg_id,
        "count": len(messages),
        "messages": messages,
    })


# ========== 统计 ==========

@router.get("/stats")
async def mb_stats(agent_name: Optional[str] = None):
    """获取 MessageBoard 统计信息"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    stats = _board.get_stats(agent_name=agent_name)
    return JSONResponse({"success": True, "stats": stats})


# ========== 记忆系统 API ==========

@router.get("/memory/context")
async def mb_memory_context():
    """
    获取 Hermes 系统提示注入上下文。

    聚合所有 persona/fact/skill 类型的记忆，按类型分组返回，
    供 Hermes 在启动时注入 system prompt。
    """
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    context = {}
    for mt in ("persona", "fact", "skill"):
        memories = _board.get_by_type(memory_type=mt, limit=50)
        if memories:
            context[mt] = [m.get("content", {}).get("text", str(m.get("content", ""))) for m in memories]

    return JSONResponse({
        "success": True,
        "context": context,
        "has_persona": "persona" in context,
        "has_facts": "fact" in context,
        "has_skills": "skill" in context,
    })


@router.post("/memory")
async def mb_write_memory(request: Request):
    """写入一条记忆（persona/fact/skill/episode）"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    memory_type = data.get("memory_type", data.get("type", "fact"))
    content = data.get("content", {})
    source = data.get("source", "hermes")
    ttl = int(data.get("ttl", 0))

    if memory_type not in ("persona", "fact", "skill", "episode"):
        return JSONResponse({"success": False, "error": f"不支持的记忆类型: {memory_type}"}, status_code=400)

    if isinstance(content, str):
        content = {"text": content}
    elif not isinstance(content, dict):
        content = {"data": str(content)}

    try:
        msg_id = _board.send_memory(
            memory_type=memory_type,
            content=content,
            source=source,
            ttl=ttl,
        )
        return JSONResponse({"success": True, "memory_id": msg_id, "type": memory_type})
    except Exception as e:
        return JSONResponse({"success": False, "error": str(e)}, status_code=500)


@router.get("/memory/{memory_type}")
async def mb_query_memory(
    memory_type: str,
    limit: Optional[int] = None,
    source: Optional[str] = None,
):
    """按类型查询记忆（persona/fact/skill/episode）"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    if memory_type not in ("persona", "fact", "skill", "episode"):
        return JSONResponse({"success": False, "error": f"不支持的记忆类型: {memory_type}"}, status_code=400)

    memories = _board.get_by_type(memory_type=memory_type, limit=limit, source=source)
    return JSONResponse({
        "success": True,
        "type": memory_type,
        "count": len(memories),
        "memories": memories,
    })


# ========== 清空留言板 ==========

@router.post("/clear")
async def mb_clear(request: Request):
    """清空 MessageBoard（需要确认）"""
    if not _board:
        return JSONResponse({"success": False, "error": "MessageBoard 未初始化"}, status_code=503)

    data = await request.json()
    confirmed = data.get("confirmed", False)

    if not confirmed:
        return JSONResponse({
            "success": False,
            "error": "需要 confirmed=true",
            "current_count": len(_board.messages),
        }, status_code=400)

    count = len(_board.messages)
    _board.messages.clear()
    _board._inbox.clear()

    await _broadcast({"type": "messageboard_cleared", "data": {"count": count}})
    return JSONResponse({"success": True, "deleted_count": count})


# ========== WebSocket 辅助 ==========

async def _broadcast(payload: dict):
    for ws in _ws_connections[:]:
        try:
            await ws.send_json(payload)
        except Exception:
            pass


def setup_messageboard_ws(app):
    @app.websocket("/ws/messageboard")
    async def ws_messageboard(websocket: WebSocket):
        await websocket.accept()
        _ws_connections.append(websocket)

        if _board:
            stats = _board.get_stats()
            await websocket.send_json({
                "type": "messageboard_init",
                "data": {"stats": stats},
            })

        try:
            while True:
                msg = await websocket.receive_json()
                if msg.get("type") == "ping":
                    await websocket.send_json({"type": "pong"})
        except WebSocketDisconnect:
            if websocket in _ws_connections:
                _ws_connections.remove(websocket)
        except Exception:
            if websocket in _ws_connections:
                _ws_connections.remove(websocket)
