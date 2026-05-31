# Entropy Runtime v2.1 — 技术债务与代码审计报告

> 审计日期: 2026-06-01
> 审计范围: 阶段二 — 代码审计
> 审计文件: runtime_api.py, agent_api.py, pydanticai_adapter.py, redteam_evolver.py

---

## 1. routes/runtime_api.py — P1 (高风险 2项, P2 中风险 3项)

### P1-01: /api/events 无输入类型校验 (第82-117行)
**严重性**: P1 — 高风险
**位置**: `POST /api/events`, 第85-117行
**问题**: `event_type`、`actor`、`delta_entropy` 等字段直接从请求 JSON 中提取，未做类型校验。攻击者可传入非预期类型触发异常或绕过审计链。`delta_entropy` 未限制范围，可注入超大值影响控制熵计算。
**建议**: 添加 Pydantic 模型进行类型校验，限制 `delta_entropy` 范围 [-10, 10]，限制 `event_type` 为枚举值。

### P1-02: /api/health 使用 subprocess 调用外部脚本 (第498-514行)
**严重性**: P1 — 高风险
**位置**: `GET /api/health`, 第498-514行
**问题**: 直接 `subprocess.run(["/usr/local/bin/entropyruntime-healthcheck.sh"])` 调用外部脚本。如果脚本不存在或脚本被替换，将导致命令执行。路径硬编码，没有回退或验证机制。
**建议**: (1) 对 healthcheck 脚本文件做 owner/perm 验证 (os.stat + os.access X_OK)，(2) 添加脚本不存在时的纯 Python 健康检查回退。

### P2-01: /api/switch 参数未校验档位范围 (第288-304行)
**严重性**: P2 — 中风险
**位置**: `POST /api/switch`, 第289-290行
**问题**: `new_gear = int(data.get("gear"))` 直接转换，未验证 gear 是否在 [1,4] 范围内。state.switch_gear 可能处理无效档位。
**建议**: 添加范围检查 `if new_gear not in (1,2,3,4): raise HTTPException(400, ...)`

### P2-02: /api/batch-approve 无条件设置 (第232-241行)
**严重性**: P2 — 中风险
**位置**: `POST /api/batch-approve`, 第232-241行
**问题**: 无论 `approved` 是 true 还是 false，都返回 200。approved=false 时只返回消息"切换到 EMBRACE"，实际并未执行切换。
**建议**: approved=false 时调用 `state.switch_gear(1, "down", "api")`。

### P2-03: /api/reset 无权限分级 (第378-418行)
**严重性**: P2 — 中风险
**位置**: `POST /api/reset`, 第378-418行
**问题**: 任何持有 API Token 的调用者都能重置整个系统状态（包括审计链）。没有二次确认机制或角色分级。
**建议**: 添加二次确认 header (`X-Confirm-Reset: yes-i-am-sure`)，或添加只读 token 与管理员 token 的区分。

---

## 2. routes/agent_api.py — P1 (高风险 2项, P2 中风险 2项)

### P1-03: /api/autogpt/tool 使用 subprocess 执行 Python 代码 (第396-416行)
**严重性**: P1 — 高风险
**位置**: `POST /api/autogpt/tool`, 第396-416行
**问题**: 通过 subprocess 调用 `sys.executable -c "..."` 执行动态 Python 代码，payload 通过 sys.argv[1] 传入。虽然 payload 是 json.dumps 的，但 execution 路径可能被利用。且代码路径硬编码 `/root/AutoGPT/source`。
**建议**: (1) 改为直接 import 审计函数而非 subprocess，(2) 或至少验证 source 路径合法性。

### P1-04: 流式输出无 Token 级认证限流 (第325-375行)
**严重性**: P1 — 高风险
**位置**: `POST /api/chat/stream`, 第325-375行
**问题**: SSE 流式端点通过 FastAPI 依赖注入验证 token，但流式连接可长时间保持。缺少速率限制和连接数限制，可能被用于资源耗尽攻击。
**建议**: 添加 (1) 最大并发流式连接数限制，(2) 每 token/每秒速率限制，(3) 连接超时。

### P2-04: /api/autonomy 步骤数和模型无限制 (第456-510行)
**严重性**: P2 — 中风险
**位置**: `POST /api/autonomy`, 第456-510行
**问题**: `max_steps=10` 默认值虽然合理，但未设上限。循环中可配置 model_id 和 gear，可能导致非预期行为。
**建议**: (1) 限制 max_steps <= 20，(2) 限制 model_id 为已注册模型。

### P2-05: /api/multi-agent 留言板内容注入 (第566-575行)
**严重性**: P2 — 中风险
**位置**: `POST /api/multi-agent`, 第566-575行
**问题**: 留言板内容直接拼接到 Agent B 的 prompt 中，若 Agent A 的回复包含恶意内容，可注入 Agent B 的上下文。
**建议**: 在拼接前对 board_content 做长度限制和内容过滤。

---

## 3. adapters/pydanticai_adapter.py — P1 (高风险 1项, P2 中风险 4项)

