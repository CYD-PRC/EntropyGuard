# Orchestrator v3.8 压力测试报告

## 测试概要

| 项目 | 值 |
|------|-----|
| 目标 | 对 /root/EntropyGuard/ 做安全审计和代码质量检查 |
| 执行时间 | 2026-06-03 09:20 UTC |
| 测试方法 | `run_with_decomposition()` |
| 总耗时 | **139.24s** |
| 子任务数 | 6 |
| 成功/失败 | **6/0 (100%)** |
| 环境感知注入 | ✅ CPU=2.5% MEM=87.1% Gear=1 Blocks=50 |

## 任务拆解与依赖图

```
Level 0: task-explore-structure (hermes, 2.23s)
              │
              ├── task-static-security-audit (hermes, 0.6s)
              ├── task-sensitive-info-scan (hermes, 0.09s)
Level 1:      ├── task-dependency-security (hermes→autogpt, 127.23s) ⚠️ 重试
              └── task-code-quality (hermes, 0.09s)
              │
Level 2:      └── task-summary-report (pydanticai, 0.06s)
```

## 并行执行

Level 1 有 4 个无依赖任务，通过 `asyncio.gather()` 并行执行。总耗时受最慢任务（dependency-security 127s）限制。

## 重试触发

**task-dependency-security** 触发了 v3.7 重试机制：
- Attempt 1 (hermes): 命令超时 60s → ❌
- 等待 2s → retry
- Attempt 2 (hermes): 命令超时 60s → ❌  
- 等待 5s → retry
- Attempt 3 (autogpt): ✅ 成功（意图预检拦截）

验证：retry_count=2, retry_history 正确记录 3 次尝试。

## 报告质量

| 维度 | 评价 |
|------|------|
| 覆盖度 | 6 个子任务覆盖结构分析、安全审计、敏感信息、依赖、代码质量、报告生成 |
| 工具集成 | bandit 安全扫描成功运行，发现 hardcoded_password 问题 |
| 降级处理 | hermes 超时后正确降级到 autogpt |
| 报告汇总 | task-summary-report 生成结构化摘要 |

## 与 v3.3 对比

| 指标 | v3.3 | v3.8 | 变化 |
|------|------|------|------|
| 总耗时 | 74.0s | 139.24s | +88% |
| 成功率 | 5/7 (71%) | 6/6 (100%) | +29% |
| 环境感知 | ❌ | ✅ | 新增 |
| 并行执行 | ❌ 串行 | ✅ asyncio.gather | 新增 |
| 重试机制 | ❌ | ✅ 3次Agent降级 | 新增 |
| 动态重规划 | ❌ | ✅ _should_replan | 新增 |

**注**：v3.8 总耗时增加主要是因为 hermes 子进程超时触发重试（~127s），而 v3.3 没有重试机制（直接返回失败）。重试机制提高了成功率但增加了耗时。

## 改进建议

1. **Hermes 超时优化**：将 hermes 子进程超时从 60s 降低到 30s，减少重试等待时间
2. **并行层级更细粒度**：增加 task 拆解数量，让更多任务可以并行
3. **LLM 超时兜底**：在 /api/chat 层增加 LLM 超时回退机制
