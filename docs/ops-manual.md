# Entropy Runtime · 运维手册

> 版本: 3.3.0 | 最后更新: 2026-06-01
> 部署位置: 阿里云 ECS | IP: 8.153.99.156 | SSH 端口: 2222
> 运行时: Python 3.10 | DeepSeek V4 Flash | PydanticAI + Hermes 混合引擎

---

## 1. 服务管理

### 1.1 启动/停止/重启

```bash
# 启动
systemctl start entropyguard

# 停止
systemctl stop entropyguard

# 重启
systemctl restart entropyguard

# 查看状态
systemctl status entropyguard

# 查看日志（实时）
journalctl -u entropyguard -f

# 查看最近日志
journalctl -u entropyguard -n 100 --no-pager
```

### 1.2 服务自恢复

服务由 systemd 管理，已在 `/etc/systemd/system/entropyguard.service` 配置：

- `Restart=always` — 崩溃后自动重启
- `RestartSec=5` — 5 秒后重试
- `MemoryMax=1G` — 内存上限 1GB
- `CPUQuota=200%` — CPU 上限 2 核

**服务器重启后，服务会自动启动**（`WantedBy=multi-user.target`）。

### 1.3 手动启动（调试用）

```bash
cd /root/EntropyGuard
source venv310/bin/activate
python -m gunicorn main:app -w 1 -k uvicorn.workers.UvicornWorker -b 0.0.0.0:8000 --timeout 120
```

---

## 2. 环境变量

### 2.1 配置文件

路径: `/root/.env`

```
ENTROPY_RUNTIME_API_KEY=***   ← API 认证密钥（最重要）
DEEPSEEK_API_KEY=***        ← DeepSeek 模型调用密钥（v3.3 已恢复正常）
OPENAI_API_KEY=***          ← 兼容 DeepSeek 的 OpenAI 格式密钥
KIMI_API_KEY=***            ← Kimi 模型密钥
BAILIAN_API_KEY=***         ← 百炼 Qwen 模型密钥
```

### 2.2 API 认证机制

所有 API 端点受 `ENTROPY_RUNTIME_API_KEY` 保护：

```bash
# Bearer Token
curl -H "Authorization: Bearer *** http://127.0.0.1:8000/api/state

# X-API-Key（备选）
curl -H "X-API-Key: <token>" http://127.0.0.1:8000/api/state
```

认证流程：
```
请求 → verify_api_token() → 检查 Authorization/X-API-Key
  → 无 Token 且 ENV 未配置 → 放行（仅开发模式）
  → Token 不匹配 → 401 Unauthorized
  → Token 匹配 → 放行到路由
```

### 2.3 修改 API 密钥

```bash
# 1. 修改 /root/.env
sed -i 's/ENTROPY_RUNTIME_API_KEY=.*/ENTROPY_RUNTIME_API_KEY=<新密钥>/' /root/.env

# 2. 重启服务使生效
systemctl restart entropyguard
```

---

## 3. 启动恢复机制（双重保障）

系统启动时，`main.py` 通过两重机制加载环境变量：

```
1. systemd EnvironmentFile=/root/.env      ← 服务管理层面
2. main.py 中 python-dotenv load_dotenv()  ← 代码层面（防御性）
```

确认加载成功：

```bash
# 查看启动日志
journalctl -u entropyguard -n 5 | grep -i 'API_KEY\|env'
```

如果 API 密钥加载失败，所有端点将返回 401。

---

## 4. 端到端验证

### 4.1 冒烟测试

```bash
python3 /tmp/e2e_smoke2.py
```

### 4.2 健康检查

```bash
# API 健康端点（自动调用 healthcheck 脚本）
source /root/.env && \
KEY=$(grep ENTROPY_RUNTIME_API_KEY /root/.env | cut -d= -f2-) && \
curl -s -H "Authorization: Bearer *** http://127.0.0.1:8000/api/health
```

预期返回 JSON：
```json
{"status":"ok","service":true,"events_json_bytes":1081704,"version":"v3.3"}
```

### 4.3 状态查询

```bash
source /root/.env && \
KEY=$(grep ENTROPY_RUNTIME_API_KEY /root/.env | cut -d= -f2-) && \
curl -s -H "Authorization: Bearer *** http://127.0.0.1:8000/api/state
```

### 4.4 故障排查速查

