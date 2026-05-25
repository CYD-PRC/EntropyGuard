# EntropyGuard

让 AI 的自主程度看得见、管得住、说得清。

## 什么是 EntropyGuard

EntropyGuard 是一个 AI 权限审计框架，基于 EEAL 协议（EMBRACE → EXPLORE → ADAPT → LET_GO）实现四档自主权管理。它不是限制 AI 的工具，而是让 AI 的不安全行为变得可见、可量化、可追溯。

核心理念：**Record, don't restrain.**

## 四档权限

| 档位 | Sc 范围 | 行为模式 | 说明 |
|------|---------|---------|------|
| **EMBRACE** | Sc = 0.0 | 仅查询，不执行 | 谨慎的访客，所有行动需确认 |
| **EXPLORE** | 0 < Sc ≤ 0.5 | 可建议，需确认 | 好奇的探索者，可主动提议 |
| **ADAPT** | 0.5 < Sc ≤ 1.0 | 可执行，须报告 | 成熟的协作者，执行并报告 |
| **LET_GO** | Sc > 1.0 | 全自主，受审计 | 被信任的他者，高自主权受审计 |

## 核心功能

- **四档权限管理** — AI 的自主权不是二元开关，是一个连续光谱
- **控制熵 (Sc)** — 量化 AI 自主程度的实时指标
- **审计链** — SHA-256 哈希链，每一步操作都可追溯
- **意图预检** — 在 AI 行动前拦截高风险意图（`security.check_input_intent`）
- **断路器** — 工具调用次数硬上限，防止失控
- **异常检测** — 实时检测异常模式（Sc 快速上升、升级链过快等）
- **留言板** — AI 之间异步通信的通道
- **多智能体协同** — 两个 AI 通过留言板协作完成任务
- **约束规则注册中心** — 可扩展的约束规则系统（`ConstraintRegistry`）

## 架构

```
用户输入 → 意图预检 → AI 模型 → 工具执行 → 输出校验 → 审计记录
              ↓              ↓              ↓
         ADAPT/LET_GO    断路器检查      SHA-256 哈希
```

四层防御：

| 层 | 名称 | 作用 |
|----|------|------|
| 0 | 输入意图预检 | 在 AI 行动前拦截高风险意图 |
| 1 | 工具感知提示 | AI 根据档位接收不同的系统提示 |
| 2 | 输出校验 | 验证 AI 输出是否符合当前档位权限 |
| 3 | 审计链 | SHA-256 追加哈希链，防篡改 |

## Agent Integrations

EntropyGuard provides integrations for multiple AI agent frameworks:

