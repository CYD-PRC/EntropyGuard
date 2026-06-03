# Entropy Runtime v4.0 · 记忆闭环实战验证报告

- **验证日期**: 2026-06-03 20:13:02
- **运行前 episode 数**: 57
- **运行前 fact 数**: 3

## 1️⃣ decompose 历史经验注入验证

- **Flask (第一次) decompose prompt**: ✅ 包含历史经验

  - `- "intent": 可执行的指令（明确、具体、可直接交给 Agent 执行）`
  - `- "expected_agent": 建议的 Agent，可选 "pydanticai"/"autogpt"/"hermes"`
  - `CPU: 2.5% | 内存: 89.6% | 磁盘: 61.0%`
  - `=== 历史经验（基于近期 episode 统计） ===`
  - `[Agent 历史表现]`
  - `hermes: 88% 成功率 (14/16)`
  - `pydanticai: 25% 成功率 (1/4)`
  - `文件操作: 11 次执行, 100% 成功率`
  - `安全扫描: 14 次执行, 86% 成功率`
  - `代码分析: 5 次执行, 80% 成功率`
  - `shell 执行: 3 次执行, 33% 成功率`
  - `依赖检查: 3 次执行, 0% 成功率`
  - `近期平均任务耗时: 0.3s`
  - `请根据以上历史经验优化拆解策略：选择历史上成功率高的 Agent，避免重复已知的失败模式。`
  - `[1] task-explore-structure | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
  - `[2] task-check-settings | 耗时 0.11s | 输出: 目标目录: /root/EntropyGuard/test-targets`
  - `[3] task-static-analysis | 耗时 0.09s | 输出: 目标目录: /root/EntropyGuard/test-targets`

- **Django (第二次) decompose prompt**: ✅ 包含历史经验
  - 经验注入详情：
    - `- "intent": 可执行的指令（明确、具体、可直接交给 Agent 执行）`
    - `- "expected_agent": 建议的 Agent，可选 "pydanticai"/"autogpt"/"hermes"`
    - `CPU: 2.5% | 内存: 87.4% | 磁盘: 61.0%`
    - `=== 历史经验（基于近期 episode 统计） ===`
    - `[Agent 历史表现]`
    - `pydanticai: 100% 成功率 (1/1)`
    - `hermes: 89% 成功率 (17/19)`
    - `代码分析: 7 次执行, 100% 成功率`
    - `安全扫描: 15 次执行, 100% 成功率`
    - `文件操作: 14 次执行, 100% 成功率`
    - `shell 执行: 3 次执行, 33% 成功率`
    - `依赖检查: 2 次执行, 0% 成功率`
    - `近期平均任务耗时: 0.2s`
    - `请根据以上历史经验优化拆解策略：选择历史上成功率高的 Agent，避免重复已知的失败模式。`
    - `[1] task-dependency-review | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
    - `[2] task-static-analysis | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
    - `[3] task-explore-structure | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
  - ✅ 引用了第一次的 Flask 分析记录作为参考

## 2️⃣ 相似 episode 检索验证

- ✅ **第二次 (Django) decompose 找到了相似历史任务**
  - 相似任务清单（从 prompt 提取）：
    - `[1] task-dependency-review | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
    - `[2] task-static-analysis | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`
    - `[3] task-explore-structure | 耗时 0.0s | 输出: === Flask 应用源码分析 ===`

## 3️⃣ 路由历史参考验证

- **第一次 (Flask) 路由决策**: 3 个
  - `task-explore-structure` → **hermes** (gear=3)
  - `task-static-analysis` → **hermes** (gear=3)
  - `task-dependency-review` → **hermes** (gear=3)

- **第二次 (Django) 路由决策**: 3 个
  - `task-explore-directory` → **hermes** (gear=3)
  - `task-security-analysis` → **pydanticai** (gear=2)
  - `task-generate-report` → **pydanticai** (gear=3)

- ℹ️ 未检测到 Agent 降级（所有路由正常）
- ✅ 第二次路由有 Agent 调整: Flask={'hermes'} vs Django={'hermes', 'pydanticai'}

## 4️⃣ 性能对比（第一次 vs 第二次）

| 指标 | 第一次 (Flask) | 第二次 (Django) | 变化 |
|------|---------------|----------------|------|
| 执行时间 | 42.44s | 99.71s | ⬆️ 劣化 |
| 子任务数 | 3 | 3 | ➡️ |
| 成功率 | 3/3 | 3/3 | ✅ 持平 |
| 墙钟时间 | 43.58s | 100.98s | ⬆️ 劣化 |

### 子任务耗时详情

| 任务 | 第一次 (Flask) | 第二次 (Django) |
|------|---------------|----------------|
| task-001 | ✅ [hermes] 0.0s | ✅ [hermes] 2.3s |
| task-002 | ✅ [hermes] 0.0s | ✅ [pydanticai] 0.1s |
| task-003 | ✅ [hermes] 0.0s | ✅ [pydanticai] 75.1s |

## 5️⃣ 并行执行验证

- ℹ️ 第一次 (Flask) 并行度不足或串行执行
  - 总耗时: 42.44s, 子任务耗时和: 0.0s
- ℹ️ 第二次 (Django) 并行度不足或串行执行
  - 总耗时: 99.71s, 子任务耗时和: 77.5s
- ⚠️ 未检测到明显的任务并行执行

## 6️⃣ 自动重试检测

- ✅ **检测到 7 个重试 episode**
  - `task-dependency-scan`: Agent `hermes` retry 2x | ❌ 失败 | Safety 依赖检查 执行超时 (60s)
  - `task-dependency-scan`: Agent `hermes` retry 1x | ❌ 失败 | Safety 依赖检查 执行超时 (60s)
  - `task-dependency-security`: Agent `autogpt` retry 2x | ✅ 成功 | 
  - `task-dependency-security`: Agent `hermes` retry 2x | ❌ 失败 | Safety 依赖检查 执行超时 (60s)
  - `task-dependency-security`: Agent `hermes` retry 1x | ❌ 失败 | Safety 依赖检查 执行超时 (60s)
  - `v3.7-test-all-fail`: Agent `autogpt` retry 3x | ❌ 失败 | 所有 Agent 均执行失败
  - `v3.7-test-retry`: Agent `pydanticai` retry 2x | ✅ 成功 | 


## 7️⃣ episode 生命周期管理

- ℹ️ 未新增 fact（episode 数未达 100 条阈值，不触发生命周期管理）

## 📊 汇总结论

✅ **[PASS] decompose 经验注入**: 第二次 Django prompt 包含历史经验统计
✅ **[PASS] 路由参考历史**: Django 路由中 1 个分配到 hermes（参考了历史成功率）
✅ **[PASS] 相似 episode 复用**: Django decompose 引用了相似历史任务
ℹ️ **[INFO] 性能变化**: 第一次 42.44s → 第二次 99.71s
ℹ️ **[INFO] 并行执行**: 本次运行串行执行（可接受）
✅ **[PASS] 自动重试**: 7 个任务触发自动重试

---
*报告生成时间: 2026-06-03 20:13:02*
*Entropy Runtime v4.0 - Memory Closed Loop Validation*