"""Entropy Runtime · 持久化跨 Session 记忆存储
v7.2 Phase 3.3: SQLite 持久化 + 时效衰减 + 跨任务关联。

依赖: sqlite3 (Python 标准库)

表结构:
  episodes  — 任务执行记录（episode 记忆）
  skills    — 自动提炼的可用技能（程序性记忆）

双写模式:
  memory.py 写入 MessageBoard API（内存）
  memory_store.py 写入 SQLite（持久化）
  两者共存，优先从 SQLite 读取。
"""
import json
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("entropyruntime.memory_store")

DB_PATH = "/var/lib/entropyguard/memory.db"

# 时效衰减参数
FRESH_DAYS = 7        # 7 天内权重 1.0
DECAY_DAYS = 30       # 7-30 天权重 0.5
STALE_WEIGHT = 0.1    # 30 天以上权重 0.1

# 技能提炼参数
SKILL_MIN_SUCCESS = 3        # 至少 3 次成功才提炼
SKILL_AGENT_SAME = 2         # 同一 agent 至少执行 2 次
SKILL_RATE_THRESHOLD = 0.6   # 成功率 > 60%


# ========== 数据模型 ==========

@dataclass
class Episode:
    """单条任务执行记录"""
    id: int = 0
    task_id: str = ""
    agent: str = ""
    intent: str = ""
    intent_input: str = ""
    output: str = ""
    success: bool = False
    duration: float = 0.0
    timestamp: float = 0.0
    metadata: str = "{}"

    @property
    def weight(self) -> float:
        """计算时效权重。"""
        age_days = (time.time() - self.timestamp) / 86400 if self.timestamp else 999
        if age_days <= FRESH_DAYS:
            return 1.0
        elif age_days <= DECAY_DAYS:
            return 0.5
        else:
            return STALE_WEIGHT

    def to_dict(self) -> dict:
        return {
            "id": self.id, "task_id": self.task_id, "agent": self.agent,
            "intent": self.intent, "output": self.output[:200],
            "success": self.success, "duration": self.duration,
            "timestamp": self.timestamp, "weight": self.weight,
        }


@dataclass
class Skill:
    """从历史经验中自动提炼的可复用技能"""
    id: int = 0
    name: str = ""
    description: str = ""
    task_pattern: str = ""
    agent: str = ""
    success_rate: float = 0.0
    use_count: int = 0
    last_used: float = 0.0
    avg_duration: float = 0.0


