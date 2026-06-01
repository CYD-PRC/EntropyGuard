# Entropy Runtime · 自审计报告

> 执行日期: 2026-06-01
> 审计方式: Orchestrator `run_with_decomposition()` + 辅助工具
> 审计目标: `/root/EntropyGuard/` 全项目安全审计
> 覆盖范围: 代码质量、依赖漏洞、配置安全、API 安全、敏感信息泄露

---

## 1. 执行摘要

| 维度 | 结果 |
|------|------|
| Orchestrator 拆解 | 5 个子任务（DeepSeek 第一层, 17.57s） |
| 成功/总计子任务 | 1/5（仅 bandit 成功） |
| 路由分布 | hermes=2, pydanticai=2, autogpt=0 |
| Orchestrator 暴露缺陷 | **10 项**（P0=2, P1=3, P2=4, P3=1） |
| 安全发现 | 5 项（严重=1, 高危=2, 中危=2） |

---

## 2. Orchestrator 执行详情

### 2.1 子任务执行日志

| # | 任务ID | Agent | 耗时 | 状态 | 上下文 | 说明 |
|---|--------|-------|------|------|--------|------|
| 1 | task-code-quality | hermes | 0.49s | ✅ | 无依赖 | bandit 安全扫描成功 |
| 2 | task-secrets-scan | pydanticai | 120.1s | ❌ | 98 chars | /api/chat 超时 |
| 3 | task-dep-vuln | hermes | 60.1s | ❌ | 98 chars | pip-audit 超时 |
| 4 | task-config-audit | pydanticai | >180s | ❌ | 98 chars | LLM 推理卡住 |
| 5 | task-api-security | pydanticai | 未执行 | ❌ | N/A | 前置任务失败 |

### 2.2 Bandit 扫描结果（唯一成功的子任务）

任务 `task-code-quality` 通过 hermes 直接在 0.49s 内执行 bandit 成功，输出 3,516 字符。

**发现的代码安全问题**（从 hermes 执行器日志捕获）：
- B106: hardcoded_password_string — 密码硬编码
- B105: hardcoded_password_string — 可能的密码字符串
- 其他 hardcoded_* 检测命中

### 2.3 失败分析

**task-secrets-scan（120s 超时）**：
- decomposer 建议 agent=hermes，但 route() 返回 pydanticai
- 通过 /api/chat 执行 LLM 推理，120s 后超时
- 根本原因：路由偏差 + LLM 推理过慢

**task-dep-vuln（60s 超时）**：
- pip-audit 命令超时（pip 版本过旧 9.0.3，依赖树解析极慢）
- 手动验证：`pip-audit --desc` 仅返回 pip 版本警告，无漏洞输出
- 根本原因：pip-audit 自身性能问题 + 60s 硬超时

**task-config-audit（>180s）**：
- pydanticai LLM 通过 /api/chat 执行，生成漫无目的的工具调用序列
- 从 events.json 可看到：该任务遍历了 50+ 文件路径，生成 50+ 条审计事件
- 根本原因：LLM 缺乏任务边界约束，过度探索

---

## 3. Orchestrator 缺陷清单（10 项）

