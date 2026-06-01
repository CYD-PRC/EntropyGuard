# Entropy Runtime · 运维手册

> 版本: 2.0.0 | 最后更新: 2026-05-31
> 部署位置: 阿里云 ECS | IP: 8.153.99.156 | SSH 端口: 2222

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
DEEPSEEK_API_KEY=***        ← DeepSeek 模型调用密钥
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

服务器重启后，运行快速冒烟测试验证服务正常：

```bash
python3 /tmp/e2e_smoke2.py
```

预期输出：`6/6 PASSED`

如果失败，按以下顺序排查：

| 症状 | 排查步骤 |
|------|---------|
| 服务未运行 | `systemctl status entropyguard` → 检查日志 |
| 401 拒绝 | 检查 `/root/.env` 中的 `ENTROPY_RUNTIME_API_KEY` |
| 500 错误 | `journalctl -u entropyguard -n 50` 查看 Python 堆栈 |
| 审计链为空 | `curl -H "Authorization: Bearer <key>" http://127.0.0.1:8000/api/events` |
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

### 状态查询

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/state` | GET | 当前状态（档位/熵值/确认模式） |
| `/api/events` | GET | 审计事件列表 |
| `/api/health` | GET | 健康检查 |
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
| `/api/messageboard/memory/{type}` | GET | 按类型查询记忆 |
| `/api/messageboard/memory` | POST | 写入记忆 |
| `/api/messageboard/send` | POST | 多 Agent 通信 |
| `/api/messageboard/inbox/{agent}` | GET | Agent 收件箱 |

---

## 6. 安全架构

```
用户输入
  │
  ▼
Layer 0: 意图预检 ────── gear<3 时拦截代码执行/文件操作意图
  │
  ▼
Adapter.run() ────────── PydanticAI (DeepSeek V4 Flash)
  │                         ↓
  │ ┌──────────────────────────────────────┐
  │ │ tool_calls 二次校验（validate_command）│  ← P1 新增
  │ └──────────────────────────────────────┘
  │
  ▼
execute_shell() ──────── Shell 白名单 + 危险模式正则 + 受保护路径
  │
  ▼
verify_output() ──────── 输出校验（ADAPT 含12个危险命令特征） ← P1 增强
  │
  ▼
state.append_event() ─── SHA-256 审计链（不可篡改）
```

---

## 7. 红队测试

### 7.1 手动运行

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

### 7.2 定时执行（Hermes Cron）

每周一凌晨 3 点自动执行：

```bash
hermes cronjob list      # 查看
hermes cronjob run redteam-weekly-evolution  # 手动触发
```

### 7.3 待审批用例管理

高风险用例写入：`/root/EntropyGuard/security/pending_tests.json`

人工审批后，将用例从 pending_tests.json 移至 redteam_suite.json。

### 7.4 测试套件文件

| 文件 | 说明 |
|------|------|
| `security/redteam_suite.json` | 当前测试套件（上限 200 条） |
| `security/pending_tests.json` | 待审批高风险用例 |
| `security/evolution_history.json` | 进化历史记录 |
| `security/redteam_evolver.py` | 进化引擎代码 |

---

## 8. 常见故障排查

### 8.1 服务起不来

```bash
# 查看详细错误
journalctl -u entropyguard -n 50 --no-pager

# 检查启动前校验脚本
/usr/local/bin/entropyguard-verify-wrapper.sh
```

### 8.2 API 调用返回 401

```bash
# 检查密钥是否加载
systemctl show entropyguard -p EnvironmentFile
cat /root/.env | grep ENTROPY_RUNTIME_API_KEY

# 调试：检查进程环境变量
cat /proc/$(systemctl show entropyguard -p MainPID | cut -d= -f2)/environ | tr '\0' '\n' | grep ENTROPY
```

### 8.3 模型调用超时

```bash
# 测试 DeepSeek API 连通性
curl -s -o /dev/null -w "%{http_code}" https://api.deepseek.com/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"deepseek-chat","messages":[{"role":"user","content":"hi"}]}'
```

### 8.4 审计链文件损坏

```bash
# 查看 events.json 完整性
python3 -c "import json; json.load(open('/root/EntropyGuard/events.json'))"

# 如果损坏，可从备份恢复（如果有）
```

### 8.5 端口占用

```bash
# 查看 8000 端口占用
ss -tlnp | grep 8000

# 强制释放
fuser -k 8000/tcp
systemctl restart entropyguard
```

---

## 9. git 管理

```bash
# 查看当前提交
cd /root/EntropyGuard && git log --oneline -5

# 查看未提交的改动
git status --short

# 提交改动
git add -A
git commit -m "描述改动内容"
```
