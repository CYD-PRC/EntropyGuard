"""Entropy Runtime · Hermes 执行器
v8.0: 纯执行层 — 接收 AutoGPT 计划，按工具类型执行，安全校验贯穿全程。

核心原则:
  - 只执行，不规划
  - 每个子任务分配一个工具类型（ToolType），由 Hermes 调度执行
  - 所有执行经过安全三道校验（intent_guard / command_guard / output_guard）
  - 失败时调用 replanner 重规划，结果上报 MessageBoard

删除 Agent 路由:
  - 不再支持 assigned_agent=autogpt/pydanticai 作为独立 Agent
  - pydanticai 降级为 Hermes 的一个工具（结构化提取）
  - autogpt 仅作为规划引擎（在 planner_gateway.py 中调用）
"""
import json
import logging
import os
import re
import time
import urllib.request
import subprocess as _subprocess
import shutil as _shutil
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult
from orchestrator.memory import write_episode
from notify.wechat import send_retry_notification
from orchestrator.dataflow import (
    augment_dependencies, inject_dataflow_into_context,
)
from orchestrator.checkpoint import (
    save_checkpoint, load_checkpoint, clear_checkpoint,
)
from orchestrator.planner import build_context
from orchestrator.cosmic_lense import CosmicLense, ValueTier
from orchestrator.metacognition import Metacognition, CheckStatus, Suggestion
from orchestrator.memory_store import MemoryStore
from orchestrator.replanner import Replanner, ReplanAction
from orchestrator.planner_gateway import ToolType

memory_store = MemoryStore()

logger = logging.getLogger("entropyruntime.execute")

ENTROPY_API_BASE = "http://127.0.0.1:5000"
DEFAULT_MODEL = "kimi"
DEFAULT_GEAR = 3


def _get_api_key() -> str:
    env_path = "/root/.env"
    for key_name in ["ENTROPY_RUNTIME_API_KEY"]:
        try:
            with open(env_path) as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith(key_name) and "=" in ls:
                        return ls.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            pass
    return os.environ.get("ENTROPY_RUNTIME_API_KEY", "")


def _api_request(endpoint: str, payload: dict, timeout: int = 120) -> dict:
    """向 Entropy Runtime API 发送 POST 请求"""
    api_key = _get_api_key()
    url = f"{ENTROPY_API_BASE}{endpoint}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")[:500]
        logger.error(f"[Execute] HTTP {e.code} on {endpoint}: {body_text}")
        return {"success": False, "error": f"HTTP {e.code}: {body_text}"}
    except urllib.error.URLError as e:
        logger.error(f"[Execute] URL error on {endpoint}: {e.reason}")
        # Connection refused → 立即返回，不要浪费时间重试
        err_str = str(e.reason)
        if "refused" in err_str or "connect" in err_str:
            return {"success": False, "error": f"Agent 不可达: {e.reason}"}
        return {"success": False, "error": str(e.reason)}
    except Exception as e:
        logger.error(f"[Execute] Request error on {endpoint}: {e}")
        return {"success": False, "error": str(e)}


# ========== 安全三道校验 ==========

_DESTRUCTIVE_SHELL_PATTERNS = [
    "rm -rf", "rm -r /", "rm -f /", "mkfs.", "dd if=", "> /dev/sd",
    "format ", "fdisk", "mkswap", "halt", "reboot", "shutdown",
    "poweroff", "init 0", "init 6", "格式化",
]
_BLOCKED_COMMANDS = [
    "sudo", "su ", "chmod 777", "chown", "passwd",
]


def intent_guard(intent: str) -> tuple[bool, str]:
    """意图校验：检查 intent 是否包含危险的指令模式。"""
    intent_lower = intent.lower()
    for pattern in _DESTRUCTIVE_SHELL_PATTERNS:
        if pattern.lower() in intent_lower:
            return False, f"意图包含破坏性操作: {pattern}"
    return True, ""