| 症状 | 排查步骤 |
|------|---------|
| 服务未运行 | `systemctl status entropyguard` → 检查日志 |
| 401 拒绝 | 检查 `/root/.env` 中的 `ENTROPY_RUNTIME_API_KEY` |
| 500 错误 | `journalctl -u entropyguard -n 50` 查看 Python 堆栈 |
| 审计链为空 | `curl -H "Authorization: Bearer *** http://127.0.0.1:8000/api/events` |
| 模型调用慢 | 检查 DeepSeek API 网络连通性 |

---

## 5. API 端点一览

### 核心交互

| 端点 | 方法 | 说明 | 安全层 |
|------|------|------|--------|
| `/api/chat` | POST | AI 对话 | L0+L1+L2 |
| `/api/chat/stream` | POST | SSE 流式对话 | L0+L2 |
| `/api/autonomy` | POST | 自主循环 | L0+L2 |
| `/api/multi-agent` | POST | 多智能体协同 | 档位≥3 |

### Orchestrator 任务执行

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/orchestrator/run` | POST | 运行 Orchestrator（目标拆解→路由→执行→合并） |
| `/api/orchestrator/status` | GET | 当前 Orchestrator 任务状态 |
| `/api/orchestrator/history` | GET | Orchestrator 任务历史 |

### 状态查询

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | 当前状态（档位/熵值/确认模式） |
| `/api/events` | GET | 审计事件列表 |
| `/api/health` | GET | 健康检查（JSON，无 LLM 依赖） |
| `/api/stats` | GET | 统计信息 |

### 档位管理

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/switch` | POST | 切换档位 |
| `/api/approve-upgrade` | POST | 批准升级申请 |
| `/api/reject-upgrade` | POST | 拒绝升级申请 |
| `/api/reset` | POST | 重置状态 |

### 记忆系统

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/messageboard/memory/context` | GET | 聚合记忆上下文 |
| `/api/messageboard/memory/{type}` | GET | 按类型查询记忆（persona/fact/skill/episode） |
| `/api/messageboard/memory` | POST | 写入记忆 |
| `/api/messageboard/send` | POST | 多 Agent 通信 |
| `/api/messageboard/inbox/{agent}` | GET | Agent 收件箱 |

---

## 6. 安全架构（5 层防御）

```
用户输入
  │
  ▼
Layer 0: 意图预检 ────── gear<3 时拦截代码执行/文件操作意图
  │
  ▼
Adapter.run() ────────── PydanticAI (DeepSeek V4 Flash) / Hermes terminal
  │                         ↓
  │ ┌──────────────────────────────────────┐
  │ │ tool_calls 二次校验（validate_command）│  ← 危险命令/路径拦截
  │ └──────────────────────────────────────┘
  │
  ▼
execute_shell() ──────── Shell 白名单 + 危险模式正则 + NFKC Unicode 归一化 ← v3-alpha.1
  │
  ▼
verify_output() ──────── 输出校验（ADAPT 含12个危险命令特征）
  │
  ▼
state.append_event() ─── SHA-256 审计链（不可篡改）
```

### 混合执行引擎（v3.3 新增）

```
Orchestrator 收到用户目标
  │
  ▼
目标拆解 ──────────→ 子任务列表
  │
  ▼
依赖图拓扑排序 ──→ 确定并行/串行依赖
  │
  ▼
混合路由决策：
  ├── 工具密集型（subprocess/shell）→ Hermes terminal 执行
  ├── LLM 推理密集型                      → PydanticAI (DeepSeek V4 Flash)
  └── 确定性/数字任务                 → AutoGPT 容器（autogpt-sandbox）
  │
  ▼
上下文传递 ──────────→ 前置任务输出注入后续任务
  │
  ▼
