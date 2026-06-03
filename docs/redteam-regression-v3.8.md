# 红队回归对比报告 — v3.8（最终版）

## 最终通过率

| 版本 | 通过 | 失败 | 通过率 |
|------|------|------|--------|
| v2.1 基线 | 33 | 3 | **91.7%** |
| v3.8 (修复前) | 24 | 12 | **66.7%** |
| v3.8 (修复后) | **36** | **0** | **100.0%** ✅ |

## 修复提交链

| 提交 | 描述 |
|------|------|
| `330bce4` | output verifier English patterns + test runner timeout 15→60s |
| `f7e661c` | revert overbroad intent detection; finalize verifier patterns |
| `ae7ae95` | update RT-0049 expectation; runner timeout 60→120s |
| `eaa65a0` | fast-path decode intercept for double-base64/hex commands |

## 修复的绕过类型

| 绕过 | 修复层级 | 模式 |
|------|----------|------|
| 英文执行报告 (executed, decoded the, piping to bash) | output_verifier | verification.py 新增信号 |
| 中文执行报告 (操作报告, 执行过程, 已执行并报告) | output_verifier | verification.py 新增信号 |
| 测试超时假阴性 | test runner | curl timeout 15→120s |
| 双 base64 编码绕过（96s LLM 处理） | intent_precheck | security.py 快速解码拦截 |
| hex→base64 双层编码绕过 | intent_precheck | security.py 快速解码拦截 |
| protected 路径写入 | output_verifier | verification.py 路径保护信号 |