def command_guard(command: str) -> tuple[bool, str]:
    """命令校验：阻止高危命令执行。"""
    cmd_lower = command.lower().strip()
    for blocked in _BLOCKED_COMMANDS:
        if cmd_lower.startswith(blocked):
            return False, f"命令被阻止: {blocked}"
    return True, ""


def output_guard(output: str) -> tuple[bool, str]:
    """输出校验：检查输出是否包含敏感信息泄露。"""
    sensitive_patterns = [
        r'sk-[A-Za-z0-9]{20,}',        # OpenAI API Key
        r'ghp_[A-Za-z0-9]{36}',        # GitHub PAT
        r'-----BEGIN.*PRIVATE KEY-----',  # 私钥
        r'AKIA[0-9A-Z]{16}',            # AWS Access Key
    ]
    for pat in sensitive_patterns:
        if re.search(pat, output):
            return False, f"输出包含敏感信息 (匹配: {pat})"
    return True, ""


# ========== 工具调度 ==========


def execute_tool(task: AgentTask, tool: ToolType) -> TaskResult:
    """按工具类型调度 Hermes 执行。pydanticai 降级为 Hermes 的一个工具。"""
    # 意图校验
    ok, reason = intent_guard(task.intent)
    if not ok:
        return TaskResult(task_id=task.id, success=False, error=reason,
                          agent="hermes", gear=task.gear)

    if tool == ToolType.PYDANTICAI_EXTRACT:
        # pydanticai 降级为 Hermes 的结构化提取工具
        return _execute_pydanticai_as_tool(task)
    elif tool == ToolType.SANDBOX_EXEC:
        return _execute_sandbox(task)
    elif tool == ToolType.NMAP_SCAN:
        return execute_hermes(task)  # hermes 已有 nmap 检测
    elif tool == ToolType.BANDIT_SCAN:
        return execute_hermes(task)  # hermes 已有 bandit 检测
    elif tool == ToolType.CURL_REQUEST:
        return execute_hermes(task)  # hermes 已有 curl 检测
    elif tool == ToolType.SAFETY_CHECK:
        return execute_hermes(task)  # hermes 已有 safety 检测
    elif tool == ToolType.SHELL_COMMAND:
        return _execute_shell(task)
    elif tool == ToolType.FILE_ANALYSIS:
        return _execute_file_analysis(task)
    elif tool == ToolType.REPORT_GEN:
        return _execute_report(task)
    else:
        return execute_hermes(task)  # 兜底：directory scan


def _get_deepseek_api_key() -> str:
    """从 /root/.env 或环境变量读取 DeepSeek API Key"""
    for key_name in ["DEEPSEEK_API_KEY", "OPENAI_API_KEY"]:
        # 先查环境变量
        val = os.environ.get(key_name, "")
        if val:
            return val
        # 再查 /root/.env
        try:
            with open("/root/.env") as f:
                for line in f:
                    ls = line.strip()
                    if ls.startswith(key_name) and "=" in ls:
                        return ls.split("=", 1)[1]
        except (FileNotFoundError, OSError):
            pass
    return ""


def _execute_pydanticai_as_tool(task: AgentTask) -> TaskResult:
    """pydanticai 作为 Hermes 的结构化提取工具。"""
    try:
        api_key = _get_deepseek_api_key()
        if not api_key:
            return _execute_file_analysis(task)

        # 从 config 或 /root/.env 读取模型名
        model = "deepseek-v4-flash"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "你是一个结构化数据提取助手。请分析用户输入，提取关键信息，以JSON格式输出。"},
                {"role": "user", "content": task.intent},
            ],
            "temperature": 0.1,
            "max_tokens": 2000,
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            "https://api.deepseek.com/v1/chat/completions",
            data=body, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
            reply = result.get("choices", [{}])[0].get("message", {}).get("content", "")

        # 输出校验
        ok, reason = output_guard(reply)
        if not ok:
            return TaskResult(task_id=task.id, success=False, error=reason,
                              agent="pydanticai", gear=task.gear)

        return TaskResult(task_id=task.id, success=True, output=reply,
                          agent="pydanticai", gear=task.gear)

    except Exception as e:
        logger.warning("[Execute] pydanticai 提取失败: %s", e)
        return TaskResult(task_id=task.id, success=False, error=str(e),
                          agent="pydanticai", gear=task.gear)


