"""Entropy Runtime · 记忆系统模块
v5.0: episode 读写、经验检索、技能沉淀、生命周期管理。
"""
import json
import logging
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Any, Optional

logger = logging.getLogger("entropyruntime.memory")

ENTROPY_API_BASE = "http://127.0.0.1:5000"

# 停用词（用于 _find_similar_episodes 关键词提取）
_STOP_WORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不",
    "人", "都", "一", "一个", "上", "也", "很", "到", "说",
    "要", "去", "你", "会", "着", "没有", "看", "好", "自己",
    "这", "他", "她", "它", "们", "那", "进行", "对", "用",
    "the", "a", "an", "is", "are", "was", "were", "be",
    "been", "have", "has", "had", "do", "does", "did",
    "will", "would", "can", "could", "may", "might",
    "this", "that", "these", "those", "with", "for",
    "of", "to", "in", "on", "at", "by", "from", "as",
}


def _get_api_key() -> str:
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


def _memory_api_post(endpoint: str, payload: dict) -> dict:
    """向 MessageBoard 内存 API 发送 POST"""
    url = f"{ENTROPY_API_BASE}{endpoint}"
    api_key = _get_api_key()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        logger.warning(f"[Memory] API POST {endpoint} 失败: {e}")
        return {"success": False}


def _memory_api_get(path: str) -> dict:
    """向 MessageBoard 内存 API 发送 GET"""
    try:
        api_key = _get_api_key()
        req = urllib.request.Request(f"{ENTROPY_API_BASE}{path}")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=5) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        logger.warning(f"[Memory] API GET {path} 失败: {e}")
        return {"success": False}


def read_recent_episodes(limit: int = 10) -> list[dict]:
    """从 MessageBoard 读取最近 N 条 episode 记忆"""
    resp = _memory_api_get(f"/api/messageboard/memory/episode?limit={limit}")
    if resp.get("success"):
        return resp.get("memories", [])
    return []


def write_episode(task_id: str, agent: str, success: bool,
                  output_preview: str = "", error: str = "",
                  duration: float = 0.0, source: str = "orchestrator",
                  **extra) -> bool:
    """将任务执行结果写入 MessageBoard episode 记忆"""
    content = {
        "task_id": task_id,
        "agent": agent,
        "success": success,
        "output_preview": output_preview[:200] if output_preview else "",
        "error": error,
        "duration": duration,
    }
    content.update(extra)
    payload = {
        "memory_type": "episode",
        "content": content,
        "source": source,
        "ttl": 0,
    }
    resp = _memory_api_post("/api/messageboard/memory", payload)
    if resp.get("success"):
        return True
    logger.warning(f"[Memory] episode 写入失败: {task_id}")
    return False


def write_skill_memory(task_type: str, agent: str, steps: list[str],
                       success_rate: float, avg_duration: float,
                       total_count: int) -> bool:
    """沉淀程序性记忆到 MessageBoard type=skill

    成功任务的学习经验沉淀为可复用的技能。
    """
    content = {
        "task_type": task_type,
        "agent": agent,
        "steps": steps,
        "success_rate": round(success_rate * 100, 1),
        "avg_duration": round(avg_duration, 1),
        "total_count": total_count,
        "generated": datetime.utcnow().isoformat() + "Z",
    }
    payload = {
        "memory_type": "skill",
        "content": content,
        "source": "orchestrator:v5.0",
        "ttl": 0,
    }
    resp = _memory_api_post("/api/messageboard/memory", payload)
    if resp.get("success"):
        logger.info(f"[Memory v5.0] 技能已沉淀: {task_type} → {agent}")
        return True
    logger.warning(f"[Memory v5.0] 技能沉淀失败: {task_type}")
    return False


def find_skill_by_task_type(task_type: str) -> list[dict]:
    """查询与任务类型匹配的程序性记忆"""
    resp = _memory_api_get(f"/api/messageboard/memory/skill?limit=30")
    if not resp.get("success"):
        return []
    memories = resp.get("memories", [])
    task_lower = task_type.lower()
    matched = []
    for mem in memories:
        c = mem.get("content", {})
        stored_type = (c.get("task_type", "") or "").lower()
        if task_lower in stored_type or stored_type in task_lower:
            matched.append(c)
    return matched


def get_agent_success_rate(task_type: str, agent: str) -> float:
    """查询指定 Agent 在同类任务上的成功率，无数据返回 0.5（中性）"""
    episodes = read_recent_episodes(limit=50)
    matched = [ep for ep in episodes
               if ep.get("content", {}).get("agent") == agent
               and task_type.lower() in str(ep.get("content", {})).lower()]
    if not matched:
        return 0.5
    successes = sum(1 for ep in matched if ep["content"].get("success", False))
    return successes / len(matched)


