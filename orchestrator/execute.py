"""Entropy Runtime · 子任务执行模块
v5.0: 统一执行 + 重试机制 + Hermes 子进程 + 审计 + 合并。
"""
import json
import logging
import os
import re
import time
import urllib.request
import subprocess as _subprocess
import shutil as _shutil
from typing import Optional

from orchestrator.task_model import AgentTask, TaskResult, OrchestratorResult
from orchestrator.memory import write_episode
from notify.wechat import send_retry_notification

logger = logging.getLogger("entropyruntime.execute")

ENTROPY_API_BASE = "http://127.0.0.1:8000"
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
        return {"success": False, "error": str(e.reason)}
    except Exception as e:
        logger.error(f"[Execute] Request error on {endpoint}: {e}")
        return {"success": False, "error": str(e)}


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


def execute(task: AgentTask) -> TaskResult:
    """执行子任务，带自动重试和 Agent 降级（最多 3 次）"""
    retry_agents = [
        task.assigned_agent or "hermes",
        "hermes",
        "autogpt",
    ]
    retry_delays = [0, 2, 5]
    retry_history: list[dict] = []
    t_start = time.time()
    result: Optional[TaskResult] = None

    for attempt in range(3):
        current_agent = retry_agents[attempt]
        delay = retry_delays[attempt]
        if attempt > 0:
            logger.info(f"[Execute v5.0] {task.id}: 第{attempt+1}次重试, 等待{delay}s, Agent: {current_agent}")
            time.sleep(delay)

        t_attempt = time.time()
        if current_agent == "hermes":
            result = execute_hermes(task)
        else:
            timeout = 240 if current_agent == "autogpt" else 120
            payload = {
                "message": task.intent,
                "gear": task.gear,
                "model_id": task.model_id or DEFAULT_MODEL,
                "actor": f"orchestrator:{current_agent}",
                "session_id": f"orch-{task.id}-a{attempt+1}",
            }
            resp = _api_request("/api/chat", payload, timeout=timeout)
            success = resp.get("success", False)
            result = TaskResult(
                task_id=task.id, success=success,
                output=resp.get("reply", "") if success else "",
                tool_calls=resp.get("tool_calls", []) or [],
                error=resp.get("error") if not success else None,
                agent=current_agent, gear=task.gear,
                validation_status=resp.get("validation_status"),
            )

        result.elapsed_seconds = round(time.time() - t_attempt, 2)
        retry_history.append({
            "attempt": attempt + 1, "agent": current_agent,
            "success": result.success, "duration": result.elapsed_seconds,
            "error": result.error,
        })
        result.elapsed_seconds = round(time.time() - t_start, 2)

        if not result.success and attempt < 2:
            write_episode(result.task_id, current_agent, False,
                          duration=result.elapsed_seconds,
                          error=result.error or "未知错误",
                          retry_count=attempt + 1, retry_history=list(retry_history),
                          source=f"orchestrator:{current_agent}")
            send_retry_notification(
                task_id=task.id, agent=current_agent,
                retry_count=attempt + 1, error=result.error or "未知错误")

        if result.success:
            write_episode(result.task_id, current_agent, True,
                          output_preview=(result.output or "")[:200],
                          duration=result.elapsed_seconds,
                          retry_count=attempt, retry_history=list(retry_history),
                          source=f"orchestrator:{current_agent}")
            return result

    # 3 次全部失败
    assert result is not None
    write_episode(result.task_id, result.agent or "unknown", False,
                  duration=result.elapsed_seconds, error=result.error or "所有 Agent 均失败",
                  retry_count=3, retry_history=list(retry_history),
                  source=f"orchestrator:{result.agent or 'unknown'}")
    return result


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


def execute_plan(subtasks: list[AgentTask], gear: int = DEFAULT_GEAR) -> OrchestratorResult:
    """按依赖关系顺序执行子任务列表

    拓扑排序 + 逐批执行，返回聚合后的 OrchestratorResult。
    """
    import time
    t0 = time.time()
    if not subtasks:
        return OrchestratorResult(
            goal="(empty)", tasks=[], results=[],
            summary="没有需要执行的子任务", success=True, total_time=0.0)

    goal = subtasks[0].intent if subtasks else "(unknown)"
    logger.info(f"[ExecutePlan] 开始执行 {len(subtasks)} 个子任务, gear={gear}")

    # 构建依赖图: task_id -> set of dependency IDs
    all_ids = {t.id for t in subtasks}
    deps_map: dict[str, set[str]] = {}
    for t in subtasks:
        deps_map[t.id] = {d for d in t.dependencies if d in all_ids}

    executed: dict[str, TaskResult] = {}
    order: list[AgentTask] = []

    # 拓扑排序：每次取无未完成依赖的任务
    remaining = set(all_ids)
    while remaining:
        ready = [t for t in subtasks if t.id in remaining
                 and deps_map[t.id].issubset(set(executed.keys()))]
        if not ready:
            # 依赖环或孤立节点 — 按优先级执行剩余任务
            logger.warning(f"[ExecutePlan] 依赖环或孤立节点: {remaining}")
            ready = [t for t in subtasks if t.id in remaining][:1]
        ready.sort(key=lambda x: x.priority)  # 优先级高的先执行
        order.extend(ready)
        for t in ready:
            remaining.discard(t.id)

    logger.info(f"[ExecutePlan] 执行顺序: {[t.id for t in order]}")

    for task in order:
        # 检查依赖是否全部成功（非关键依赖不阻塞）
        dep_failures = []
        for dep_id in task.dependencies:
            if dep_id in executed and not executed[dep_id].success:
                dep_failures.append(dep_id)
        if dep_failures:
            logger.warning(
                f"[ExecutePlan] {task.id} 的前置任务 {dep_failures} 失败，继续执行")
            # 仍然继续，但记录警告

        result = execute(task)
        executed[task.id] = result
        log_audit(task, result)
        logger.info(
            f"[ExecutePlan] {task.id}: {'✅' if result.success else '❌'} "
            f"({result.elapsed_seconds:.1f}s)")

    results = [executed[t.id] for t in subtasks]
    merged = merge(goal, subtasks, results)
    merged.total_time = round(time.time() - t0, 2)
    return merged