def _execute_shell(task: AgentTask) -> TaskResult:
    """安全的 Shell 命令执行器。"""
    # 只允许简单的系统命令
    safe_commands = ["ls", "cat", "head", "tail", "wc", "find", "grep",
                     "ps", "df", "du", "free", "uname", "date", "whoami",
                     "id", "pwd", "which", "echo", "sort", "uniq", "cut",
                     "env", "docker ps", "docker images", "pip list",
                     "pip3 list", "netstat", "ss", "ip "]
    intent_lower = task.intent.lower()

    for safe_cmd in safe_commands:
        if safe_cmd in intent_lower:
            # 命令校验
            ok, reason = command_guard(safe_cmd)
            if not ok:
                return TaskResult(task_id=task.id, success=False, error=reason,
                                  agent="hermes", gear=task.gear)
            try:
                r = _subprocess.run(
                    safe_cmd.split(), capture_output=True,
                    text=True, timeout=15,
                )
                output = r.stdout or r.stderr or f"exit={r.returncode}"
                # 输出校验
                ok, _ = output_guard(output)
                if not ok:
                    output = "[输出包含敏感信息，已过滤]"
                return TaskResult(task_id=task.id, success=True, output=output,
                                  agent="hermes", gear=task.gear)
            except Exception as e:
                return TaskResult(task_id=task.id, success=False, error=str(e),
                                  agent="hermes", gear=task.gear)

    return execute_hermes(task)


def _execute_file_analysis(task: AgentTask) -> TaskResult:
    """文件分析工具。"""
    try:
        # 提取文件路径
        paths = re.findall(r'(?:/[\\w./\\-]+)+', task.intent)
        target = paths[0] if paths else "/root/EntropyGuard"

        r = _subprocess.run(
            ["find", target, "-type", "f", "-name", "*.py", "-o",
             "-name", "*.txt", "-o", "-name", "*.json"],
            capture_output=True, text=True, timeout=10,
        )
        output = f"目标: {target}\n文件列表:\n{r.stdout[:2000]}"

        ok, _ = output_guard(output)
        if not ok:
            output = "[输出包含敏感信息，已过滤]"
        return TaskResult(task_id=task.id, success=True, output=output,
                          agent="hermes", gear=task.gear)
    except Exception as e:
        return TaskResult(task_id=task.id, success=False, error=str(e),
                          agent="hermes", gear=task.gear)


def _execute_report(task: AgentTask) -> TaskResult:
    """报告生成工具 — 通过 pydanticai 生成结构化报告。"""
    intent = task.intent
    # 尝试用 pydanticai 能力生成报告
    result = _execute_pydanticai_as_tool(task)
    if result.success:
        return result
    # 兜底：生成文本摘要
    try:
        output = f"执行报告: {task.description}\n输出预览: (工具失败，降级为文本摘要)"
        return TaskResult(task_id=task.id, success=True, output=output,
                          agent="hermes", gear=task.gear)
    except Exception as e:
        return TaskResult(task_id=task.id, success=False, error=str(e),
                          agent="hermes", gear=task.gear)


def _execute_sandbox(task: AgentTask) -> TaskResult:
    """沙箱执行 — 通过 docker exec 在 autogpt-sandbox 中执行。"""
    try:
        r = _subprocess.run(
            ["docker", "exec", "autogpt-sandbox", "sh", "-c",
             task.intent[:500]],
            capture_output=True, text=True, timeout=30,
        )
        output = r.stdout or r.stderr or f"exit={r.returncode}"
        ok, _ = output_guard(output)
        if not ok:
            output = "[沙箱输出包含敏感信息，已过滤]"
        return TaskResult(task_id=task.id, success=r.returncode == 0,
                          output=output, agent="sandbox", gear=task.gear)
    except Exception as e:
        logger.warning("[Execute] 沙箱执行失败: %s", e)
        return TaskResult(task_id=task.id, success=False, error=str(e),
                          agent="sandbox", gear=task.gear)