def build_experience_context(goal: str) -> str:
    """从 MessageBoard 检索历史 episode，统计成功率，生成经验文本"""
    episodes = read_recent_episodes(limit=20)
    if not episodes:
        return ""

    agent_stats: dict[str, dict] = {}
    type_keywords = {
        "代码分析": ["code", "代码", "review", "审查", "分析", "analyze"],
        "安全扫描": ["security", "安全", "scan", "扫描", "vuln", "漏洞"],
        "依赖检查": ["dependency", "依赖", "safety", "pip"],
        "端口扫描": ["port", "端口", "nmap"],
        "文件操作": ["file", "文件", "read", "write", "cat", "ls"],
        "shell 执行": ["shell", "bash", "命令", "execute", "执行"],
    }

    for ep in episodes:
        c = ep.get("content", {})
        agent = c.get("agent", "unknown")
        success = c.get("success", False)
        preview = (c.get("output_preview", "") or "") + (c.get("error", "") or "")
        if agent not in agent_stats:
            agent_stats[agent] = {"total": 0, "success": 0}
        agent_stats[agent]["total"] += 1
        if success:
            agent_stats[agent]["success"] += 1

    lines = ["=== 历史经验（基于近期 episode 统计） ==="]
    lines.append("[Agent 历史表现]")
    for agent, stats in sorted(agent_stats.items(),
                                key=lambda x: x[1]["success"] / max(x[1]["total"], 1),
                                reverse=True):
        rate = stats["success"] / max(stats["total"], 1)
        lines.append(f"  {agent}: {rate*100:.0f}% 成功率 ({stats['success']}/{stats['total']})")

    # 耗时参考
    durations = [ep.get("content", {}).get("duration", 0) for ep in episodes[:10]
                 if ep.get("content", {}).get("duration", 0) > 0]
    if durations:
        avg_dur = sum(durations) / len(durations)
        lines.append(f"  近期平均任务耗时: {avg_dur:.1f}s")

    return "\n".join(lines)


def find_similar_episodes(goal: str) -> list[dict]:
    """关键词匹配查找历史中相似目标的 episode"""
    goal_lower = goal.lower()
    tokens = re.findall(r'[a-zA-Z\u4e00-\u9fff]+', goal_lower)
    keywords = [t for t in tokens if t not in _STOP_WORDS and len(t) > 1]
    if not keywords:
        return []

    episodes = read_recent_episodes(limit=50)
    scored: list[tuple[dict, int]] = []
    for ep in episodes:
        c = ep.get("content", {})
        preview = ((c.get("output_preview", "") or "") + " " +
                   (c.get("error", "") or "") + " " +
                   (c.get("task_id", "") or "")).lower()
        score = sum(1 for kw in keywords if kw.lower() in preview)
        if score > 0:
            scored.append((ep, score))
    scored.sort(key=lambda x: x[1], reverse=True)
    return [ep for ep, _ in scored[:5]]


def manage_episode_lifecycle():
    """episode 生命周期管理：>100 条时自动合并为 summary fact，过期写入 skill"""
    episodes = read_recent_episodes(limit=200)
    if len(episodes) < 100:
        return
    logger.info(f"[Memory v5.0] episode 数 {len(episodes)} ≥ 100，开始生命周期管理")

    agent_groups: dict[str, list[dict]] = {}
    for ep in episodes[50:]:
        c = ep.get("content", {})
        agent = c.get("agent", "unknown")
        agent_groups.setdefault(agent, []).append(c)

    for agent, group in agent_groups.items():
        total = len(group)
        successes = sum(1 for g in group if g.get("success", False))
        avg_duration = sum(g.get("duration", 0) for g in group) / max(total, 1)
        failed_tasks = [g.get("task_id", "?") for g in group if not g.get("success", False)]

        summary_content = {
            "agent": agent, "type": "summary",
            "total_episodes": total,
            "success_rate": f"{successes}/{total}",
            "success_rate_pct": round(successes / max(total, 1) * 100, 1),
            "avg_duration": round(avg_duration, 1),
            "failed_count": len(failed_tasks),
            "failed_tasks_sample": failed_tasks[:5],
            "generated": datetime.utcnow().isoformat() + "Z",
        }
        payload = {
            "memory_type": "fact",
            "content": summary_content,
            "source": "orchestrator:v5.0-lifecycle",
            "ttl": 0,
        }
        _memory_api_post("/api/messageboard/memory", payload)

        # 如果成功率不错，提取为 skill
        if successes > 0 and successes >= total // 2:
            skill_content = {
                "agent": agent, "type": "experience",
                "recommendation": (
                    f"Agent {agent} 在 {total} 次同类任务中成功率 "
                    f"{summary_content['success_rate_pct']}%，"
                    f"平均耗时 {avg_duration:.1f}s。适合处理类似任务。"
                ),
                "caveats": f"失败次数: {len(failed_tasks)}",
                "generated": datetime.utcnow().isoformat() + "Z",
            }
            payload = {
                "memory_type": "skill",
                "content": skill_content,
                "source": "orchestrator:v5.0-lifecycle",
                "ttl": 0,
            }
            _memory_api_post("/api/messageboard/memory", payload)
    logger.info(f"[Memory v5.0] 生命周期管理完成")
