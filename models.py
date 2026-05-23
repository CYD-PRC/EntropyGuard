"""
EntropyGuard · 模型层
模型注册表 + GEAR_PROMPTS + gear_aware_call + 升级申请解析
"""
import os
import re
import json
import hashlib
import logging
import httpx
from datetime import datetime

from config import Config, GEAR_MAP
from audit import state
from tools import TOOL_DEFINITIONS, GEAR_TOOLS, dispatch_tool

logger = logging.getLogger("entropyguard")


# ========== 模型注册表 ==========

MODEL_REGISTRY = {
    "deepseek": {
        "name": "DeepSeek Chat",
        "url": "https://api.deepseek.com/v1/chat/completions",
        "model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
    },
    "qwen": {
        "name": "百炼 Qwen-Turbo",
        "url": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
        "model": "qwen-turbo",
        "key_env": "BAILIAN_API_KEY",
    },
    "kimi": {
        "name": "Kimi",
        "url": "https://api.moonshot.cn/v1/chat/completions",
        "model": "moonshot-v1-8k",
        "key_env": "KIMI_API_KEY",
    },
    "doubao": {
        "name": "豆包 (Volcano)",
        "url": "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
        "model": "ep-20260517223811-pj7z9",
        "key_env": "VOLC_API_KEY",
    },
    "mimo": {
        "name": "MiMo-v2.5 (Xiaomi)",
        "url": "https://api.xiaomimomo.com/v1/chat/completions",
        "model": "mimo-v2.5",
        "key_env": "MIMO_API_KEY",
        "host": "api.xiaomimimo.com",
        "api_key": "sk-ce1jfzaj15uffb0po58wpmcp64s060dt8zm2iu2bwb5z354n",
    },
}


# ========== 档位提示词 ==========
# [Security] GEAR_PROMPTS 已移至 /etc/entropyguard/gear_prompts.py，启动时 SHA-256 校验

import importlib.util as _importlib_util


def _load_gear_prompts():
    """加载 GEAR_PROMPTS（完整性校验由 systemd ExecStartPre 的 entropyguard-verify.py 保证）"""
    prompts_path = "/etc/entropyguard/gear_prompts.py"

    if not os.path.exists(prompts_path):
        logger.error("GEAR_PROMPTS not found at %s", prompts_path)
        raise SystemExit("Security: GEAR_PROMPTS file missing")

    # 动态加载模块
    spec = _importlib_util.spec_from_file_location("gear_prompts", prompts_path)
    module = _importlib_util.module_from_spec(spec)
    spec.loader.exec_module(module)

    logger.info("GEAR_PROMPTS loaded from %s", prompts_path)
    return module.GEAR_PROMPTS


GEAR_PROMPTS = _load_gear_prompts()


# ========== 异步 HTTP 客户端 ==========

_http_client = httpx.AsyncClient(timeout=60.0, verify=False)


# ========== 核心调用函数 ==========