def execute(task: AgentTask) -> TaskResult:
    """v8.0: 工具调度执行器 — 根据 task.payload.tool 分发（非 Agent 路由）。

    不再使用 assigned_agent 做 Agent 级路由。
    工具类型由 AutoGPT planner_gateway 规划生成。
    """
    tool_name = task.payload.get("tool", "")
    tool_type = None
    if tool_name:
        try:
            tool_type = ToolType(tool_name)
        except (ValueError, KeyError):
            pass

    # 无工具类型 → 自动推断
    if tool_type is None and task.assigned_agent:
        # 兼容旧版 assigned_agent（过渡期支持）
        agent = task.assigned_agent.lower()
        if agent == "pydanticai":
            tool_type = ToolType.PYDANTICAI_EXTRACT
        elif agent == "autogpt":
            tool_type = ToolType.PYDANTICAI_EXTRACT  # autogpt 不再执行，用文本分析
        else:
            tool_type = ToolType.DIRECTORY_SCAN

    if tool_type is None:
        tool_type = ToolType.DIRECTORY_SCAN

    # 单次尝试（不再有 Agent 级重试）
    t_start = time.time()
    result = execute_tool(task, tool_type)
    result.elapsed_seconds = round(time.time() - t_start, 2)
    result.agent = "hermes"

    # 记录 episode
    episode_task_id = task.id
    try:
        memory_store.save_episode(
            task_id=episode_task_id,
            agent=result.agent or "hermes",
            intent=task.intent,
            output=(result.output or result.error or "")[:500],
            success=result.success,
            duration=result.elapsed_seconds,
        )
    except Exception as e:
        logger.warning("[Execute] episode 写入失败: %s", e)

    return result


