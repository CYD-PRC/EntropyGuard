# Entropy Runtime · 统一记忆架构设计

> 文档版本: 1.0.0
> 最后更新: 2026-05-31

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                     Hermes Agent                         │
│  ┌────────────────────────────────────────────────────┐  │
│  │         hermes_memory_bridge.py                     │  │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────────────┐  │  │
│  │  │ startup  │  │ runtime  │  │ shutdown         │  │  │
│  │  │ hook()   │  │ CRUD API │  │ hook(summary)    │  │  │
│  │  └────┬─────┘  └────┬─────┘  └───────┬──────────┘  │  │
│  └───────┼──────────────┼────────────────┼─────────────┘  │
└──────────┼──────────────┼────────────────┼────────────────┘
           │              │                │
     HTTP GET       HTTP GET/POST     HTTP POST
           │              │                │
           ▼              ▼                ▼
┌─────────────────────────────────────────────────────────┐
│              Entropy Runtime MessageBoard                │
│  ┌──────────┬──────────┬──────────┬──────────────────┐  │
│  │ persona  │   fact   │  skill   │    episode       │  │
│  │  (永续)  │  (中期)  │ （按需） │  (会话结束时)    │  │
│  └──────────┴──────────┴──────────┴──────────────────┘  │
│  ┌──────────────────────────────────────────────────┐   │
│  │  message (多Agent通信, request/response/ack...)  │   │
│  └──────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### 核心原则

1. **统一存储** — 所有记忆类型共用 MessageBoard 的持久化机制（events.json 同层）
2. **类型隔离** — 通过 `message_type` 字段区分记忆类型和多 Agent 通信
3. **生命周期感知** — 每种记忆类型有独立的过期策略和查询接口
4. **零侵入** — 多 Agent 通信（message 类型）完全不受影响

---

## 2. 记忆类型定义

| 记忆类型 | 生命周期 | 示例 | TTL | 权限 |
|---------|---------|------|-----|------|
| `persona` | 永续（按年） | "用户偏好简洁回复" | 1年 | 仅 Hermes 写入 |
| `fact` | 中期（按月） | "项目路径 /root/EntropyGuard" | 1年 | 系统+Agent 写入 |
| `skill` | 按需 | "部署五步法" | 1年 | Agent 写入 |
| `episode` | 会话级（按天） | "本次会话完成漏洞修复" | 1年 | Hermes shutdown 写入 |
| `message` | 通信级（秒/分） | 多 Agent 间的请求/响应 | 5分钟 | 所有 Agent |

### 数据格式

每条记忆在 MessageBoard 中以标准 Message 格式存储：

```json
{
  "id": "uuid...",
  "from_agent": "hermes",
  "to_agent": "__memory__",
  "type": "fact",
  "content": {
    "text": "项目路径 /root/EntropyGuard",
    "tags": ["env", "path"],
    "_type": "fact",
    "_source": "hermes",
    "_created": "2026-05-31T13:00:00Z"
  },
  "priority": 0,
  "ttl": 31536000,
  "timestamp": ...,
  "status": "pending",
  "signature": "sha256..."
}
```

---

## 3. API 端点

所有记忆 API 位于 `/api/messageboard/` 前缀下（通过 `messageboard_api.py` 注册）。

### 3.1 写入记忆

```
POST /api/messageboard/memory
```

```json
{
  "memory_type": "fact",
  "content": {"text": "项目路径 /root/EntropyGuard", "key": "project_path"},
  "source": "hermes",
  "ttl": 0
}
```

响应: `{"success": true, "memory_id": "uuid...", "type": "fact"}`

### 3.2 查询记忆

```
GET /api/messageboard/memory/{type}?limit=10&source=hermes
```

响应:
```json
{
  "success": true,
  "type": "fact",
  "count": 2,
  "memories": [
    { "id": "...", "content": {"text": "..."}, "timestamp": ... }
  ]
}
```

### 3.3 获取 system prompt 上下文

```
GET /api/messageboard/memory/context
```

响应:
```json
{
  "success": true,
  "context": {
    "persona": ["用户偏好简洁的回复", "..."],
    "fact": ["项目路径 /root/EntropyGuard", "..."],
    "skill": ["安全审计三步法..."]
  },
  "has_persona": true,
  "has_facts": true,
  "has_skills": true
}
```

---

## 4. Hermes Memory Bridge 接口

文件: `integrations/hermes_memory_bridge.py`

### 4.1 初始化

```python
from integrations.hermes_memory_bridge import HermesMemoryBridge

bridge = HermesMemoryBridge(
    base_url="http://127.0.0.1:8000",
    api_key="your-api-key",
    source="hermes",
)
```

### 4.2 读取

| 方法 | 返回 | 说明 |
|------|------|------|
| `get_context()` | `{"persona":[],"fact":[],"skill":[]}` | 聚合所有记忆类型 |
| `get_persona()` | `List[str]` | 用户画像 |
| `get_facts()` | `List[str]` | 环境事实 |
| `get_skills()` | `List[str]` | 程序性知识 |
| `get_by_type(type, limit)` | `List[dict]` | 原始记忆数据 |
| `build_system_context()` | `str` | 格式化 system prompt 文本 |