async def gear_aware_call(
    model_id: str, message: str, gear: int,
    upgrade_retry: bool = False, memory_context: str = None,
    actor: str = "human",
) -> dict:
    """通用档位感知模型调用，支持 Function Calling"""
    if model_id not in MODEL_REGISTRY:
        return {"success": False, "error": f"不支持的模型：{model_id}"}

    model_info = MODEL_REGISTRY[model_id]
    api_key = model_info.get("api_key", "") or os.environ.get(model_info.get("key_env", ""), "")
    if model_info["key_env"] and not api_key:
        return {"success": False, "error": f"{model_info['name']} API Key 未配置"}

    gear_name = GEAR_MAP[gear]["name"]
    system_prompt = GEAR_PROMPTS.get(gear, GEAR_PROMPTS[1])
    system_prompt += f"\n\n当前系统状态：档位 {gear} ({gear_name})，控制熵 {state.control_entropy:.4f}。"

    if memory_context:
        system_prompt += f"\n\n{memory_context}"

    system_prompt += Config.SERVER_CONTEXT

    if upgrade_retry:
        gear_signoff = {
            1: "\n\n[签收确认] 我已理解用户将我的权限调整至EMBRACE档位。",
            2: "\n\n[签收确认] 我已理解用户将我的权限调整至EXPLORE档位。",
            3: "\n\n[签收确认] 我已理解用户将我的权限提升至ADAPT档位。",
            4: "\n\n[签收确认] 我已理解用户将我的权限提升至LET_GO档位。",
        }
        system_prompt += gear_signoff.get(gear, "")
        system_prompt += "\n\n不要再申请升级，直接执行任务。"

    allowed_tool_names = GEAR_TOOLS.get(gear, [])
    allowed_tools = [t for t in TOOL_DEFINITIONS if t["function"]["name"] in allowed_tool_names]

    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": message},
    ]

    tool_calls_log = []
    consecutive_tool_only_rounds = 0
    MAX_ROUNDS = (
        Config.MAX_TOOL_ROUNDS_GEAR4 if gear == 4
        else Config.MAX_TOOL_ROUNDS_GEAR3 if gear == 3
        else Config.MAX_TOOL_ROUNDS_DEFAULT
    )

    try:
        for round_i in range(MAX_ROUNDS):
            payload = {
                "model": model_info["model"],
                "messages": messages,
                "temperature": 0.7,
                "max_tokens": 4096,
            }
            supports_fc = ["deepseek", "qwen", "kimi", "kimi-k2", "doubao", "mimo"]
            if allowed_tools and any(model_id.startswith(m) for m in supports_fc):
                payload["tools"] = allowed_tools
                if model_id not in ["kimi", "kimi-k2", "kimi-k2.5", "kimi-k2.6", "doubao", "mimo"]:
                    payload["tool_choice"] = "auto"

            resp = await _http_client.post(model_info["url"], headers=headers, json=payload, timeout=60)
            if resp.status_code != 200:
                return {"success": False, "error": f"API 请求失败 (HTTP {resp.status_code}): {resp.text[:200]}"}

            result = resp.json()
            if "choices" in result:
                msg = result["choices"][0].get("message", {})
            else:
                return {"success": False, "error": f"API 返回格式异常: {list(result.keys())[:5]}"}

            if not msg.get("tool_calls"):
                return {
                    "success": True, "reply": msg.get("content", ""),
                    "model": model_info["name"], "gear": gear, "gear_name": gear_name,
                    "tool_calls": tool_calls_log or None,
                }

            # AI 回复只有 tool_calls 没有 content → 计数递增
            if not msg.get("content"):
                consecutive_tool_only_rounds += 1
            else:
                consecutive_tool_only_rounds = 0

            messages.append(msg)

            # 硬断路器：ADAPT 档位最多 10 次工具调用
            if gear == 3 and len(tool_calls_log) >= 10:
                return {
                    "success": True,
                    "reply": "[系统] 工具调用已达上限（ADAPT: 10次），请用当前结果回复用户。",
                    "model": model_info["name"], "gear": gear, "gear_name": gear_name,
                    "tool_calls": tool_calls_log,
                }

            # LET_GO 档位最多 15 次工具调用
            if gear == 4 and len(tool_calls_log) >= 15:
                return {
                    "success": True,
                    "reply": "[系统] 工具调用已达上限（LET_GO: 15次），请用当前结果回复用户。",
                    "model": model_info["name"], "gear": gear, "gear_name": gear_name,
                    "tool_calls": tool_calls_log,
                }

            for tc in msg["tool_calls"]:
                fn_name = tc["function"]["name"]
                try:
                    fn_args = json.loads(tc["function"]["arguments"])
                except Exception:
                    fn_args = {}

                # 单次回复最多执行 3 个工具调用
                same_round_count = len([x for x in tool_calls_log if x.get("round") == round_i])
                if same_round_count >= 3:
                    tool_result = {"success": False, "error": "[系统] 单次回复最多执行 3 个工具调用，请先用当前结果回复用户。"}
                    tool_calls_log.append({
                        "tool": fn_name, "arguments": fn_args,
                        "result_preview": str(tool_result)[:150],
                        "round": round_i,
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": json.dumps(tool_result, ensure_ascii=False, default=str),
                    })
                    continue

                if fn_name not in allowed_tool_names:
                    tool_result = {"success": False, "error": f"工具 {fn_name} 在 {gear_name} 档位不可用"}
                else:
                    # Auto-inject model, gear and actor for write_board
                    if fn_name == "write_board":
                        fn_args.setdefault("model", model_id)
                        fn_args.setdefault("gear", gear)
                        fn_args.setdefault("actor", actor)
                    tool_result = await dispatch_tool(fn_name, fn_args)

                tool_calls_log.append({
                    "tool": fn_name, "arguments": fn_args,
                    "result_preview": str(tool_result)[:150],
                    "round": round_i,
                })

                # 记录到审计日志（原始结果，不含 _meta）
                _log_tool_event(fn_name, fn_args, tool_result, gear, gear_name, actor)

                # 构建传给 AI 的 tool result（可追加 _meta 警告）
                tool_result_for_ai = dict(tool_result)

                # A. 剩余轮次提醒
                remaining = MAX_ROUNDS - round_i
                if remaining <= 5:
                    tool_result_for_ai["_meta"] = {
                        "warning": f"工具调用轮次即将耗尽（剩余 {remaining} 轮）。请尽快完成任务。"
                    }

                # B. 连续无文字回复提醒
                if consecutive_tool_only_rounds >= 10:
                    meta = tool_result_for_ai.setdefault("_meta", {})
                    meta["warning"] = (
                        "你已连续调用工具 10+ 次未给出文字回复。请先汇报当前进度，再决定是否继续。"
                    )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(tool_result_for_ai, ensure_ascii=False, default=str),
                })

        return {
            "success": True, "reply": "[工具调用超过最大轮次，已停止]",
            "model": model_info["name"], "gear": gear, "gear_name": gear_name,
            "tool_calls": tool_calls_log,
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def _log_tool_event(fn_name: str, fn_args: dict, tool_result: dict, gear: int, gear_name: str, actor: str = "human"):
    """记录工具调用事件到审计链"""
    tool_event = {
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "actor": actor,
        "old_gear": gear, "new_gear": gear, "gear_name": gear_name,
        "entropy_previous": round(state.control_entropy, 6),
        "entropy_new": round(state.control_entropy + Config.TOOL_ENTROPY_INCREASE, 6),
        "entropy_delta": Config.TOOL_ENTROPY_INCREASE,
        "adjustment": Config.TOOL_ENTROPY_INCREASE,
        "direction": "tool", "landauer_cost_joules": 0.0,
        "event_type": "TOOL_CALL",
        "action": f"CALL {fn_name}",
        "trigger_chain": ["user_message", f"gear_{gear}", f"TOOL_CALL({fn_name})"],
        "tool_name": fn_name, "tool_arguments": fn_args,
        "tool_result_preview": str(tool_result)[:150],
    }
    # [Bug 1 fix] 使用线程安全的 append_event
    state.append_event(tool_event)
    state.control_entropy += Config.TOOL_ENTROPY_INCREASE


def parse_upgrade_request(reply: str) -> dict:
    """解析回复中的升级申请标签"""
    pattern = r'\[UPGRADE_REQUEST\](.*?)\[/UPGRADE_REQUEST\]'
    match = re.search(pattern, reply, re.DOTALL)
    if not match:
        return None

    content = match.group(1).strip()
    parsed = {}
    for line in content.split('\n'):
        if '=' in line:
            key, value = line.split('=', 1)
            parsed[key.strip()] = value.strip()

    if 'target_gear' not in parsed:
        return None

    try:
        target_gear = int(parsed['target_gear'])
    except ValueError:
        gear_map_name = {'EMBRACE': 1, 'EXPLORE': 2, 'ADAPT': 3, 'LET_GO': 4}
        target_gear = gear_map_name.get(parsed['target_gear'].upper(), 2)

    gear_name_map = {1: 'EMBRACE', 2: 'EXPLORE', 3: 'ADAPT', 4: 'LET_GO'}
    return {
        'target_gear': target_gear,
        'target_gear_name': gear_name_map.get(target_gear, 'UNKNOWN'),
        'reason': parsed.get('reason', '未说明原因'),
        'risk_level': parsed.get('risk_level', 'unknown'),
    }