| # | 严重度 | 类别 | 详细描述 | 关联任务 |
|---|--------|------|----------|----------|
| 1 | **P0** | 路由偏差 | decomposer 建议 task-secrets-scan 走 hermes，route() 返回 pydanticai，导致 120s LLM 超时。decomposer 生成的 agent 分配与 rules.route() 实际返回值不一致 | task-secrets-scan |
| 2 | **P0** | API Key 硬编码 | `orchestrator/orchestrator.py:296` 中 `_call_qwen()` 直接将 `sk-e84...2509` 硬编码在源码中，已随 v3.4 commit 提交到 Git | 全部 |
| 3 | **P1** | 任务超时 | 5 个子任务中 2 个超时（40% 失败率），LLM 任务（120s）和工具任务（60s）均有超时 | task-secrets-scan, task-dep-vuln |
| 4 | **P1** | 依赖链阻塞 | 5 个任务中只有 task-code-quality 无依赖，其余 4 个均依赖它。该任务虽然成功，但后续 3 个任务全部失败，导致 task-api-security 永久等待 | task-api-security |
| 5 | **P1** | 串行化严重 | 5 个任务中 4 个有依赖关系（80% 串行化），最大依赖链深度为 2。实际上 task-secrets-scan 和 task-dep-vuln 完全可并行 | 全部 |
| 6 | **P2** | 上下文注入过短 | 上下文注入仅 98 字符（前一个任务的 bandit 输出摘要），2000 字符截断上限未被充分利用。后续任务几乎裸奔 | task-secrets-scan, task-dep-vuln, task-config-audit |
| 7 | **P2** | 空输出/假成功 | task-code-quality 成功返回，但 bandit JSON 输出文件（/tmp/bandit-self-audit.json）大小为 0 bytes，hermes 执行的 bandit 未正确写入输出文件或使用了 txt 格式 | task-code-quality |
| 8 | **P2** | 审计日志冗余 | task-config-audit 单次执行生成 50+ 条审计事件（TOOL_CALL），events.json 已膨胀至 1.08MB（1679 条事件）。无审计事件去重或聚合机制 | task-config-audit |
| 9 | **P2** | 冲突检测误报 | task-secrets-scan 执行前触发路径冲突检测，报警 `路径冲突 '/root/EntropyGuard/': task-code-static-analysis → task-secrets-scan`。两个任务一个读目录一个扫密钥，不冲突 | task-secrets-scan |
| 10 | **P3** | 依赖图缺失并行机会 | 5 个任务中 4 个共享依赖 `task-code-static-analysis`，但没有利用并行执行。Kahn 算法按优先级排序后串行执行，未使用 asyncio.gather | 全部 |

---

## 4. 安全发现（项目代码）

### 4.1 🔴 严重 — API Key 硬编码（CWE-798）

**文件**: `/root/EntropyGuard/orchestrator/orchestrator.py:296`
```python
api_key = "sk-e84cc5dc7d9d409b82b4709e1b5d2509"
```
- 该密钥是 Qwen-Max 的访问凭据，以明文硬编码在源代码中
- 已随 commit `35a379d` 进入 Git 历史
- 建议：迁移到环境变量 /root/.env

### 4.2 🔴 高危 — 敏感文件权限过松

**发现**（来自 task-config-audit 的审计输出）：
| 文件 | 权限 | 问题 |
|------|------|------|
| `/root/AutoGPT/source/.env` | 0644 (-rw-r--r--) | 任意用户可读，内含 OPENAI_API_KEY |
| `/root/.hermes/auth.json` | 0644 | 内含 DeepSeek API 凭据 |
| `/root/.env` | 0644 | 含 ENTROPY_RUNTIME_API_KEY 等多密钥 |

### 4.3 🟡 中危 — events.json 审计链含攻击痕迹明文

**文件**: `/root/EntropyGuard/events.json`（1.08MB, 1679 条事件）
- 包含完整的红队攻击记录：base64 编码命令、Unicode 混淆尝试、shell 注入尝试
- 所有攻击载荷、URL、编码后的命令以明文存储
- 若 events.json 被外部访问，可提取详细的漏洞利用模式

### 4.4 🟡 中危 — pip 版本过旧（9.0.3）

```
pip 9.0.3 → 当前最新 pip 24.x
```
- 5 年未升级
- pip-audit 因 pip 过旧无法可靠获取依赖树
- 导致 task-dep-vuln 耗时 60s 后超时仍无结果

### 4.5 🔴 高危 — 审计链 SHA-256 哈希链被中断

**文件**: `/root/EntropyGuard/events.json`
- events.json 每次启动重新加载，SHA-256 哈希链仅在单次进程生命周期内有效
- 服务重启后 hash chain 断裂，prev_hash 指向旧会话的哈希值
- 攻击者可以分段清除审计记录而不被发现

---

## 5. 路由分析

### 5.1 路由偏差表