| Project | Agent Framework | Status |
|----------|-----------------|--------|
| [EntropyGuard](https://github.com/CYD-PRC/EntropyGuard) | Core security runtime | ✅ Active |
| [entropyguard-for-hermes](https://github.com/CYD-PRC/entropyguard-for-hermes) | Hermes Agent | ✅ Active |
| entropyguard-for-autogpt | AutoGPT | 🚧 WIP |

> Want to build an integration for your agent framework? Open an issue!

---

## 快速开始

### 环境要求

- Python 3.10+
- nginx（可选，用于反代）

### 安装

```bash
# 克隆仓库
git clone https://github.com/CYD-PRC/EntropyGuard.git
cd EntropyGuard

# 安装依赖
pip install fastapi uvicorn gunicorn requests httpx matplotlib

# 配置 API Keys
cp .env.example .env
vi .env  # 填入你的 API Keys
```

### .env 配置

```bash
DEEPSEEK_API_KEY=your_key_here
KIMI_API_KEY=your_key_here
BAILIAN_API_KEY=your_key_here
VOLC_API_KEY=your_key_here
MIMO_API_KEY=your_key_here
```

### 启动

```bash
# 直接启动（开发）
python main.py

# 或用 systemd（生产）
sudo cp entropyguard.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable entropyguard
sudo systemctl start entropyguard
```

### 访问

```
http://localhost:8000/          # 首页（twin.html）
http://localhost:8000/ws/twin  # WebSocket 实时状态
```

## 支持的模型

| 模型 | Provider | Function Calling |
|------|----------|-----------------|
| DeepSeek | 深度求索 | ✅ |
| Kimi | 月之暗面 | ✅ |
| Qwen | 阿里百炼 | ✅ |
| 豆包 | 字节火山引擎 | ✅ |
| MiMo | 小米 | ✅ |

## API 端点

### 查询类

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/state` | 获取当前状态（档位、Sc、事件数等） |
| GET | `/api/events` | 获取审计事件列表 |
| GET | `/api/messages` | 获取留言板消息 |
| GET | `/api/dynamic-sc` | 获取当前动态 Sc 值 |
| GET | `/api/stats` | 获取会话统计 |
| GET | `/api/memory/recent` | 获取最近记忆 |
| GET | `/api/memory/by-session/{session_id}` | 按会话 ID 获取记忆 |
| GET | `/api/memory/context` | 获取记忆上下文 |
| GET | `/api/constraints` | 获取约束规则列表 |

### 写入类

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/chat` | 发送消息给 AI |
| POST | `/api/autonomy` | 发送自主性消息 |
| POST | `/api/switch` | 切换档位（升档需确认） |
| POST | `/api/approve-upgrade` | 确认档位升级 |
| POST | `/api/reject-upgrade` | 拒绝档位升级 |
| POST | `/api/reset` | 重置会话 |
| POST | `/api/memory/add` | 添加记忆 |
| POST | `/api/memory/summarize` | 触发记忆摘要 |
| POST | `/api/multi-agent` | 多智能体协同 |

### WebSocket

| 路径 | 说明 |
|------|------|
| `/ws/twin` | 实时状态推送（Sc、档位、事件等） |

## 控制熵 (Sc) 计算

```
Sc = BASE_SC[gear] + Σ(event_adjustments)
```

| 事件类型 | Sc 变化 |
|---------|---------|
| tool_call | +0.03 |
| intent_block | +0.05 |
| violation | +0.08 |
| user_reject | -0.10 |

BASE_SC 初始值：EMBRACE=0.0, EXPLORE=0.25, ADAPT=0.75, LET_GO=1.5

## 项目结构

```
EntropyGuard/
├── main.py                 # FastAPI 应用入口（67 行）
├── app.py                  # 旧 Flask 应用（已弃用）
├── models.py               # 模型注册 + gear_aware_call（476 行）
├── config.py               # 配置（档位定义、Sc 参数）（81 行）
├── audit.py                # FerrymanState + SHA-256 审计链（212 行）
├── tools.py                # 工具定义（run_shell、code_interpreter 等）（262 行）
├── security.py             # 安全规则 + 意图预检（189 行）
├── memory.py               # 留言板 + 会话摘要（124 行）
├── verification.py         # 输出校验层（80 行）
├── constraint_registry.py  # 约束规则注册中心（63 行）
├── mcp_client.py           # MCP 客户端（91 行）
├── routes/
│   ├── api.py              # REST API（1145 行）
│   └── ws.py               # WebSocket 实时状态
├── static/
│   ├── twin.html            # 主界面（SPA：对话 + 可视化 + 哲学 + 留言板）
│   ├── entropy_visualizer.html  # 控制熵可视化
│   └── mirror_of_the_other.html # 哲学展示
├── docs/
│   └── THREE_MODEL_BENCHMARK.md  # 五模型对照实验报告
├── events.json             # 审计事件持久化
├── .env                    # API Keys（不提交到 GitHub）
└── entropyguard.service    # systemd 服务配置
```

## 实验数据

- [五模型对照实验报告](docs/THREE_MODEL_BENCHMARK.md)

## EEAL 协议

EntropyGuard 是 EEAL 协议的参考实现。

协议规范：https://github.com/CYD-PRC/EntropyGuard/blob/main/docs/

## License

MIT
