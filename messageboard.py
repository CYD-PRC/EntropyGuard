"""
Enhanced MessageBoard for Multi-Agent Communication

Provides async messaging system for Entropy Runtime and Hermes Agent collaboration:
- Priority-based message queuing
- TTL (time-to-live) with automatic expiration
- Message signing and verification (SHA-256)
- ACK (acknowledgment) mechanism
- Message persistence and recovery
- Session-scoped message groups

Usage:
    board = MessageBoard()
    
    # Send a request from Hermes to Entropy Runtime
    msg_id = board.send(
        from_agent="hermes-agent-1",
        to_agent="entropyruntime-verifier",
        message_type="request",
        content={"action": "verify_command", "command": "ls -la"},
        priority=8,
        ttl=30
    )
    
    # Entropy Runtime checks inbox
    messages = board.get_inbox("entropyruntime-verifier")
    
    # Process and reply
    for msg in messages:
        result = process_message(msg)
        board.send_reply(msg["id"], result, priority=9)
        board.ack(msg["id"], "entropyruntime-verifier")
"""

import json
import time
import hashlib
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta
from enum import Enum
import threading


class MessageType(Enum):
    """Standard message types for multi-agent communication."""
    REQUEST = "request"           # One agent asking another to do something
    RESPONSE = "response"         # Reply to a request
    NOTIFICATION = "notification"  # Async notification
    ACK = "ack"                   # Acknowledgment
    NEGOTIATE = "negotiate"        # Dispute/conflict resolution
    HEARTBEAT = "heartbeat"       # Keep-alive signal
    # Hermes 记忆类型（MessageType 兼容层，确保 send_memory 通过验证）
    PERSONA = "persona"
    FACT = "fact"
    SKILL = "skill"
    EPISODE = "episode"


# ========== 记忆系统扩展：持久化记忆类型 ==========

class MemoryType(Enum):
    """
    Hermes 记忆系统接入 MessageBoard 的标准化记忆类型。

    每种类型对应 Hermes 记忆系统的一个生命周期阶段：
    - PERSONA: 用户身份、偏好、沟通风格（长期不变）
    - FACT:    环境事实、项目结构、工具特性（中期稳定）
    - SKILL:   可复用的工作流/程序性知识（按需更新）
    - EPISODE: 会话情节记录（每次会话结束时写入）
    """
    PERSONA = "persona"    # 用户画像：身份、偏好、习惯
    FACT = "fact"          # 环境事实：路径、配置、依赖
    SKILL = "skill"        # 程序性知识：工作流、技巧
    EPISODE = "episode"    # 情节记忆：会话摘要
    MESSAGE = "message"    # 普通通信（兼容 MessageType）


class MessagePriority(Enum):
    """Message priority levels (0-10 scale)."""
    LOWEST = 0
    LOW = 2
    NORMAL = 5
    HIGH = 7
    CRITICAL = 10


class MessageStatus(Enum):
    """Message lifecycle status."""
    PENDING = "pending"           # Waiting for recipient to receive
    RECEIVED = "received"         # Recipient has retrieved it
    ACKNOWLEDGED = "acknowledged"  # Recipient has processed it
    EXPIRED = "expired"           # TTL exceeded
    REJECTED = "rejected"         # Recipient refused to process
    COMPLETED = "completed"       # Operation finished