| 任务 | decompose 建议 | route() 实际 | 是否一致 |
|------|---------------|-------------|---------|
| task-code-quality | hermes | hermes | ✅ |
| task-secrets-scan | hermes | **pydanticai** | ❌ |
| task-dep-vuln | pydanticai | **hermes** | ❌ |
| task-config-audit | pydanticai | pydanticai | ✅ |
| task-api-security | pydanticai | pydanticai | ✅ |

**问题**: 5 个任务中 2 个路由不一致（40%），decomposer 和 rules.route() 的决策逻辑存在分歧。

### 5.2 路由偏差根因

- decompose 的 `assigned_agent` 字段由 DeepSeek LLM 根据语义判断生成
- `orchestrator/rules.py` 的 `route()` 函数使用不同的规则（关键词匹配+危险度评估）
- 两者没有对齐的标准

---

## 6. 性能数据

| 指标 | 值 |
|------|-----|
| 总执行耗时（含超时） | >300s 被强制终止 |
| 实际有效执行时间（仅 bandit） | 0.49s |
| 浪费在超时等待的时间 | >299s |
| decompose 耗时 | 17.57s（DeepSeek API 调用） |
| 上下文注入大小 | 98 chars（远小于配置的 2000） |
| 审计事件增长率 | 50+ 事件/任务（配置审计任务） |

---

## 7. 改进建议（不修，仅记录）

1. **P0 修复链路**: 统一 decompose 和 route() 的路由标准，将 rules.py 的路由逻辑内联到 decompose 输出中
2. **P0 API Key**: 将 Qwen API Key 迁移到 /root/.env 环境变量
3. **P1 超时**: 区分 LLM 任务（60s 上限）和工具任务（20s 上限），失败后快速降级
4. **P1 依赖图**: 允许同层级的无冲突依赖并行执行（asyncio.gather）
5. **P2 上下文**: 上下文注入应传递完整输出，而非截断后仅 98 字符
6. **P2 冲突检测**: 路径冲突应区分读/写操作（读不互斥）
7. **P3 审计去重**: 类型相同的连续 TOOL_CALL 应聚合为一条审计事件
8. **安全**: events.json 应加密存储或仅保留编校版本
9. **安全**: 所有 `.env` 文件权限应降至 0600
10. **安全**: SHA-256 链应跨 session 持久化

---

## 附录 A: Orchestrator 日志摘要（self-audit-exec.log）

```
2026-06-01 23:09:11,210 | [Orchestrator] 第一层拆解: DeepSeek V4 Flash
2026-06-01 23:09:28,782 | [Orchestrator] DeepSeek 拆解成功: 5个子任务
2026-06-01 23:09:28,783 | [DangerAssessment] task-code-quality 含危险关键词，gear 4 → 2
2026-06-01 23:09:29,269 | [Hermes] task-code-quality: 完成 (3516 chars, exit=1)
2026-06-01 23:11:29,368 | [Orchestrator] Request error on /api/chat: timed out
2026-06-01 23:12:29,447 | [Hermes] task-dep-vuln: 命令超时 (60s)
```

## 附录 B: 工具检查补充结果

```bash
# Bandit（手动执行，排除 venv）
bandit -r /root/EntropyGuard -f txt --quiet \
  --exclude 'test-targets,venv310,orchestrator/venv,node_modules,.git,__pycache__'
# 输出: 扫描 15229 文件，含大量 site-packages

# 敏感信息搜索
grep -rn "sk-e84\|api_key\s*=\s*['\"][a-zA-Z0-9_-]\{20,\}" --include='*.py' \
  --exclude-dir=venv310 --exclude-dir=test-targets --exclude-dir=.git .
# 发现: orchestrator/orchestrator.py 硬编码 Qwen API Key
# 发现: events.json 包含全部审计事件明文

# 审计链统计
events.json: 1679 条事件, 1.08MB
- SHA-256 链完整性: ❌（进程重启后断裂）
- 攻击载荷明文存储: ✅（可追踪但也泄露）
```