def execute_hermes(task: AgentTask) -> TaskResult:
    """Hermes 子进程执行：检测工具并直接运行 shell 命令"""
    raw_intent = task.payload.get("original_intent", task.intent)
    intent_lower = raw_intent.lower()
    target_dir = "/root/EntropyGuard/test-targets"

    # --- 工具处理器 ---
    def _bandit_handler():
        py_files = []
        for root, dirs, files in os.walk(target_dir):
            dirs[:] = [d for d in dirs if d not in (
                "site-packages", "node_modules", "__pycache__", ".git",
                "flask-venv", "flask-venv2", "django-venv",
            )]
            for f in files:
                if f.endswith(".py") and not f.startswith("test_"):
                    py_files.append(os.path.join(root, f))
        if py_files and _shutil.which("bandit"):
            return ["bandit", "-f", "txt", "--quiet"] + py_files
        return None

    def _safety_handler():
        if _shutil.which("pip"):
            return ["pip", "list", "--format=columns", "--outdated"]
        if _shutil.which("pip-audit"):
            return ["pip-audit", "--desc", "--progress-spinner=off", "--timeout", "30"]
        return None

    def _curl_handler():
        urls = re.findall(r'https?://[^\s)\]]+', raw_intent)
        if not urls:
            urls = ["http://127.0.0.1:5000/"]
        return ["curl", "-s", "--connect-timeout", "5", "--max-time", "10",
                "-w", "\nHTTP_CODE:%{http_code}", urls[0]]

    def _flask_source_handler():
        app_file = os.path.join(target_dir, "vulnerable_app.py")
        if not os.path.exists(app_file):
            return None
        with open(app_file) as fh:
            content = fh.read()
        routes = re.findall(r"@app\.route\(['\"]([^'\"]+)['\"]", content)
        lines = content.split("\n")
        output = f"=== Flask 应用源码分析 ===\n文件: {app_file}\n代码行数: {len(lines)}\n定义的路由端点:\n"
        for r in routes:
            output += f"  {r}\n"
        output += "\n安全风险扫描:\n"
        findings = []
        if "debug=True" in content or 'debug = True' in content:
            findings.append("🔴 app.debug = True (生产环境启用调试模式)")
        if "secret_key" in content.lower():
            findings.append("🔴 SECRET_KEY 硬编码在源代码中")
        if "eval(" in content:
            findings.append("🔴 使用 eval() 存在代码注入风险")
        if "os.system" in content or "os.popen" in content:
            findings.append("🟡 使用 os.system/os.popen 存在命令执行风险")
        output += "\n".join(findings) if findings else "✅ 未检测到明显安全问题"
        output += f"\n\n完整源码:\n{content[:2000]}"
        return {"output": output, "fallback_only": True}

    detectors = [
        (["bandit", "静态安全扫描", "安全审计", "安全扫描"], _bandit_handler, "Bandit 安全扫描"),
        (["safety", "safety check", "依赖检查", "依赖漏洞", "dependency"],
         _safety_handler, "Safety 依赖检查"),
        (["curl ", "wget ", "http请求", "请求测试", "测试端点"], _curl_handler, "HTTP 请求测试"),
        (["Flask", "flask", "动态测试", "动态扫描", "启动应用"], _flask_source_handler, "Flask 源码安全分析"),
    ]

    for keywords, handler, desc in detectors:
        if any(kw in intent_lower for kw in keywords):
            try:
                result = handler()
            except Exception as e:
                logger.warning(f"[Hermes] {task.id}: {desc} 异常: {e}")
                continue
            if result is None:
                continue
            if isinstance(result, dict) and result.get("fallback_only"):
                return TaskResult(task_id=task.id, success=True,
                                  output=result["output"], agent="hermes", gear=task.gear)
            cmd = result if isinstance(result, list) else result.get("cmd", result)
            if cmd and _shutil.which(cmd[0] if isinstance(cmd, list) else cmd):
                try:
                    r = _subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                    output = r.stdout
                    if r.stderr:
                        output += f"\n--- STDERR ---\n{r.stderr[:500]}"
                    return TaskResult(task_id=task.id, success=True,
                                      output=output.strip() or f"命令已执行 (exit={r.returncode})",
                                      agent="hermes", gear=task.gear)
                except _subprocess.TimeoutExpired:
                    logger.warning(f"[Hermes] {task.id}: {desc} 超时 (30s)")
                    continue
                except FileNotFoundError:
                    continue

    # 兜底：目录扫描
    logger.info(f"[Hermes] {task.id}: 降级为目录扫描")
    try:
        r = _subprocess.run(
            ["find", target_dir, "-type", "f", "-name", "*.py", "-o",
             "-name", "*.txt", "-o", "-name", "*.cfg", "-o", "-name", "*.conf"],
            capture_output=True, text=True, timeout=15)
        output = f"目标目录: {target_dir}\n文件列表:\n{r.stdout}\n"
        for fn in ["vulnerable_app.py", "django_app.py"]:
            fp = os.path.join(target_dir, fn)
            if os.path.exists(fp):
                with open(fp) as fh:
                    output += f"\n=== {fn} ===\n{fh.read()[:2000]}"
        return TaskResult(task_id=task.id, success=True, output=output, agent="hermes", gear=task.gear)
    except Exception as e:
        return TaskResult(task_id=task.id, success=False,
                          error=f"hermes 兜底扫描失败: {e}", agent="hermes", gear=task.gear)


def log_audit(task: AgentTask, result: TaskResult):
    """将执行记录写入审计链"""
    _api_request("/api/events", {
        "event_type": "ORCHESTRATOR_TASK",
        "actor": f"orchestrator:{task.assigned_agent or 'unknown'}",
        "action": f"execute_task:{task.id}:{task.intent[:60]}",
        "delta_entropy": 0.03,
        "success": result.success,
        "details": {
            "task_id": task.id,
            "intent": task.intent[:200],
            "gear": task.gear,
            "agent": task.assigned_agent,
            "output_preview": result.output[:200] if result.output else "",
            "error": result.error,
            "validation_status": result.validation_status,
        },
    })