class Message:
    """Single message with metadata and integrity."""
    
    def __init__(
        self,
        from_agent: str,
        to_agent: str,
        message_type: MessageType,
        content: Dict[str, Any],
        priority: int = 5,
        ttl: int = 300,
        reply_to: Optional[str] = None,
        msg_id: Optional[str] = None,
        timestamp: Optional[float] = None,
    ):
        self.id = msg_id or str(uuid.uuid4())
        self.from_agent = from_agent
        self.to_agent = to_agent
        self.message_type = message_type if isinstance(message_type, MessageType) else MessageType(message_type)
        self.content = content
        self.priority = max(0, min(priority, 10))  # Clamp 0-10
        self.ttl = max(1, ttl)  # At least 1 second
        self.reply_to = reply_to
        self.timestamp = timestamp or time.time()
        self.expires_at = self.timestamp + self.ttl
        self.status = MessageStatus.PENDING
        self.signature = ""
        self.ack_by: Dict[str, float] = {}  # agent_name -> ack_time
        
    def sign(self, secret: str = "") -> str:
        """Generate SHA-256 signature for integrity verification."""
        payload = {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "type": self.message_type.value,
            "content": self.content,
            "priority": self.priority,
            "ttl": self.ttl,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
        }
        msg_str = json.dumps(payload, sort_keys=True, default=str)
        if secret:
            msg_str += secret
        self.signature = hashlib.sha256(msg_str.encode()).hexdigest()
        return self.signature
    
    def verify(self, signature: str, secret: str = "") -> bool:
        """Verify message signature."""
        return self.sign(secret) == signature
    
    def is_expired(self) -> bool:
        """Check if message TTL exceeded."""
        return time.time() > self.expires_at
    
    def to_dict(self) -> Dict[str, Any]:
        """Serialize message to dict."""
        return {
            "id": self.id,
            "from_agent": self.from_agent,
            "to_agent": self.to_agent,
            "type": self.message_type.value,
            "content": self.content,
            "priority": self.priority,
            "ttl": self.ttl,
            "reply_to": self.reply_to,
            "timestamp": self.timestamp,
            "expires_at": self.expires_at,
            "status": self.status.value,
            "signature": self.signature,
            "acks": self.ack_by,
        }
    
    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Message":
        """Deserialize message from dict."""
        msg = cls(
            from_agent=data["from_agent"],
            to_agent=data["to_agent"],
            message_type=data["type"],
            content=data["content"],
            priority=data.get("priority", 5),
            ttl=data.get("ttl", 300),
            reply_to=data.get("reply_to"),
            msg_id=data["id"],
            timestamp=data.get("timestamp"),
        )
        msg.status = MessageStatus(data.get("status", MessageStatus.PENDING.value))
        msg.signature = data.get("signature", "")
        msg.ack_by = data.get("acks", {})
        return msg


