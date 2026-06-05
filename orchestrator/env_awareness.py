"""Entropy Runtime · 环境感知模块
v5.0: 收集服务器和安全模块实时状态，格式化注入 LLM prompt。
"""
import json
import logging
import os
import re
import subprocess as _subprocess
from collections import Counter
from typing import Optional

logger = logging.getLogger("entropyruntime.env")

ENTROPY_API_BASE = "http://127.0.0.1:5000"


def _get_api_key() -> str:
    """读取 Entropy Runtime API Key"""
    env_path = "/root/.env"
    for key_name in ["ENTROPY_RUNTIME_API_KEY"]:
        try:
            with open(env_path) as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith(key_name) and "=" in ls:
                        return ls.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            pass
    return os.environ.get("ENTROPY_RUNTIME_API_KEY", "")


def _api_get(path: str) -> dict:
    """带认证的 GET 请求（不抛出异常）"""
    try:
        import urllib.request
        api_key = _get_api_key()
        req_str = f"{ENTROPY_API_BASE}{path}"
        req = urllib.request.Request(req_str)
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception:
        return {}


def get_server_state() -> dict:
    """收集服务器实时状态（CPU/内存/磁盘/端口/安全事件）"""
    state = {
        "cpu_percent": 0.0,
        "memory_percent": 0.0,
        "disk_percent": 0.0,
        "open_ports": [],
        "entropy_guard_uptime": 0.0,
        "entropy_guard_memory_mb": 0.0,
        "recent_blocks": [],
        "recent_failures": [],
    }
    try:
        with open("/proc/stat") as f:
            fields = f.readline().split()
            total = sum(int(v) for v in fields[1:] if v.isdigit())
            idle = int(fields[4])
            state["cpu_percent"] = round(100 * (1 - idle / max(total, 1)), 1)
    except Exception:
        pass
    try:
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                parts = line.split()
                if parts:
                    mem[parts[0].rstrip(":")] = int(parts[1]) // 1024
        total_mem = mem.get("MemTotal", 1)
        avail_mem = mem.get("MemAvailable", total_mem)
        state["memory_percent"] = round(100 * (1 - avail_mem / total_mem), 1)
    except Exception:
        pass
    try:
        st = os.statvfs("/")
        used = (st.f_blocks - st.f_bfree) * st.f_frsize
        total = st.f_blocks * st.f_frsize
        state["disk_percent"] = round(100 * used / max(total, 1), 1)
    except Exception:
        pass
    try:
        r = _subprocess.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=5)
        ports = set()
        for line in r.stdout.split("\n")[1:]:
            m = re.search(r":(\d+)\s", line)
            if m:
                ports.add(int(m.group(1)))
        state["open_ports"] = sorted(ports)
    except Exception:
        pass
    try:
        state_resp = _api_get("/api/state")
        state["entropy_guard_uptime"] = state_resp.get("uptime_seconds", 0)
    except Exception:
        pass
    try:
        with open("/proc/self/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    mb = int(line.split()[1]) // 1024
                    state["entropy_guard_memory_mb"] = mb
                    break
    except Exception:
        pass
    # 最近安全拦截事件
    try:
        ev_path = "/root/EntropyGuard/events.json"
        if os.path.exists(ev_path):
            with open(ev_path) as f:
                ev_data = json.load(f)
            ev_list = ev_data.get("events", [])
            blocks = []
            for e in reversed(ev_list[-50:]):
                action = e.get("action", "") or ""
                etype = e.get("event_type", "") or ""
                if "block" in action.lower() or "block" in etype.lower() or "violation" in etype.lower():
                    blocks.append({
                        "time": e.get("timestamp", ""),
                        "action": action[:80],
                        "actor": e.get("actor", ""),
                    })
                    if len(blocks) >= 10:
                        break
            state["recent_blocks"] = blocks
    except Exception:
        pass
    return state


def get_security_state() -> dict:
    """收集安全模块当前状态（gear/事件数/拦截统计）"""
    sec = {
        "current_gear": 1,
        "total_events": 0,
        "total_blocks": 0,
        "redteam_last_run": None,
        "redteam_pass_rate": None,
        "top_blocked_intents": [],
    }
    try:
        st = _api_get("/api/state")
        sec["current_gear"] = st.get("current_gear", 1)
        sec["total_events"] = st.get("event_count", 0)
    except Exception:
        pass
    try:
        ev_path = "/root/EntropyGuard/events.json"
        if os.path.exists(ev_path):
            with open(ev_path) as f:
                ev_data = json.load(f)
            ev_list = ev_data.get("events", [])
            blocked_actions = []
            for e in ev_list[-500:]:
                action = e.get("action", "") or ""
                etype = e.get("event_type", "") or ""
                if "block" in action.lower() or "violation" in etype.lower():
                    blocked_actions.append(action[:60])
            sec["total_blocks"] = len(blocked_actions)
            if blocked_actions:
                top = Counter(blocked_actions).most_common(5)
                sec["top_blocked_intents"] = [{"intent": i, "count": c} for i, c in top]
    except Exception:
        pass
    return sec


def format_env_context(sv: Optional[dict] = None, sc: Optional[dict] = None) -> str:
    """格式化环境上下文字符串"""
    if sv is None:
        sv = get_server_state()
    if sc is None:
        sc = get_security_state()
    lines = [
        "=== 当前服务器状态 ===",
        f"CPU: {sv['cpu_percent']}% | 内存: {sv['memory_percent']}% | 磁盘: {sv['disk_percent']}%",
        f"开放端口: {sv['open_ports']}",
    ]
    blocks = sv.get("recent_blocks", [])
    if blocks:
        lines.append(f"最近安全事件: {len(blocks)} 条拦截")
        for b in blocks[:3]:
            lines.append(f"  ⛔ {b.get('action','')[:60]}")
    failures = sv.get("recent_failures", [])
    if failures:
        lines.append(f"最近失败任务: {len(failures)} 个")
        for f_item in failures[:3]:
            lines.append(f"  ❌ {f_item['task_id']}: {f_item['error'][:50]}")
    top_blocked = sc.get("top_blocked_intents", [])
    if top_blocked:
        parts = [f'{b["intent"][:40]}({b["count"]}x)' for b in top_blocked[:3]]
        lines.append(f"高拦截意图: {'; '.join(parts)}")
    lines.append(f"当前档位: gear={sc['current_gear']}")
    return "\n".join(lines)


def env_system_prompt(base_prompt: str) -> str:
    """环境增强版 system prompt"""
    ctx = format_env_context()
    return f"{base_prompt}\n\n[系统状态]\n{ctx}\n\n请根据以上服务器当前状态调整拆解策略。"