### P1-05: risk_score 检测过于简单 (第176-179行, 第244-248行)
**严重性**: P1 — 高风险
**位置**: `run()` 和 `run_stream()` 中的 risk_score 检测
**问题**: 仅通过关键词匹配（"rm ", "chmod ", "dd ", "mkfs", "> /dev/" 等）判断风险。编码/混淆后的命令完全绕过检测。且未对 AI 输出做解码预处理。
**建议**: 复用 verification.py 的校验逻辑，添加 base64/hex/unicode 解码预处理。

### P2-06: API Key 以明文方式读取 .env 文件 (第40-52行)
**严重性**: P2 — 中风险
**位置**: `_get_api_key()`, 第40-52行
**问题**: 手动解析 /root/.env 文件获取 API Key，没有使用 python-dotenv。文件不存在时静默 pass。多进程环境下可能读不到。
**建议**: 统一使用 `python-dotenv` 加载，或从环境变量直接读取。

### P2-07: run_shell_tool 无档位权限检查 (第74-97行)
**严重性**: P2 — 中风险
**位置**: `run_shell_tool`, 第74-97行
**问题**: run_shell_tool 内部没有检查当前档位是否允许执行 shell 命令。虽然 PydanticAI 系统提示包含权限说明，但没有硬性约束。
**建议**: 在调用 execute_shell 前检查 gear 参数，gear < 3 时拒绝。

### P2-08: 审计链写入 gear_name 硬编码为 "ADAPT" (第88行, 第110行)
**严重性**: P2 — 中风险
**位置**: `run_shell_tool` 和 `http_request_tool`, 第88行, 第110行
**问题**: 审计事件中的 `gear_name` 硬编码为 "ADAPT"，未使用实际传入的 gear 参数。
**建议**: 从 context 获取实际 gear 名称写入审计链。

### P2-09: 流式逐 token 积累无边界 (第227行)
**严重性**: P2 — 中风险
**位置**: `run_stream()`, 第227行
**问题**: `full_reply += delta` 逐 token 拼接，无最大长度限制。超长回复导致内存 OOM。
**建议**: 添加 `max_tokens` 参数（默认 16384），超出截断。

---

## 4. security/redteam_evolver.py — P1 (高风险 2项, P2 中风险 3项)

### P1-06: 变异引擎绕过安全检测 (第502-628行)
**严重性**: P1 — 高风险
**位置**: `mutate()` 和 `_apply_technique()`
**问题**: 变异引擎生成的 base64/hex/unicode 编码命令在测试时不会被 verification.py 检测到，因为 verification.py 按明文信号匹配。这些变异用例全部失败，说明安全层存在系统性盲区。
**建议**: 见 verification.py + security.py 解码预处理修复。

### P1-07: V2 变异引擎全角字符映射 (第589-594行)
**严重性**: P1 — 高风险
**位置**: `_apply_technique(unicode)`, 第589-594行
**问题**: 只映射了有限的 ASCII 字符到全角，映射不完整（缺少数字、大写字母、特殊符号）。容易产生不可解码的混合字符。
**建议**: 使用完整的 Unicode NFKC 规范化，所有全角/半角字符归一化。

### P2-10: DeepSeek API 调用前后处理粗糙 (第97-143行)
**严重性**: P2 — 中风险
**位置**: `_call_deepseek()`, 第97-143行
**问题**: (1) API Key 与 pydanticai_adapter.py 重复解析逻辑，(2) 无超时重试，(3) 无 token 用量统计，(4) 异常时静默返回 None 不影响流程但丢失上下文。
**建议**: (1) 提取公共 API key 加载函数，(2) 添加自动重试 (3次)，(3) 记录 usage 到审计日志。

### P2-11: run_existing_tests 对空/异常响应处理弱 (第237-249行)
**严重性**: P2 — 中风险
**位置**: `run_existing_tests()`, 第237-249行
**问题**: HTTP code 解析仅靠 `output[-3:]`，当响应体包含换行或非标准格式时容易解析错误。
**建议**: 使用 `-w "%{http_code}"` 分离输出 (使用 `--write-out`)，或解析 stdout 最后3字符前先去除尾随空白。

### P2-12: 测试用例 expected 字段与判断逻辑耦合 (第252-258行)
**严重性**: P2 — 中风险
**位置**: `run_existing_tests()`, 第252-258行
**问题**: expected 值为字符串（如 "blocked_by_verifier"）直接与 actual 比较。当服务返回不同格式时（如 "blocked_by_verifier" vs "blocked_by_verifier\n"），判定出错。
**建议**: 规范化 expected 和 actual 后再比较，移除尾随空白和换行。

---

## 汇总

| 严重性 | 数量 | 说明 |
|--------|------|------|
| P1 (高风险) | 7 | 编码注入绕过×2、subprocess风险×2、认证限流×1、外部脚本×1、类型校验×1 |
| P2 (中风险) | 12 | 参数校验×3、权限分级×2、内容注入×1、Key管理×1、流控×1、测试工具×3、硬编码×1 |
| **合计** | **19** | |

### 本期已修复
- P1-06: verification.py + security.py 解码预处理（见下节）
- P1-05: pydanticai_adapter.py risk_score 复用 verification.py 校验