class MessageBoard:
    """Central message hub for multi-agent communication."""
    
    def __init__(self, storage_path: Optional[str] = None):
        """
        Initialize MessageBoard.
        
        Args:
            storage_path: Path to persist messages (JSON file).
                         If None, messages are in-memory only.
        """
        self.storage_path = Path(storage_path) if storage_path else None
        self.messages: Dict[str, Message] = {}
        self._inbox: Dict[str, List[str]] = {}  # agent -> [msg_ids]
        self._lock = threading.RLock()
        self._cleanup_interval = 60  # Clean expired messages every 60s
        self._last_cleanup = time.time()
        self._secret = ""  # Can be set for HMAC signing
        
        self._load()
    
    def set_secret(self, secret: str) -> None:
        """Set HMAC secret for message signing."""
        self._secret = secret
    
    def send(
        self,
        from_agent: str,
        to_agent: str,
        message_type: str,
        content: Dict[str, Any],
        priority: int = 5,
        ttl: int = 300,
        reply_to: Optional[str] = None,
    ) -> str:
        """
        Send a message to another agent.
        
        Args:
            from_agent: Sender agent name
            to_agent: Recipient agent name
            message_type: Message type (e.g., 'request', 'response')
            content: Message content/payload
            priority: Priority level (0-10)
            ttl: Time-to-live in seconds
            reply_to: Optional ID of message being replied to
        
        Returns:
            Message ID
        """
        with self._lock:
            msg = Message(
                from_agent=from_agent,
                to_agent=to_agent,
                message_type=message_type,
                content=content,
                priority=priority,
                ttl=ttl,
                reply_to=reply_to,
            )
            msg.sign(self._secret)
            self.messages[msg.id] = msg
            
            # Add to recipient's inbox
            if to_agent not in self._inbox:
                self._inbox[to_agent] = []
            self._inbox[to_agent].append(msg.id)
            
            # Sort by priority (highest first)
            self._inbox[to_agent].sort(
                key=lambda mid: self.messages[mid].priority,
                reverse=True
            )
            
            self._save()
            return msg.id
    
    def send_reply(
        self,
        reply_to_msg_id: str,
        content: Dict[str, Any],
        from_agent: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> str:
        """
        Send a reply to an existing message.
        
        Args:
            reply_to_msg_id: ID of message being replied to
            content: Reply content
            from_agent: Sender (if None, swaps roles from original)
            priority: Reply priority (if None, uses original +1)
        
        Returns:
            Reply message ID
        """
        with self._lock:
            if reply_to_msg_id not in self.messages:
                raise ValueError(f"Message {reply_to_msg_id} not found")
            
            original = self.messages[reply_to_msg_id]
            to_agent = original.from_agent
            from_agent = from_agent or original.to_agent
            priority = priority if priority is not None else min(original.priority + 1, 10)
            
            return self.send(
                from_agent=from_agent,
                to_agent=to_agent,
                message_type="response",
                content=content,
                priority=priority,
                ttl=original.ttl,
                reply_to=reply_to_msg_id,
            )
    
    def get_inbox(self, agent_name: str, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        """
        Get all pending messages for an agent.
        
        Args:
            agent_name: Agent name
            limit: Max messages to return (None = all)
        
        Returns:
            List of messages (sorted by priority, highest first)
        """
        with self._lock:
            self._cleanup()
            msg_ids = self._inbox.get(agent_name, [])
            
            # Filter out expired and already-received messages
            active_msgs = []
            for mid in msg_ids:
                if mid in self.messages:
                    msg = self.messages[mid]
                    if not msg.is_expired() and msg.status == MessageStatus.PENDING:
                        active_msgs.append(msg.to_dict())
            
            return active_msgs[:limit] if limit else active_msgs
    
    def get_message(self, msg_id: str) -> Optional[Dict[str, Any]]:
        """Get a specific message by ID."""
        with self._lock:
            if msg_id in self.messages:
                msg = self.messages[msg_id]
                if not msg.is_expired():
                    return msg.to_dict()
            return None
    
    def mark_received(self, msg_id: str, agent_name: str) -> bool:
        """
        Mark a message as received (first read).
        
        Args:
            msg_id: Message ID
            agent_name: Agent marking as received
        
        Returns:
            Success status
        """
        with self._lock:
            if msg_id not in self.messages:
                return False
            msg = self.messages[msg_id]
            if msg.to_agent == agent_name and msg.status == MessageStatus.PENDING:
                msg.status = MessageStatus.RECEIVED
                self._save()
                return True
            return False
    
    def ack(self, msg_id: str, agent_name: str) -> bool:
        """
        Acknowledge message processing.
        
        Args:
            msg_id: Message ID
            agent_name: Agent acknowledging
        
        Returns:
            Success status
        """
        with self._lock:
            if msg_id not in self.messages:
                return False
            msg = self.messages[msg_id]
            if msg.to_agent == agent_name:
                msg.ack_by[agent_name] = time.time()
                msg.status = MessageStatus.ACKNOWLEDGED
                self._save()
                return True
            return False
    
    def reject(self, msg_id: str, agent_name: str, reason: str = "") -> bool:
        """
        Reject a message.
        
        Args:
            msg_id: Message ID
            agent_name: Agent rejecting
            reason: Rejection reason
        
        Returns:
            Success status
        """
        with self._lock:
            if msg_id not in self.messages:
                return False
            msg = self.messages[msg_id]
            if msg.to_agent == agent_name:
                msg.status = MessageStatus.REJECTED
                msg.content["rejection_reason"] = reason
                self._save()
                return True
            return False
    
    def get_conversation(self, root_msg_id: str) -> List[Dict[str, Any]]:
        """
        Get full message thread (root + all replies).
        
        Args:
            root_msg_id: Root message ID
        
        Returns:
            List of messages in conversation order
        """
        with self._lock:
            conversation = []
            to_visit = [root_msg_id]
            visited = set()
            
            while to_visit:
                mid = to_visit.pop(0)
                if mid in visited or mid not in self.messages:
                    continue
                visited.add(mid)
                
                msg = self.messages[mid]
                conversation.append(msg.to_dict())
                
                # Find replies to this message
                for other_id, other_msg in self.messages.items():
                    if other_msg.reply_to == mid and other_id not in visited:
                        to_visit.append(other_id)
            
            return conversation
    
    def get_stats(self, agent_name: Optional[str] = None) -> Dict[str, Any]:
        """
        Get statistics about messages.

        Args:
            agent_name: If specified, return stats for this agent only

        Returns:
            Stats dict
        """
        with self._lock:
            self._cleanup()

            if agent_name:
                inbox_size = len([m for m in self.messages.values() if m.to_agent == agent_name])
                pending = len([m for m in self.messages.values()
                              if m.to_agent == agent_name and m.status == MessageStatus.PENDING])
                return {
                    "agent": agent_name,
                    "inbox_size": inbox_size,
                    "pending_messages": pending,
                    "total_agents_communicating": len(set(m.from_agent for m in self.messages.values() if m.to_agent == agent_name)),
                }
            else:
                return {
                    "total_messages": len(self.messages),
                    "expired": len([m for m in self.messages.values() if m.is_expired()]),
                    "pending": len([m for m in self.messages.values() if m.status == MessageStatus.PENDING]),
                    "acknowledged": len([m for m in self.messages.values() if m.status == MessageStatus.ACKNOWLEDGED]),
                    "by_type": {mt.value: len([m for m in self.messages.values() if m.message_type == mt])
                               for mt in MessageType},
                    "agents_communicating": len(set(m.from_agent for m in self.messages.values())),
                }

    # ========== 记忆系统接口 ==========

    def send_memory(
        self,
        memory_type: str,
        content: Dict[str, Any],
        source: str = "hermes",
        ttl: int = 0,
    ) -> str:
        """
        写入一条记忆到 MessageBoard。

        Args:
            memory_type: 记忆类型 — persona / fact / skill / episode
            content: 记忆内容（dict，可含 text/ tags/ metadata 等字段）
            source: 来源标识（如 hermes, entropyruntime 等）
            ttl: 过期秒数（0 = 永不过期，默认 0）

        Returns:
            记忆消息 ID
        """
        # ttl=0 表示不设置过期（内部用 100 年替代）
        actual_ttl = ttl if ttl > 0 else 31536000  # 1 year
        content.setdefault("_type", memory_type)
        content.setdefault("_source", source)
        content.setdefault("_created", datetime.utcnow().isoformat() + "Z")
        return self.send(
            from_agent=source,
            to_agent="__memory__",
            message_type=memory_type,
            content=content,
            priority=0,
            ttl=actual_ttl,
        )

    def get_by_type(
        self,
        memory_type: str,
        limit: Optional[int] = None,
        source: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        按记忆类型查询记忆。

        Args:
            memory_type: 记忆类型 — persona / fact / skill / episode
            limit: 最大返回条数
            source: 可选来源过滤

        Returns:
            匹配的记忆消息列表（按时间倒序）
        """
        with self._lock:
            results = []
            for msg in self.messages.values():
                if msg.is_expired():
                    continue
                if msg.message_type.value != memory_type and msg.message_type.value != memory_type:
                    continue
                if source and msg.from_agent != source:
                    continue
                results.append(msg.to_dict())

        # 按时间倒序排列
        results.sort(key=lambda m: m.get("timestamp", 0), reverse=True)
        return results[:limit] if limit else results
    
    def _cleanup(self) -> None:
        """Remove expired messages periodically."""
        now = time.time()
        if now - self._last_cleanup < self._cleanup_interval:
            return
        
        expired_ids = [
            mid for mid, msg in self.messages.items()
            if msg.is_expired()
        ]
        for mid in expired_ids:
            msg = self.messages[mid]
            msg.status = MessageStatus.EXPIRED
            # Don't delete, just mark as expired for audit trail
        
        self._last_cleanup = now
        self._save()
    
    def _save(self) -> None:
        """Persist messages to storage."""
        if not self.storage_path:
            return
        
        try:
            self.storage_path.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "timestamp": datetime.utcnow().isoformat() + "Z",
                "messages": {mid: msg.to_dict() for mid, msg in self.messages.items()},
                "inbox": self._inbox,
            }
            with open(self.storage_path, "w") as f:
                json.dump(data, f, indent=2, default=str)
        except Exception as e:
            print(f"[ERROR] Failed to save MessageBoard: {e}")
    
    def _load(self) -> None:
        """Load messages from storage."""
        if not self.storage_path or not self.storage_path.exists():
            return
        
        try:
            with open(self.storage_path, "r") as f:
                data = json.load(f)
            
            for mid, msg_dict in data.get("messages", {}).items():
                msg = Message.from_dict(msg_dict)
                self.messages[mid] = msg
            
            self._inbox = data.get("inbox", {})
        except Exception as e:
            print(f"[ERROR] Failed to load MessageBoard: {e}")


# Global singleton instance
_global_messageboard: Optional[MessageBoard] = None


def get_messageboard(storage_path: Optional[str] = None) -> MessageBoard:
    """Get or create global MessageBoard instance."""
    global _global_messageboard
    if _global_messageboard is None:
        _global_messageboard = MessageBoard(storage_path)
    return _global_messageboard
