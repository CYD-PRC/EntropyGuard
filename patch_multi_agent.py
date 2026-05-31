#!/usr/bin/env python3
"""
Entropy Runtime · multi-agent 端点升级脚本
将 MemoryStore 留言板读写切换到 MessageBoard
"""
import os, sys

API_FILE = "/root/EntropyGuard/routes/api.py"
BAK_FILE = API_FILE + f".bak.mb_multi_{int(__import__('time').time())}"

with open(API_FILE, "r", encoding="utf-8") as f:
    content = f.read()

# 备份
with open(BAK_FILE, "w", encoding="utf-8") as f:
    f.write(content)
print(f"Backup: {BAK_FILE}")

# ---- Patch 1: 添加 import ----
old_import = "from memory import MemoryStore, generate_summary_text, auto_summarize_session"
new_import = old_import + "\nfrom messageboard import get_messageboard"
assert old_import in content, "import line not found"
content = content.replace(old_import, new_import, 1)
print("Patch 1: import added")

# ---- Patch 2: 更新 prompt_a 指导 Agent A 使用 session_id ----
old_prompt_a = '''留言板消息格式（必须使用 write_board 工具写入）：

{{
  "from": "{agent_a}",
  "to": "{agent_b}",
  "type": "analysis_result",
  "content": "你的分析结果内容...",
  "timestamp": "<ISO时间戳>"
}}'''

new_prompt_a = f'''使用 write_board 工具写入留言板，设置参数：
- content: 你的分析结果（JSON 格式，包含分析摘要、发现的问题、建议等）
- to_agent: "board"
- session_id: "{{session_id}}" (本次多智能体会话 ID)'''

assert old_prompt_a in content, "prompt_a old text not found"
content = content.replace(old_prompt_a, new_prompt_a, 1)
print("Patch 2: prompt_a updated")

# ---- Patch 3: 替换 Step 2 读取留言板 ----
old_read_board = '''    # ---------- Step 2: 读取留言板内容 ----------
    board_memories = MemoryStore.get_recent(limit=20)
    board_content = "\\n".join(
        f"[{m.get('timestamp','?')}] {m.get('content','')}"
        for m in board_memories[-10:]
    )
    results["steps"].append({
        "step": "read_board",
        "board_messages_count": len(board_memories),
        "content_preview": board_content[-1500:] if board_content else "(empty)",
    })'''

new_read_board = '''    # ---------- Step 2: 读取留言板内容 (MessageBoard v2) ----------
    board = get_messageboard()
    all_board_msgs = board.get_inbox("board", limit=50)
    # 按 session_id 过滤本次会话的消息
    if session_id:
        board_messages = [
            m for m in all_board_msgs
            if (m.get("metadata") or {}).get("session_id") == session_id
        ]
    else:
        board_messages = all_board_msgs[-20:]

    def _fmt_content(c):
        if isinstance(c, dict):
            return c.get("text", json.dumps(c, ensure_ascii=False))
        return str(c)

    board_content = "\\n".join(
        f"[{m.get('timestamp','?')}] [{m.get('from_agent','?')}] {_fmt_content(m.get('content',''))}"
        for m in board_messages[-10:]
    )
    results["steps"].append({
        "step": "read_board_v2",
        "board_messages_count": len(board_messages),
        "board_total": len(all_board_msgs),
        "session_id": session_id,
        "content_preview": board_content[-1500:] if board_content else "(empty)",
    })'''

assert old_read_board in content, "read_board old text not found"
content = content.replace(old_read_board, new_read_board, 1)
print("Patch 3: read_board section updated")

# ---- Patch 4: 更新 prompt_b 提示 ----
old_prompt_b_hint = "Agent A ({agent_a}) 已经完成了分析并将结果写入了留言板。"
new_prompt_b_hint = "Agent A ({agent_a}) 已经完成了分析并将结果写入了留言板（会话 ID: {session_id}）。"

assert old_prompt_b_hint in content, "prompt_b old hint not found"
content = content.replace(old_prompt_b_hint, new_prompt_b_hint, 1)
print("Patch 4: prompt_b hint updated")

# 写回
with open(API_FILE, "w", encoding="utf-8") as f:
    f.write(content)

print(f"\nAll 4 patches applied successfully to {API_FILE}")
print("Restart entropyruntime service to take effect")