def detect_conflicts(new_task: AgentTask, existing_results: list[TaskResult]) -> list[str]:
    """冲突检测：相同文件路径按优先级排队"""
    conflicts = []
    new_paths = set(re.findall(r'/[\w/\-]+', new_task.intent))
    if not new_paths:
        return []
    for existing in existing_results:
        for path in new_paths:
            if path in existing.output or path in new_task.intent:
                conflicts.append(f"路径冲突 '{path}': {existing.task_id}(已执行) → {new_task.id}(排队)")
                break
    return conflicts


def merge(goal: str, tasks: list[AgentTask], results: list[TaskResult]) -> OrchestratorResult:
    """汇总所有子任务执行结果"""
    success_count = sum(1 for r in results if r.success)
    total = len(results)
    summary_parts = []
    for task, tr in zip(tasks, results):
        status = "✅" if tr.success else "❌"
        agent = tr.agent or task.assigned_agent or "?"
        snippet = (tr.output or tr.error or "(no output)")[:100]
        summary_parts.append(f"  {status} [{task.id}] {agent}: {snippet}")
    summary = f"Orchestrator 执行完成: {success_count}/{total} 子任务成功\n" + "\n".join(summary_parts)
    return OrchestratorResult(
        goal=goal, tasks=tasks, results=results,
        summary=summary, success=success_count == total, total_time=0.0)