class MemoryStore:
    """持久化记忆存储，封装 SQLite 操作。"""

    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        self._init_db()

    # ---- 初始化 ----

    def _init_db(self):
        """创建表结构（如不存在）。"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS episodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    agent TEXT NOT NULL DEFAULT '',
                    intent TEXT NOT NULL DEFAULT '',
                    intent_input TEXT NOT NULL DEFAULT '',
                    output TEXT NOT NULL DEFAULT '',
                    success INTEGER NOT NULL DEFAULT 0,
                    duration REAL NOT NULL DEFAULT 0.0,
                    timestamp REAL NOT NULL DEFAULT (strftime('%s','now')),
                    metadata TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_episodes_task_id ON episodes(task_id);
                CREATE INDEX IF NOT EXISTS idx_episodes_agent ON episodes(agent);
                CREATE INDEX IF NOT EXISTS idx_episodes_timestamp ON episodes(timestamp DESC);
                CREATE INDEX IF NOT EXISTS idx_episodes_success ON episodes(success);

                CREATE TABLE IF NOT EXISTS skills (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT NOT NULL DEFAULT '',
                    task_pattern TEXT NOT NULL DEFAULT '',
                    agent TEXT NOT NULL DEFAULT '',
                    success_rate REAL NOT NULL DEFAULT 0.0,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    last_used REAL NOT NULL DEFAULT 0.0,
                    avg_duration REAL NOT NULL DEFAULT 0.0,
                    created_at REAL NOT NULL DEFAULT (strftime('%s','now'))
                );
                CREATE INDEX IF NOT EXISTS idx_skills_agent ON skills(agent);
                CREATE INDEX IF NOT EXISTS idx_skills_rate ON skills(success_rate DESC);
            """)

    def _conn(self):
        """创建新连接（线程安全：每次调用新建）。"""
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # ---- Episode CRUD ----

    def save_episode(
        self,
        task_id: str,
        agent: str = "",
        intent: str = "",
        intent_input: str = "",
        output: str = "",
        success: bool = False,
        duration: float = 0.0,
        metadata: Optional[dict] = None,
    ) -> int:
        """保存一条 episode 记录。返回新记录的 ID。"""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO episodes
                   (task_id, agent, intent, intent_input, output, success, duration, timestamp, metadata)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, agent, intent, intent_input, output[:2000],
                    int(success), duration, time.time(),
                    json.dumps(metadata or {}, ensure_ascii=False),
                ),
            )
            episode_id = cur.lastrowid
            logger.info(
                "[MemoryStore] episode 已保存: %s (%s) success=%s, id=%d",
                task_id, agent, success, episode_id,
            )
            return episode_id

    def query_episodes(
        self,
        intent_pattern: Optional[str] = None,
        agent: Optional[str] = None,
        limit: int = 20,
        days: Optional[int] = None,
    ) -> list[Episode]:
        """查询 episode，按时效权重排序。

        Args:
            intent_pattern: 关键词匹配 intent
            agent: Agent 名称过滤
            limit: 最大返回数
            days: 只返回最近 N 天的记录
        """
        conditions = []
        params: list[Any] = []

        if agent:
            conditions.append("agent = ?")
            params.append(agent)

        if days:
            cutoff = time.time() - days * 86400
            conditions.append("timestamp >= ?")
            params.append(cutoff)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM episodes WHERE {where} ORDER BY timestamp DESC LIMIT ?"
        params.append(limit * 3)  # 多取一些用于过滤

        results = []
        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            for row in rows:
                ep = Episode(
                    id=row["id"], task_id=row["task_id"],
                    agent=row["agent"], intent=row["intent"],
                    intent_input=row["intent_input"],
                    output=row["output"], success=bool(row["success"]),
                    duration=row["duration"], timestamp=row["timestamp"],
                    metadata=row["metadata"],
                )
                # 关键词过滤
                if intent_pattern:
                    pattern_lower = intent_pattern.lower()
                    if not (pattern_lower in ep.intent.lower() or
                            pattern_lower in ep.intent_input.lower()):
                        continue
                results.append(ep)
                if len(results) >= limit:
                    break

        # 按时效权重排序
        results.sort(key=lambda e: e.weight, reverse=True)
        return results[:limit]

    def get_episode_by_id(self, episode_id: int) -> Optional[Episode]:
        """根据 ID 查询单条 episode。"""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM episodes WHERE id = ?", (episode_id,)
            ).fetchone()
            if row:
                return Episode(
                    id=row["id"], task_id=row["task_id"],
                    agent=row["agent"], intent=row["intent"],
                    intent_input=row["intent_input"],
                    output=row["output"], success=bool(row["success"]),
                    duration=row["duration"], timestamp=row["timestamp"],
                    metadata=row["metadata"],
                )
        return None

    def get_recent_episodes(self, limit: int = 10) -> list[Episode]:
        """获取最近的 episode 记录（无过滤）。"""
        return self.query_episodes(limit=limit)

    # ---- 相似任务检索 ----

    def get_similar_tasks(self, intent: str, limit: int = 5) -> list[Episode]:
        """根据 intent 关键词匹配相似历史任务。"""
        # 提取关键词
        keywords = self._extract_keywords(intent)
        if not keywords:
            return []

        all_recent = self.query_episodes(limit=50)
        scored: list[tuple[Episode, int]] = []

        for ep in all_recent:
            score = sum(1 for kw in keywords if kw in ep.intent.lower())
            if score > 0:
                scored.append((ep, score))

        scored.sort(key=lambda x: (x[1] * x[0].weight), reverse=True)
        return [ep for ep, _ in scored[:limit]]

    def _extract_keywords(self, text: str) -> list[str]:
        """从文本中提取有意义的特征关键词。"""
        _STOP = {
            "的", "了", "在", "是", "我", "有", "和", "就", "不",
            "人", "都", "一", "也", "很", "到", "要", "去", "会",
            "这", "那", "对", "用", "进行", "the", "a", "an",
            "is", "are", "be", "to", "of", "in", "for", "on",
            "with", "at", "by", "from", "do", "does", "did",
            "this", "that", "and", "or", "but", "not",
        }
        tokens = re.findall(r'[a-zA-Z]+|[\u4e00-\u9fff]', text.lower())
        return [t for t in tokens if t not in _STOP and (len(t) > 1 or '\u4e00' <= t <= '\u9fff')]

    # ---- Agent 推荐 ----

    def get_recommended_agent(self, intent: str) -> Optional[str]:
        """根据历史经验推荐最佳 Agent。

        对同类任务统计各 Agent 的成功率，返回成功率最高的。
        无历史数据返回 None。
        """
        similar = self.get_similar_tasks(intent, limit=30)
        if not similar:
            return None

        agent_stats: dict[str, dict] = {}
        for ep in similar:
            if not ep.agent:
                continue
            stats = agent_stats.setdefault(ep.agent, {"total": 0, "success": 0, "weighted": 0.0})
            stats["total"] += 1
            if ep.success:
                stats["success"] += 1
            stats["weighted"] += ep.weight

        if not agent_stats:
            return None

        best_agent = max(
            agent_stats.items(),
            key=lambda x: (
                # Bayesian average: prefer more data
                (x[1]["success"] + 1) / (x[1]["total"] + 2),  # smoothed rate
                x[1]["total"],  # prefer more samples
            ),
        )
        rate = best_agent[1]["success"] / max(best_agent[1]["total"], 1)
        logger.info(
            "[MemoryStore] 推荐 Agent %s (成功率 %.0f%%, %d 次)",
            best_agent[0], rate * 100, best_agent[1]["total"],
        )
        return best_agent[0]

    # ---- 技能管理 ----

    def auto_refine_skills(self) -> list[Skill]:
        """从 episode 中自动提炼技能。

        扫描最近 100 条 episode，对同一 agent 成功多次的同类任务，
        自动生成或更新 skill 记录。
        """
        episodes = self.query_episodes(limit=100)
        if len(episodes) < SKILL_MIN_SUCCESS:
            return []

        # 按 (agent, task_type) 分组
        groups: dict[tuple[str, str], list[Episode]] = {}
        for ep in episodes:
            task_type = self._infer_task_type(ep.intent)
            if not task_type:
                continue
            key = (ep.agent, task_type)
            groups.setdefault(key, []).append(ep)

        new_skills = []
        for (agent, task_type), group in groups.items():
            total = len(group)
            successes = sum(1 for e in group if e.success)
            rate = successes / max(total, 1)
            avg_dur = sum(e.duration for e in group) / max(total, 1)

            if (total >= SKILL_MIN_SUCCESS and
                    successes >= SKILL_AGENT_SAME and
                    rate >= SKILL_RATE_THRESHOLD):
                skill = self._upsert_skill(
                    name=f"{task_type}@{agent}",
                    description=f"{task_type} 使用 {agent} Agent",
                    task_pattern=task_type,
                    agent=agent,
                    success_rate=round(rate, 2),
                    use_count=total,
                    avg_duration=round(avg_dur, 1),
                )
                new_skills.append(skill)

        if new_skills:
            logger.info(
                "[MemoryStore] 自动提炼 %d 个技能", len(new_skills)
            )
        return new_skills

    def _infer_task_type(self, intent: str) -> str:
        """从 intent 推断任务类型。"""
        intent_lower = intent.lower()
        patterns = [
            (["scan", "扫描", "port", "端口", "nmap"], "端口扫描"),
            (["security", "安全", "vuln", "漏洞", "cve"], "安全扫描"),
            (["code", "代码", "review", "审查", "analyze", "分析"], "代码分析"),
            (["file", "文件", "cat", "ls", "read"], "文件操作"),
            (["shell", "bash", "命令", "execute", "执行"], "Shell执行"),
            (["dependency", "依赖", "pip", "safety"], "依赖检查"),
            (["audit", "审计", "log", "日志"], "审计检查"),
            (["performance", "性能", "bottleneck", "瓶颈"], "性能分析"),
            (["report", "报告", "summary", "汇总"], "报告生成"),
            (["test", "测试", "unittest"], "测试执行"),
        ]
        for keywords, task_type in patterns:
            if any(kw in intent_lower for kw in keywords):
                return task_type
        return "通用任务"

    def _upsert_skill(
        self, name: str, description: str, task_pattern: str,
        agent: str, success_rate: float, use_count: int,
        avg_duration: float,
    ) -> Skill:
        """插入或更新技能记录。"""
        with self._conn() as conn:
            existing = conn.execute(
                "SELECT * FROM skills WHERE name = ?", (name,)
            ).fetchone()
            now = time.time()
            if existing:
                conn.execute(
                    """UPDATE skills SET
                       success_rate = ?, use_count = ?, last_used = ?,
                       avg_duration = ?, description = ?
                       WHERE name = ?""",
                    (success_rate, use_count, now, avg_duration, description, name),
                )
                sid = existing["id"]
                logger.info("[MemoryStore] 技能已更新: %s (rate=%.0f%%, count=%d)",
                            name, success_rate * 100, use_count)
            else:
                cur = conn.execute(
                    """INSERT INTO skills
                       (name, description, task_pattern, agent, success_rate,
                        use_count, last_used, avg_duration)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (name, description, task_pattern, agent, success_rate,
                     use_count, now, avg_duration),
                )
                sid = cur.lastrowid
                logger.info("[MemoryStore] 新技能已创建: %s (rate=%.0f%%, count=%d)",
                            name, success_rate * 100, use_count)

            row = conn.execute("SELECT * FROM skills WHERE id = ?", (sid,)).fetchone()
            return Skill(
                id=row["id"], name=row["name"], description=row["description"],
                task_pattern=row["task_pattern"], agent=row["agent"],
                success_rate=row["success_rate"], use_count=row["use_count"],
                last_used=row["last_used"], avg_duration=row["avg_duration"],
            )

    def query_skills(self, task_pattern: Optional[str] = None,
                     min_rate: float = 0.0, limit: int = 20) -> list[Skill]:
        """查询可用技能。"""
        conditions = []
        params: list[Any] = []

        if task_pattern:
            conditions.append("task_pattern LIKE ?")
            params.append(f"%{task_pattern}%")
        if min_rate > 0:
            conditions.append("success_rate >= ?")
            params.append(min_rate)

        where = " AND ".join(conditions) if conditions else "1=1"
        sql = f"SELECT * FROM skills WHERE {where} ORDER BY success_rate DESC, use_count DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [
                Skill(
                    id=r["id"], name=r["name"], description=r["description"],
                    task_pattern=r["task_pattern"], agent=r["agent"],
                    success_rate=r["success_rate"], use_count=r["use_count"],
                    last_used=r["last_used"], avg_duration=r["avg_duration"],
                )
                for r in rows
            ]

    # ---- 统计 ----

    def get_stats(self) -> dict:
        """获取存储统计信息。"""
        with self._conn() as conn:
            ep_count = conn.execute("SELECT COUNT(*) FROM episodes").fetchone()[0]
            sk_count = conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0]
            ep_success = conn.execute(
                "SELECT success, COUNT(*) as c FROM episodes GROUP BY success"
            ).fetchall()
            successes = sum(r["c"] for r in ep_success if r["success"])
            failures = sum(r["c"] for r in ep_success if not r["success"])
            db_size = os.path.getsize(self.db_path) if os.path.exists(self.db_path) else 0

        return {
            "total_episodes": ep_count,
            "total_skills": sk_count,
            "successful_episodes": successes,
            "failed_episodes": failures,
            "database_size_bytes": db_size,
            "database_path": self.db_path,
        }

    def close(self):
        """关闭连接池（兼容接口）。"""
        pass
