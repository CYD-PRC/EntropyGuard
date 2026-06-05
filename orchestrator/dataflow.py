"""Entropy Runtime Orchestrator · 声明式数据流
v7.2: 子任务输入输出依赖解析 + 自动数据注入。

声明式语法示例（在 AgentTask.payload 中）:
```json
{
  "dataflow": {
    "target_path": "task-explore.output.files[0]",
    "api_response": "task-scan.output.raw_text",
    "config": "task-setup.output.parsed_json"
  }
}
```

parser: 解析 dataflow 声明，构建依赖图
injector: 将上游输出注入到下游任务的 intent/context 中
"""
import json
import logging
import re
from typing import Any, Optional

from orchestrator.task_model import AgentTask, TaskResult

logger = logging.getLogger("entropyruntime.dataflow")

# ========== 解析器 ==========

DATAFLOW_PATTERN = re.compile(
    r'(?P<task_id>[\w-]+)\.output(?:\.(?P<field>[\w\[\]]+))?'
)


def parse_dataflow_declaration(task: AgentTask) -> dict[str, tuple[str, Optional[str]]]:
    """解析子任务 payload 中的 dataflow 声明。

    Returns:
        dict[str, (source_task_id, field_path)]
        如 {"target_path": ("task-explore", "files[0]")}
    """
    raw = task.payload.get("dataflow", {})
    if not raw or not isinstance(raw, dict):
        return {}

    resolved: dict[str, tuple[str, Optional[str]]] = {}
    for input_key, declaration in raw.items():
        if not isinstance(declaration, str):
            continue
        m = DATAFLOW_PATTERN.match(declaration)
        if m:
            task_id = m.group("task_id")
            field = m.group("field")  # may be None
            resolved[input_key] = (task_id, field)
        else:
            logger.warning(
                "[Dataflow] 无法解析声明 '%s': %s", input_key, declaration
            )
    return resolved


def resolve_dataflow_dependencies(
    tasks: list[AgentTask],
) -> dict[str, set[str]]:
    """从所有任务的 dataflow 声明中提取隐式依赖关系。

    返回: {source_task_id: {dependent_task_id, ...}}
    """
    deps: dict[str, set[str]] = {}
    for task in tasks:
        flow = parse_dataflow_declaration(task)
        for _input_key, (source_id, _field) in flow.items():
            if source_id not in deps:
                deps[source_id] = set()
            deps[source_id].add(task.id)
            # 同时确保依赖任务已知
    return deps


def augment_dependencies(
    tasks: list[AgentTask],
) -> list[AgentTask]:
    """将 dataflow 声明的依赖自动注入到 task.dependencies 中。

    如果 tasks[1] 声明了 dataflow: {x: "task-001.output.foo"},
    且 task-001 不在 tasks[1].dependencies 中，则自动追加。
    """
    all_ids = {t.id for t in tasks}
    for task in tasks:
        flow = parse_dataflow_declaration(task)
        for _input_key, (source_id, _field) in flow.items():
            if source_id in all_ids and source_id not in task.dependencies:
                task.dependencies.append(source_id)
                logger.info(
                    "[Dataflow] 自动注入依赖 %s → %s (dataflow)", source_id, task.id
                )
    return tasks


# ========== 注入器 ==========


def _extract_field(data: str, field_path: Optional[str]) -> str:
    """从文本输出中提取指定字段。

    支持的路径:
      - None / ""         → 返回完整文本
      - "files[0]"        → 从 JSON 中提取 files 数组的第 0 项
      - "parsed_json"     → 尝试解析 JSON 并提取 key
    """
    if not field_path:
        return data

    # 尝试解析为 JSON
    try:
        obj = json.loads(data)
    except (json.JSONDecodeError, TypeError):
        pass
    else:
        return _navigate_json(obj, field_path)

    # 尝试行匹配: 查找 "key: value" 或 "key = value" 模式
    for line in data.splitlines():
        line_lower = line.lower().strip()
        key_lower = field_path.lower().strip().rstrip("[]").rstrip("0123456789")
        if line_lower.startswith(key_lower + ":") or line_lower.startswith(key_lower + "="):
            value = line.split(":", 1)[-1].split("=", 1)[-1].strip()
            return value

    # 兜底：返回包含关键字的行
    for line in data.splitlines():
        if field_path.lower() in line.lower():
            return line.strip()[:200]

    logger.warning("[Dataflow] 无法从输出中提取字段 '%s'", field_path)
    return data[:500]


def _navigate_json(obj: Any, field_path: str) -> str:
    """在 JSON 对象中导航字段路径，如 files[0] -> obj["files"][0]"""
    current = obj
    parts = field_path.replace("[", ".").replace("]", "").split(".")
    for part in parts:
        if not part:
            continue
        if isinstance(current, dict):
            current = current.get(part, "")
        elif isinstance(current, list):
            try:
                idx = int(part)
                current = current[idx] if idx < len(current) else ""
            except ValueError:
                current = ""
        else:
            current = ""
        if current is None:
            current = ""
    return str(current) if current else ""


def build_dataflow_context(
    task: AgentTask,
    task_output_map: dict[str, str],
) -> str:
    """为子任务构建数据流注入上下文。

    根据 task.payload.dataflow 声明，从 task_output_map 中提取
    上游任务的输出字段，拼接为注入上下文。
    """
    flow = parse_dataflow_declaration(task)
    if not flow:
        return ""

    parts = []
    for input_key, (source_id, field_path) in flow.items():
        source_output = task_output_map.get(source_id, "")
        if not source_output:
            logger.warning(
                "[Dataflow] %s 声明来自 %s，但该任务无输出", task.id, source_id
            )
            continue
        extracted = _extract_field(source_output, field_path)
        if extracted:
            parts.append(
                "[Dataflow Input: %s]\n%s\n[/Dataflow Input: %s]\n"
                % (input_key, extracted, input_key)
            )

    if parts:
        context = "\n".join(parts)
        logger.info(
            "[Dataflow] %s 注入 %d 个数据流字段", task.id, len(parts)
        )
        return context
    return ""


def inject_dataflow_into_context(
    task: AgentTask,
    existing_context: str,
    task_output_map: dict[str, str],
) -> str:
    """将数据流注入附加到已有的 context 后面。"""
    flow_context = build_dataflow_context(task, task_output_map)
    if not flow_context:
        return existing_context
    if existing_context:
        return existing_context + "\n\n" + flow_context
    return flow_context