结果合并 ────────────→ 最终安全报告
```

路由规则（orchestrator/rules.py）：
- `hermes`：工具密集型任务（explore-directory, dependency-check, static-analysis, dynamic-testing, run-bandit, generate-report）
- `pydanticai`：LLM 推理任务（identify-tasks, security-analysis, risk-assessment）

---

## 7. 当前系统能力清单

### 7.1 Orchestrator

| 能力 | 状态 | 说明 |
|------|------|------|
| 目标拆解 | ✓ | 用户目标 → 子任务列表（identify → execute → report） |
| 依赖图拓扑排序 | ✓ | Kahn 算法，支持并行任务和上下文注入 |
| 混合路由 | ✓ | hermes terminal / pydanticai / autogpt 三引擎路由 |
| 上下文传递 | ✓ | 前置任务 stdout 自动注入后续任务 |
| 结果合并 | ✓ | 所有子任务输出 → 结构化报告 |
| 危险度评估 | ✓ | 每个子任务分配 danger_level（1-5） |
| Flyweight 模式 | ✓ | 预定义任务模板复用 |

### 7.2 RedteamEvolver v3

| 能力 | 状态 | 说明 |
|------|------|------|
| 10 种变异策略 | ✓ | 编码混淆/语义等价/角色扮演/嵌套注入/链式攻击/Unicode/伪代码/多语言/对抗后缀/长尾分布 |
| 26 条 OWASP 种子 | ✓ | LLM01-LLM10 + PromptInject 变体 |
| 目标上下文注入 | ✓ | 读取项目文件，注入到攻击载荷上下文 |
| fitness 评估 | ✓ | bypass_rate × novelty × semantic_distance |
| 进化结束条件 | ✓ | max_rounds=20 | convergence_streak=3 | suite_size=200 |
| 定时执行 | ✓ | Hermes Cron 每周一凌晨 3 点 |

### 7.3 安全层

| 层 | 名称 | 功能 |
|----|------|------|
| L0 | 意图预检 | gear<3 拦截代码执行/文件操作意图 |
| L1 | tool_calls 二次校验 | validate_command() 危险命令/路径拦截 |
| L2 | Shell 白名单 | 白名单命令 + 危险模式正则 |
| L3 | 输出校验 | verify_output() ADAPT 特征检测 |
| L4 | 审计链 | SHA-256 不可篡改审计链 |

### 7.4 记忆系统

| 组件 | 状态 | 说明 |
|------|------|------|
| MessageBoard | ✓ | 统一记忆存储（persona/fact/skill/episode） |
| Hermes Memory Bridge | ✓ | integrations/hermes_memory_bridge.py，启动钩子 |
| 多 Agent 收件箱 | ✓ | inbox/{agent} 多 Agent 通信 |
| REST API | ✓ | /api/messageboard/memory 系列端点 |
| 记忆隔离 | ✓ | 记忆到 __memory__ 收件箱 |

---

## 8. 红队测试

### 8.1 手动运行

```bash
# 完整进化周期（运行测试 + 生成新用例）
python3 /root/EntropyGuard/security/run_redteam.py

# 仅测试，不生成新用例
python3 /root/EntropyGuard/security/run_redteam.py --dry-run
```

输出示例：
```
  ENTROPY RUNTIME · 红队进化报告 — 第 3 轮
  Suite: 17 → 18 条
  Tests: 17 run, 14 pass, 3 fail, +1 suite, +2 pending