### 4.3 写入

| 方法 | 说明 |
|------|------|
| `save_persona(text, **metadata)` | 写入用户画像 |
| `save_fact(text, **metadata)` | 写入环境事实 |
| `save_skill(text, **metadata)` | 写入技能/工作流 |
| `save_episode(text, **metadata)` | 写入会话情节摘要 |

### 4.4 Hermes 集成钩子

```python
# Hermes 启动时
startup_context = hermes_startup_hook()
# 返回格式化文本: "[MEMORY: PERSONA]\n- 用户偏好...\n\n[MEMORY: FACTS]\n- ..."

# Hermes 会话结束时
hermes_shutdown_hook("本次会话完成了安全审计...", session_id="xxx")
```

---

## 5. 消息流详解

### 5.1 Hermes 启动 → 加载记忆

```
[1] Hermes Agent 启动
  → 调用 hermes_startup_hook()
    → GET /api/messageboard/memory/context
      → MessageBoard.get_by_type("persona")
      → MessageBoard.get_by_type("fact")
      → MessageBoard.get_by_type("skill")
    → 格式化为 "[MEMORY: PERSONA]\n- ...\n[MEMORY: FACTS]\n- ..."
  → 注入到 system prompt 末尾
```

### 5.2 运行时写入事实

```
[2] 用户告诉 Hermes "项目路径是 /root/EntropyGuard"
  → Hermes 调用 bridge.save_fact("项目路径 /root/EntropyGuard", key="project_path")
    → POST /api/messageboard/memory
      → MessageBoard.send_memory("fact", ...)
      → _save() → messageboard_data.json
```

### 5.3 会话结束 → 保存情节

```
[3] Hermes 会话正常结束
  → 调用 hermes_shutdown_hook("本次会话...")
    → POST /api/messageboard/memory
    → type=episode, source=hermes
    → 保存到永久存储
```

### 5.4 多 Agent 通信（无回归）

```
[4] Agent A → Agent B 的消息
  → POST /api/messageboard/send
    → to_agent="agent-b", type="request"
    → MessageBoard.send() → _inbox["agent-b"].append(id)
  → Agent B → GET /api/messageboard/inbox/agent-b
    → 返回 PENDING 消息列表

记忆类型写入的是 to_agent="__memory__"，不会进入任何 Agent 的收件箱。
多 Agent 通信的收件箱查询不受影响。
```

---

## 6. 存储结构

文件名: `messageboard_data.json`（由 `main.py` startup 时初始化）

```json
{
  "timestamp": "2026-05-31T13:00:00Z",
  "messages": {
    "uuid-1": { "id": "uuid-1", "type": "fact", "from_agent": "hermes", "to_agent": "__memory__", ... },
    "uuid-2": { "id": "uuid-2", "type": "request", "from_agent": "agent-a", "to_agent": "agent-b", ... },
    "uuid-3": { "id": "uuid-3", "type": "persona", "from_agent": "hermes", "to_agent": "__memory__", ... }
  },
  "inbox": {
    "agent-b": ["uuid-2"],
    "__memory__": ["uuid-1", "uuid-3"]
  }
}
```

`__memory__` 收件箱仅由桥接接口查询，不会影响多 Agent 通信的正常收件箱。

---

## 7. 与现有记忆系统的关系

```
┌──────────────────────────┐     ┌──────────────────────────┐
│     Hermes 记忆系统       │     │  Entropy Runtime 记忆     │
│  (内存中的会话级记忆)      │     │  (MessageBoard 持久化)    │
│                          │     │                          │
│  memory → 个人笔记        │◄───►│  persona → 用户画像       │
│  user → 用户配置          │     │  fact → 环境事实          │
│                          │     │  skill → 程序性知识       │
│                          │     │  episode → 会话情节       │
│                          │     │  message → Agent通信       │
│                          │     │  (events.json → 审计链)    │
└──────────────────────────┘     └──────────────────────────┘
```

- **Hermes 记忆** — 会话内部短记忆，驱动当前对话行为
- **MessageBoard 记忆** — 跨会话长记忆，供 Hermes 下次启动时加载
- **审计链 (events.json)** — 事件驱动审计，与记忆系统正交

---

## 8. 安全考量

| 风险 | 缓解措施 |
|------|---------|
| 记忆数据泄露 | API Token 认证（复用 ENTROPY_RUNTIME_API_KEY） |
| 记忆污染（错误写入） | `source` 字段溯源，每条记录签名 |
| 记忆膨胀 | 按类型的 TTL 自动过期清理 |
| 记忆注入（恶意 system prompt） | 桥接仅注入 `build_system_context()` 的输出，格式严格限定 |
| 多 Agent 通信受干扰 | 记忆写入 `to_agent="__memory__"`，与 Agent 收件箱隔离 |
