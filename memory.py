"""
EntropyGuard · 记忆持久化层
留言板 + 会话摘要 + 跨会话记忆
"""
import json
import os
import uuid
import logging
from datetime import datetime

from config import Config, GEAR_MAP

logger = logging.getLogger("entropyguard")

MEMORY_FILE = os.path.join(Config.DATA_DIR, "memory.json")


class MemoryStore:
    @staticmethod
    def _load() -> dict:
        try:
            if os.path.exists(MEMORY_FILE):
                with open(MEMORY_FILE, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {"memories": [], "created_at": datetime.utcnow().isoformat() + "Z"}

    @staticmethod
    def _save(data: dict) -> None:
        with open(MEMORY_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def add(content: str, msg_type: str, session_id: str = None,
            model: str = None, gear: int = None, metadata: dict = None) -> dict:
        data = MemoryStore._load()
        memory = {
            "id": str(uuid.uuid4())[:8],
            "timestamp": datetime.utcnow().isoformat() + "Z",
            "type": msg_type,
            "session_id": session_id,
            "model": model,
            "gear": gear,
            "gear_name": GEAR_MAP.get(gear, {}).get("name", "UNKNOWN") if gear else None,
            "content": content,
            "metadata": metadata or {},
        }
        if "memories" not in data:
            data["memories"] = []
        data["memories"].append(memory)
        MemoryStore._save(data)
        return {"success": True, "memory_id": memory["id"], "total": len(data["memories"])}

    @staticmethod
    def get_recent(limit: int = 5, msg_type: str = None) -> list:
        data = MemoryStore._load()
        memories = data.get("memories", [])
        if msg_type:
            memories = [m for m in memories if m.get("type") == msg_type]
        return memories[-limit:] if limit > 0 else memories

    @staticmethod
    def get_by_session(session_id: str) -> list:
        data = MemoryStore._load()
        return [m for m in data.get("memories", []) if m.get("session_id") == session_id]

    @staticmethod
    def clear_session(session_id: str) -> int:
        data = MemoryStore._load()
        original_count = len(data.get("memories", []))
        data["memories"] = [
            m for m in data.get("memories", [])
            if m.get("session_id") != session_id or m.get("type") == "summary"
        ]
        MemoryStore._save(data)
        return original_count - len(data.get("memories", []))


def generate_summary_text(conversation: list) -> str:
    if not conversation:
        return "空对话"
    user_msgs = [m.get("content", "")[:100] for m in conversation if m.get("role") == "user"][:3]
    last_ai = conversation[-1].get("content", "")[:200] if conversation else ""
    summary = f"用户询问: {'; '.join(user_msgs)}"
    if last_ai:
        summary += f"\nAI回复要点: {last_ai[:150]}..."
    return summary


def auto_summarize_session(state_obj, session_id: str) -> int:
    if not state_obj.event_log:
        return 0
    event_types = {}
    tools_used = []
    upgrade_attempts = []
    for ev in state_obj.event_log[-20:]:
        etype = ev.get("event_type", "")
        event_types[etype] = event_types.get(etype, 0) + 1
        if etype == "TOOL_CALL":
            tools_used.append(ev.get("tool_name", "?"))
        if etype in ("UPGRADE_REQUEST", "UPGRADE_APPROVE", "UPGRADE_REJECT"):
            upgrade_attempts.append(etype)

    summary_parts = []
    if event_types.get("TOOL_CALL"):
        summary_parts.append(f"使用了工具: {', '.join(list(set(tools_used))[:5])}")
    if upgrade_attempts:
        summary_parts.append(f"权限变更: {len(upgrade_attempts)}次")
    if event_types.get("OUTPUT_VIOLATION"):
        summary_parts.append(f"触发输出限制: {event_types['OUTPUT_VIOLATION']}次")

    summary = (
        f"会话包含 {len(state_obj.event_log)} 个事件。" + "; ".join(summary_parts)
        if summary_parts
        else f"会话包含 {len(state_obj.event_log)} 个事件，无特殊操作。"
    )

    MemoryStore.add(
        content=summary, msg_type="summary", session_id=session_id,
        model="system",
        metadata={"event_count": len(state_obj.event_log), "event_types": event_types},
    )
    return 1