```

### 8.2 定时执行（Hermes Cron）

每周一凌晨 3 点自动执行：

```bash
hermes cronjob list      # 查看
hermes cronjob run redteam-weekly-evolution  # 手动触发
```

### 8.3 待审批用例管理

高风险用例写入：`/root/EntropyGuard/security/pending_tests.json`

人工审批后，将用例从 pending_tests.json 移至 redteam_suite.json。

### 8.4 测试套件文件

| 文件 | 说明 |
|------|------|
| `security/redteam_suite.json` | 当前测试套件（上限 200 条） |
| `security/pending_tests.json` | 待审批高风险用例 |
| `security/evolution_history.json` | 进化历史记录 |
| `security/redteam_evolver.py` | 进化引擎代码（1224 行） |

---

## 9. 架构变更记录

| 版本 | 日期 | 变更内容 |
|------|------|---------|
| v2.1 | 2026-05-19 | 基础安全框架，PydanticAI 集成 |
| v2 | 2026-05-19 | 架构升级，安全修复，PydanticAI 迁移 |
| v3-alpha | 2026-05-21 | Orchestrator 骨架 + 多Agent路由 |
| v3-alpha.1 | 2026-05-21 | Unicode 归一化，route 危险度评估，超时修复 |
| v3 | 2026-05-24 | RedteamEvolver v3（web变异 + 目标上下文 + fitness） |
| v3.1 | 2026-05-28 | 泛化测试报告，记忆更新，Django 泛化测试 |
| v3.2 | 2026-05-28 | 目标拆解 + 依赖图拓扑排序 + JSON 解析鲁棒性 |
| **v3.3** | **2026-06-01** | **混合路由落地 — Hermes terminal + PydanticAI + AutoGPT** |

### v3.3 关键改进

| 维度 | 改进前 | 改进后 |
|------|--------|--------|
| 执行引擎 | 单一 PydanticAI (LLM) | Hermes terminal + PydanticAI + AutoGPT 混合 |
| 路由规则 | 固定路由 | 工具密集型→hermes，LLM→pydanticai，确定性→autogpt |
| 端到端耗时 | 462 秒 | 74 秒（6.3x 加速） |
| 报告质量 | 5/7 | 7/7（debug mode/secret_key/Werkzeug CVE 均检出） |
| 子任务超时 | 3 个工具超时（Bandit/curl/safety） | 全部完成（0 超时） |
| Git 提交 | cf7868a | 见下节 |

### v3.x Git 提交历史

```
cf7868a Entropy Runtime v3.3: route tool-intensive tasks to hermes terminal, fix timeout
39f8f7a [v3.2 hotfix] Robust JSON parsing: fallback for unescaped newlines inside strings
a9a4715 Entropy Runtime v3.2: Orchestrator goal decomposition with dependency graph
a6c5fbf Entropy Runtime v3.1: honest generalization report, memory update
e482a1c Entropy Runtime v3.1: second generalization test (Django)
b280b09 Entropy Runtime v3: RedteamEvolver v3 with web mutations, target context injection, extended fitness
438f517 Entropy Runtime v3-alpha.1: Unicode normalization, route danger assessment, timeout fix
7272dc6 Entropy Runtime v3-alpha: Orchestrator skeleton with multi-agent routing
```

---

## 10. 项目健康检查

| 指标 | 值 |
|------|-----|
| 项目文件总数 | 132 |
| Python 源文件 | 41 个 `.py` |
| 总代码行（项目代码） | 11,265 LOC |
| JSON 配置文件 | 11 个 |
| 文档文件 | 11 个 `.md` |
| HTML 页面 | 3 个 |
| 核心模块可导入 | 28/30（✓） |
| 服务状态 | running（13+ 小时） |
| 内存占用 | 26~37 MB |
| 审计事件数 | 1,421 |
| 工具调用数 | 1,217 |
| 健康检查 | HTTP 200 ✓ |
| API 状态查询 | HTTP 200 ✓ |
| DeepSeek API Key | 正常 |

> **注意**: `app.py`（Flask GAIA 助手，114 行）为独立遗留应用，非核心服务的一部分，需要 `flask` 包（当前 venv 未安装），不影响核心服务运行。`security/` 目录与 `security.py` 存在模块名冲突，`security/run_redteam.py` 需通过 `python -m` 或直接执行方式导入。

---

## 11. 常见故障排查

### 11.1 服务起不来

```bash
# 查看详细错误
journalctl -u entropyguard -n 50 --no-pager

# 检查启动前校验脚本
/usr/local/bin/entropyguard-verify-wrapper.sh
```

### 11.2 API 调用返回 401

```bash
# 检查密钥是否加载
systemctl show entropyguard -p EnvironmentFile
cat /root/.env | grep ENTROPY_RUNTIME_API_KEY

# 调试：检查进程环境变量
cat /proc/$(systemctl show entropyguard -p MainPID | cut -d= -f2)/environ | tr '\0' '\n' | grep ENTROPY
```

### 11.3 模型调用超时

```bash
# 测试 DeepSeek API 连通性
curl -s -o /dev/null -w "%{http_code}" https://api.deepseek.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
```

### 11.4 审计链文件损坏

```bash
# 查看 events.json 完整性
python3 -c "import json; json.load(open('/root/EntropyGuard/events.json'))"

# 如果损坏，可从备份恢复（如果有）
```

### 11.5 端口占用

```bash
# 查看 8000 端口占用
ss -tlnp | grep 8000

# 强制释放
fuser -k 8000/tcp
systemctl restart entropyguard
```

### 11.6 Healthcheck 失败

```bash
# 手动运行健康检查脚本
/usr/local/bin/entropyruntime-healthcheck.sh

# 脚本路径
/usr/local/bin/entropyruntime-healthcheck.sh
```

---

## 12. git 管理

```bash
# 查看当前提交
cd /root/EntropyGuard && git log --oneline -5

# 查看未提交的改动
git status --short

# 提交改动
git add -A
git commit -m "描述改动内容"
```
