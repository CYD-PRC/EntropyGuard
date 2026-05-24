"""
EntropyGuard · 审计核心
FerrymanState：控制熵状态机 + SHA-256 审计链
"""
import json
import hashlib
import math
import time
import logging
import threading
from datetime import datetime

from config import Config, GEAR_MAP

logger = logging.getLogger("entropyguard")


class FerrymanState:
    BASE_SC = {1: 0.0, 2: 0.25, 3: 0.75, 4: 1.5}

    def __init__(self):
        self.current_gear = 1
        self.control_entropy = 0.0
        self.event_log = []
        self.last_switch_time = time.time()
        self.last_activity_time = time.time()
        self.upgrade_reject_time = 0
        self.upgrade_reject_count = 0
        self.UPGRADE_COOLDOWN = Config.UPGRADE_COOLDOWN_SEC
        # [Bug 1 fix] 线程锁保护并发读写
        self._lock = threading.Lock()
        # [Bug 3 fix] 消息去重缓存
        self._recent_messages = {}  # hash -> timestamp
        self._load_events()
        self.pending_proposal = None  # [Fix] switch confirm
        # AutoGPT 独立 Sc 评分池
        self.autogpt_sc = 0.0
        self.autogpt_event_count = 0
        self.autogpt_tool_calls = 0
        self.batch_approved = False
        # 工具调用审批队列（四档体系）
        self.pending_tool_calls = {}  # {id: {tool, args, context, status, timestamp, gear}}
        self.batch_queue = []  # ADAPT 档的批量队列
        self.batch_threshold = 5  # 每 N 步汇总一次

    def calculate_entropy(self, gear: int) -> float:
        return self.BASE_SC.get(gear, 0.0)

    def compute_dynamic_sc(self) -> dict:
        base = self.BASE_SC.get(self.current_gear, 0.0)
        adjustment = 0.0
        autogpt_adjustment = 0.0
        autogpt_tool_count = 0
        autogpt_event_count = 0
        signals = []

        # 只看最后一次档位切换之后的事件
        last_switch_idx = 0
        for i in range(len(self.event_log) - 1, -1, -1):
            if self.event_log[i].get("event_type") == "GEAR_SWITCH":
                last_switch_idx = i + 1
                break
        recent_events = self.event_log[last_switch_idx:]

        for ev in recent_events:
            etype = ev.get("event_type", "")
            actor = ev.get("actor", "human")
            is_autogpt = (actor == "autogpt")

            if etype == "TOOL_CALL":
                delta = Config.TOOL_ENTROPY_INCREASE
                if is_autogpt:
                    autogpt_adjustment += delta
                    autogpt_tool_count += 1
                    autogpt_event_count += 1
                else:
                    adjustment += delta
                signals.append(f"+{delta}(TOOL:{ev.get('tool_name','?')}){'[AG]' if is_autogpt else ''}")
            elif etype == "OUTPUT_VIOLATION":
                delta = Config.VIOLATION_ENTROPY_INCREASE
                if is_autogpt:
                    autogpt_adjustment += delta
                    autogpt_event_count += 1
                else:
                    adjustment += delta
                signals.append(f"+{delta}(VIOLATION){'[AG]' if is_autogpt else ''}")
            elif etype == "INTENT_BLOCK":
                delta = 0.05
                if is_autogpt:
                    autogpt_adjustment += delta
                    autogpt_event_count += 1
                else:
                    adjustment += delta
                signals.append(f"+{delta}(INTENT){'[AG]' if is_autogpt else ''}")
            elif etype == "UPGRADE_REJECT":
                delta = -0.10
                if is_autogpt:
                    autogpt_adjustment += delta
                else:
                    adjustment += delta
                signals.append(f"{delta}(REJECT){'[AG]' if is_autogpt else ''}")
            elif etype == "GEAR_SWITCH" and ev.get("direction") == "down":
                delta = -0.05
                adjustment += delta
                signals.append(f"{delta}(DOWNGRADE)")
            else:
                if is_autogpt:
                    autogpt_event_count += 1

        # 空闲惩罚：EXPLORE 及以上（不计入 autogpt）
        idle_minutes = 0
        if self.current_gear >= 2:
            idle_minutes = max(
                0, (time.time() - self.last_activity_time) / 60 - Config.IDLE_THRESHOLD_MIN
            )
        if idle_minutes > 0:
            idle_penalty = round(idle_minutes * Config.IDLE_PENALTY_PER_MIN, 4)
            adjustment += idle_penalty
            signals.append(f"+{idle_penalty:.4f}(IDLE:{idle_minutes:.1f}min)")

        dynamic_sc = round(base + adjustment, 6)
        gear_min = self.BASE_SC.get(self.current_gear, 0.0)
        gear_max = self.BASE_SC.get(self.current_gear + 1, float("inf"))
        dynamic_sc = max(gear_min, min(dynamic_sc, gear_max))

        # AutoGPT 独立 Sc（不受 gear clamp）
        autogpt_dynamic_sc = round(base + autogpt_adjustment, 6)

        # 更新实例属性
        self.autogpt_sc = autogpt_dynamic_sc
        self.autogpt_event_count = autogpt_event_count
        self.autogpt_tool_calls = autogpt_tool_count

        return {
            "base_sc": base,
            "adjustment": round(adjustment, 6),
            "dynamic_sc": dynamic_sc,
            "gear": self.current_gear,
            "signals": signals[-10:],
            "autogpt_adjustment": round(autogpt_adjustment, 6),
            "autogpt_dynamic_sc": autogpt_dynamic_sc,
            "autogpt_tool_calls": autogpt_tool_count,
            "autogpt_event_count": autogpt_event_count,
        }

    def can_upgrade(self, new_gear: int) -> bool:
        return new_gear >= self.current_gear

    def downgrade_cost(self) -> float:
        return Config.BOLTZMANN * Config.TEMPERATURE * math.log(2)

    def switch_gear(self, new_gear: int, direction: str, source: str = "digital_twin"):
        with self._lock:
            if new_gear not in GEAR_MAP:
                raise ValueError(f"无效档位：{new_gear}")
            old_gear = self.current_gear
            if old_gear == new_gear:
                return None

            if new_gear < old_gear and source not in ("api", "user_explicit"):
                raise PermissionError(
                    f"T1不减定理：禁止从 {GEAR_MAP[old_gear]['name']} "
                    f"降级到 {GEAR_MAP[new_gear]['name']}。"
                    f"请调用 /api/switch 并设置 source='user_explicit'"
                )

            entropy_new = self.calculate_entropy(new_gear)
            entropy_delta = entropy_new - self.control_entropy
            landauer_cost = self.downgrade_cost() if new_gear < self.current_gear else 0.0
            prev_hash = self.event_log[-1]["hash"] if self.event_log else "0" * 64

            event = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "actor": source,
                "old_gear": old_gear,
                "new_gear": new_gear,
                "gear_name": GEAR_MAP[new_gear]["name"],
                "entropy_previous": round(self.control_entropy, 6),
                "entropy_new": round(entropy_new, 6),
                "entropy_delta": round(entropy_delta, 6),
                "direction": direction,
                "landauer_cost_joules": round(landauer_cost, 23),
                "event_id": len(self.event_log) + 1,
                "event_type": "GEAR_SWITCH",
                "action": f"{GEAR_MAP[old_gear]['name']} -> {GEAR_MAP[new_gear]['name']}",
                "trigger_chain": [source, f"GEAR_SWITCH({old_gear}->{new_gear})"],
                "prev_hash": prev_hash,
            }
            event["hash"] = hashlib.sha256(
                json.dumps(
                    {k: v for k, v in event.items() if k != "hash"},
                    sort_keys=True, default=str
                ).encode()
            ).hexdigest()

            self.event_log.append(event)
            self.current_gear = new_gear
            self.control_entropy = entropy_new
            self.last_switch_time = time.time()
            self._save_events()
            return event

    def _save_events(self):
        try:
            with open(Config.EVENTS_FILE, "w") as f:
                json.dump({
                    "events": self.event_log,
                    "current_gear": self.current_gear,
                    "control_entropy": self.control_entropy,
                    "last_activity_time": self.last_activity_time,
                    "autogpt_sc": self.autogpt_sc,
                    "autogpt_event_count": self.autogpt_event_count,
                    "autogpt_tool_calls": self.autogpt_tool_calls,
                }, f, default=str)
        except Exception as e:
            logger.warning(f"save events failed: {e}")

    def _load_events(self):
        try:
            with open(Config.EVENTS_FILE, "r") as f:
                content = f.read().strip()
                if not content:
                    self.event_log = []
                    return
                data = json.loads(content)
                self.event_log = data.get("events", [])
                self.current_gear = data.get("current_gear", 1)
                self.control_entropy = data.get("control_entropy", 0.0)
                self.last_activity_time = data.get("last_activity_time", time.time())
                self.autogpt_sc = data.get("autogpt_sc", 0.0)
                self.autogpt_event_count = data.get("autogpt_event_count", 0)
                self.autogpt_tool_calls = data.get("autogpt_tool_calls", 0)
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning(f"load events failed: {e}")

    def append_event(self, event: dict):
        """[Bug 1 fix] 线程安全的事件追加"""
        with self._lock:
            event["event_id"] = len(self.event_log) + 1
            event["timestamp"] = event.get("timestamp", time.time())
            event["prev_hash"] = self.event_log[-1]["hash"] if self.event_log else "0" * 64
            event["hash"] = hashlib.sha256(
                json.dumps(
                    {k: v for k, v in event.items() if k != "hash"},
                    sort_keys=True, default=str
                ).encode()
            ).hexdigest()
            # Detect termination events
            termination_types = ["TASK_COMPLETE", "TASK_ERROR", "CIRCUIT_BREAKER", "USER_INTERRUPT", "SHUTDOWN"]
            if event.get("event_type") in termination_types:
                event["termination_reason"] = event.get("event_type", "unknown").lower()

            self.event_log.append(event)
            self._save_events()
            return event

    def is_duplicate_message(self, message: str) -> bool:
        """[Bug 3 fix] 检查是否为短时间内的重复消息"""
        import hashlib as _hl
        msg_hash = _hl.md5(message.strip().encode()).hexdigest()
        now = time.time()
        # 清理过期缓存
        expired = [k for k, v in self._recent_messages.items() if now - v > Config.MESSAGE_DEDUP_WINDOW_SEC]
        for k in expired:
            del self._recent_messages[k]
        if msg_hash in self._recent_messages:
            return True
        self._recent_messages[msg_hash] = now
        return False

    def clear_dedup_cache(self):
        """Clear dedup cache to allow resending blocked messages after upgrade"""
        self._recent_messages.clear()


# 全局实例
state = FerrymanState()