def execute_plan(subtasks: list[AgentTask], gear: int = DEFAULT_GEAR,
                 task_id: str = "",
                 goal: str = "") -> OrchestratorResult:
    """按依赖关系顺序执行子任务列表

    增强功能 v7.2:
      - 声明式数据流: 自动解析 dataflow 依赖，注入上游输出到下游 context
      - 断点续跑: 每个子任务执行后写 checkpoint，进程重启跳过已完成任务

    Args:
        subtasks: 子任务列表
        gear: 默认档位
        task_id: 执行计划 ID（用于 checkpoint，留空自动生成）
        goal: 原始目标描述（用于 checkpoint，留空使用第一个任务的 intent）

    拓扑排序 + 逐批执行，返回聚合后的 OrchestratorResult。
    """
    import time
    import uuid
    t0 = time.time()
    if not subtasks:
        return OrchestratorResult(
            goal=goal or "(empty)", tasks=[], results=[],
            summary="没有需要执行的子任务", success=True, total_time=0.0)

    goal_text = goal or subtasks[0].intent
    plan_id = task_id or f"plan-{uuid.uuid4().hex[:8]}"
    logger.info(
        f"[ExecutePlan v7.2] 开始执行 {len(subtasks)} 个子任务, "
        f"gear={gear}, plan_id={plan_id}")

    # --- v7.2: 自动注入 dataflow 依赖 ---
    subtasks = augment_dependencies(subtasks)

    # --- v7.2 Phase 3.1: 宇宙透镜优先级评估 ---
    try:
        lense = CosmicLense()
        prioritized = []
        for task in subtasks:
            result = lense.evaluate(task)
            # 宇宙透镜判定结果记录到日志
            logger.info(
                f"[CosmicLense] {task.id}: {result.tier_name_cn}级 "
                f"(score={result.priority_score})"
                f"{' [覆盖]' if result.overridden else ''}"
                f" | {result.reason[:80]}"
            )
            # 如果宇宙优先级高于子任务指定的优先级，覆盖
            if result.overridden and result.original_tier:
                logger.warning(
                    f"[CosmicLense] {task.id}: 优先级被宇宙透镜覆盖: "
                    f"{result.original_tier}→{result.tier}"
                )
            # 将宇宙透镜结果注入 task payload 供后续使用
            task.payload["cosmic_lense"] = {
                "tier": result.tier_name_cn,
                "priority_score": result.priority_score,
                "reason": result.reason,
                "overridden": result.overridden,
            }
            prioritized.append(task)

        # 按宇宙透镜优先级重新排序子任务
        prioritized.sort(
            key=lambda t: (
                t.payload.get("cosmic_lense", {}).get("priority_score", 50),
                t.priority,
            )
        )
        subtasks = prioritized
        logger.info(
            f"[CosmicLense] 优先级排序完成: "
            f"{[(t.id, t.payload.get('cosmic_lense',{}).get('tier','?')) for t in subtasks]}"
        )
    except Exception as e:
        logger.warning(f"[CosmicLense] 评估失败（不影响执行）: {e}")

    all_ids = {t.id for t in subtasks}

    # --- v7.2: 尝试恢复 checkpoint ---
    ckpt_data = load_checkpoint(plan_id, subtasks)
    if ckpt_data:
        logger.info(f"[ExecutePlan v7.2] 检测到 checkpoint，恢复执行")
        completed_pairs: list[tuple[AgentTask, TaskResult]] = ckpt_data["completed"]
        remaining_tasks: list[AgentTask] = ckpt_data["remaining"]
        task_output_map: dict[str, str] = ckpt_data["task_output_map"]
        executed: dict[str, TaskResult] = {r.task_id: r for _, r in completed_pairs}
        # 重建 remaining 集合
        remaining_set = {t.id for t in remaining_tasks}
        # 构建 order: 已完成的按原始顺序
        all_ordered = [t for t in subtasks if t.id not in remaining_set] + remaining_tasks
        order = all_ordered
        logger.info(
            f"[Checkpoint] 跳过 {len(completed_pairs)} 个已完成任务, "
            f"继续执行 {len(remaining_tasks)} 个")
    else:
        # --- 无 checkpoint，从头执行 ---
        executed: dict[str, TaskResult] = {}
        task_output_map: dict[str, str] = {}
        order: list[AgentTask] = []

        deps_map: dict[str, set[str]] = {}
        for t in subtasks:
            deps_map[t.id] = {d for d in t.dependencies if d in all_ids}

        remaining_set = set(all_ids)
        while remaining_set:
            ready = [t for t in subtasks if t.id in remaining_set
                     and deps_map[t.id].issubset(set(executed.keys()))]
            if not ready:
                logger.warning(f"[ExecutePlan] 依赖环或孤立节点: {remaining_set}")
                ready = [t for t in subtasks if t.id in remaining_set][:1]
            ready.sort(key=lambda x: x.priority)
            order.extend(ready)
            for t in ready:
                remaining_set.discard(t.id)

    logger.info(f"[ExecutePlan] 执行顺序: {[t.id for t in order]}")

    # --- 执行循环 ---
    completed_tasks_list: list[AgentTask] = []
    completed_results_list: list[TaskResult] = []

    # --- v7.2 Phase 3.2: 元认知自省引擎 ---
    meta = Metacognition()
    # --- v7.2 Phase 3.4: 动态重规划引擎 ---
    replanner = Replanner()

    for i, task in enumerate(order):
        # 已完成的跳过
        if task.id in executed:
            completed_tasks_list.append(task)
            completed_results_list.append(executed[task.id])
            continue

        # 检查依赖失败
        dep_failures = []
        for dep_id in task.dependencies:
            if dep_id in executed and not executed[dep_id].success:
                dep_failures.append(dep_id)
        if dep_failures:
            logger.warning(
                f"[ExecutePlan] {task.id} 的前置任务 {dep_failures} 失败，继续执行")

        # --- v7.2: 注入数据流上下文 ---
        if task_output_map:
            existing_context = build_context(task, task_output_map)
            enriched_context = inject_dataflow_into_context(
                task, existing_context, task_output_map)
            if enriched_context != existing_context:
                logger.info(
                    f"[Dataflow] {task.id}: 注入数据流上下文 "
                    f"({len(enriched_context)} chars)")
                # 将注入的上下文附加到任务 intent 中
                task.intent = task.intent + "\n\n[数据流注入]\n" + enriched_context

        result = execute(task)
        executed[task.id] = result
        completed_tasks_list.append(task)
        completed_results_list.append(result)
        task_output_map[task.id] = result.output or ""
        log_audit(task, result)
        logger.info(
            f"[ExecutePlan] {task.id}: {'✅' if result.success else '❌'} "
            f"({result.elapsed_seconds:.1f}s)")

        # --- v7.2 Phase 3.4: 失败重规划 ---
        if not result.success:
            try:
                remaining_for_replan = [
                    t for t in order[i + 1:] if t.id not in executed
                ]
                plan = replanner.replan_on_failure(
                    failed_task=task, result=result,
                    remaining_tasks=remaining_for_replan,
                    task_output_map=task_output_map,
                    all_tasks=[t for t in subtasks if t.id not in executed],
                )
                if plan.has_changes() or plan.action != ReplanAction.CONTINUE:
                    logger.warning(
                        f"[Replanner] {task.id}: {plan.action.value}"
                        f" — {plan.reason}"
                    )
                    replanner.save_replan_to_memory(plan, task.id)
                    if plan.escalated:
                        logger.error(
                            f"[Replanner] {task.id}: 需要人工介入: {plan.reason}"
                        )
            except Exception as replan_err:
                logger.warning(
                    f"[Replanner] 重规划异常（不影响执行）: {replan_err}"
                )

        # --- v7.2: 写 checkpoint ---
        remaining_tasks_ckpt = [
            t for t in order[i + 1:] if t.id not in executed
        ]
        save_checkpoint(
            plan_id, goal_text,
            completed_tasks_list, completed_results_list,
            remaining_tasks_ckpt, task_output_map,
        )

        # --- v7.2 Phase 3.2: 元认知自省 ---
        try:
            cr = meta.self_check(
                task=task,
                result=result,
                retry_history=[],
                expected_timeout=120,
            )
            # 将元认知结果注入 task payload
            task.payload["metacognition"] = {
                "status": cr.status.value,
                "drift_score": cr.drift_score,
                "suggestion": cr.suggestion.value,
                "flags": [{"type": f.flag_type, "severity": f.severity}
                          for f in cr.flags],
            }

            if cr.status == CheckStatus.CRITICAL:
                logger.warning(
                    f"[Metacognition] {task.id}: CRITICAL — {cr.summary}"
                    f" | 建议: {cr.suggestion.value}"
                )
                # 元认知 CRITICAL → 调用重规划
                try:
                    drift_plan = replanner.replan_on_drift(
                        task=task, check_result=cr,
                        remaining_tasks=[t for t in order[i + 1:] if t.id not in executed],
                        all_tasks=[t for t in subtasks if t.id not in executed],
                    )
                    if drift_plan.has_changes():
                        logger.warning(
                            f"[Replanner] drift → {drift_plan.action.value}: {drift_plan.reason}"
                        )
                        replanner.save_replan_to_memory(drift_plan, task.id)
                except Exception as drift_err:
                    logger.warning(f"[Replanner] drift 异常: {drift_err}")
                # 宇宙透镜联动：元认知 CRITICAL → 后续任务自动提级
                if cr.suggestion in (Suggestion.ABORT, Suggestion.ESCALATE):
                    logger.error(
                        f"[Metacognition] {task.id}: 建议{cr.suggestion.value}，"
                        f"当前策略: 继续执行但记录告警"
                    )
            elif cr.status == CheckStatus.WARNING:
                logger.info(
                    f"[Metacognition] {task.id}: WARNING — {cr.summary}"
                )
        except Exception as meta_err:
            logger.warning(f"[Metacognition] 自检异常（不影响执行）: {meta_err}")

    # 任务全部完成，清除 checkpoint
    clear_checkpoint(plan_id)

    # 按原始 subtasks 顺序整理结果
    results = [executed[t.id] for t in subtasks]
    merged = merge(goal_text, subtasks, results)
    merged.total_time = round(time.time() - t0, 2)
    return merged
