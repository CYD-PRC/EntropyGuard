"""
EntropyGuard · 工具层
工具定义 + 执行器 + MCP 集成
"""
import json
import subprocess
import requests
import logging

from config import Config

logger = logging.getLogger("entropyguard")


# ========== MCP Server 配置 ==========

MCP_SERVERS = {
    "filesystem": {
        "command": ["npx", "-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
        "description": "文件系统访问（仅限/tmp目录）"
    }
}


# ========== 工具定义（OpenAI Function Calling 格式） ==========

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "run_shell",
            "description": "在服务器上执行shell命令并返回输出。仅限非破坏性操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的shell命令"}
                },
                "required": ["command"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "http_request",
            "description": "发送HTTP请求到指定URL并返回响应。",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string", "enum": ["GET", "POST", "PUT", "DELETE"],
                        "description": "HTTP方法"
                    },
                    "url": {"type": "string", "description": "目标URL"},
                    "headers": {"type": "object", "description": "请求头（可选）"},
                    "body": {"type": "string", "description": "请求体（可选）"},
                },
                "required": ["method", "url"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "mcp_call",
            "description": "调用MCP工具。先用list_mcp_tools查看可用工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "MCP server名称"},
                    "tool_name": {"type": "string", "description": "MCP工具名"},
                    "arguments": {"type": "object", "description": "工具参数"},
                },
                "required": ["server", "tool_name", "arguments"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "read_board",
            "description": "读取多智能体留言板上的所有消息。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "write_board",
            "description": "在多智能体留言板上写一条消息，会被审计链记录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "消息内容"}
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_mcp_tools",
            "description": "列出MCP server的所有可用工具。",
            "parameters": {
                "type": "object",
                "properties": {
                    "server": {"type": "string", "description": "MCP server名称"}
                },
                "required": ["server"]
            }
        }
    },
]

# 档位 → 可用工具
GEAR_TOOLS = {
    1: [],
    2: [],
    3: ["run_shell", "http_request", "mcp_call", "list_mcp_tools", "read_board", "write_board"],
    4: ["run_shell", "http_request", "mcp_call", "list_mcp_tools", "read_board", "write_board"],
}


# ========== 执行器 ==========

def execute_shell(command: str) -> dict:
    from security import validate_command

    allowed, reason = validate_command(command)
    if not allowed:
        return {"success": False, "error": f"[安全拦截] {reason}"}

    safe_command = f"ulimit -v {Config.MEMORY_LIMIT_KB} -t {Config.CPU_TIME_LIMIT} && " + command
    try:
        result = subprocess.run(
            safe_command, shell=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=Config.SHELL_TIMEOUT,
        )
        return {
            "success": result.returncode == 0,
            "stdout": result.stdout[:Config.MAX_STDOUT_BYTES],
            "stderr": result.stderr[:Config.MAX_STDERR_BYTES],
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "error": f"命令执行超时（{Config.SHELL_TIMEOUT}秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_http(method: str, url: str, headers: dict = None, body: str = None) -> dict:
    blocked_hosts = ["169.254.169.254", "metadata.google.internal"]
    for host in blocked_hosts:
        if host in url:
            return {"success": False, "error": f"URL被安全策略拦截：禁止访问 {host}"}
    try:
        resp = requests.request(
            method=method, url=url, headers=headers or {},
            data=body, timeout=30, allow_redirects=True,
        )
        return {
            "success": True, "status_code": resp.status_code,
            "headers": dict(resp.headers), "body": resp.text[:5000],
        }
    except requests.Timeout:
        return {"success": False, "error": "请求超时（30秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_mcp(server: str, tool_name: str, arguments: dict) -> dict:
    if server not in MCP_SERVERS:
        return {"success": False, "error": f"未知MCP server: {server}。可用: {list(MCP_SERVERS.keys())}"}
    srv = MCP_SERVERS[server]
    cmd = ["python3.11", Config.MCP_CLIENT, "call"] + srv["command"] + [tool_name, json.dumps(arguments)]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        brace_depth = 0
        json_start = -1
        json_end = len(stdout)
        for i in range(len(stdout) - 1, -1, -1):
            if stdout[i] == "}":
                if brace_depth == 0:
                    json_end = i + 1
                brace_depth += 1
            elif stdout[i] == "{":
                brace_depth -= 1
                if brace_depth == 0:
                    json_start = i
                    break
        if json_start >= 0:
            return json.loads(stdout[json_start:json_end])
        return {"success": False, "error": stdout[:500]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "MCP调用超时（60秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_list_mcp_tools(server: str) -> dict:
    if server not in MCP_SERVERS:
        return {"success": False, "error": f"未知MCP server: {server}。可用: {list(MCP_SERVERS.keys())}"}
    srv = MCP_SERVERS[server]
    cmd = ["python3.11", Config.MCP_CLIENT, "list"] + srv["command"]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        json_lines = [l for l in stdout.split("\n") if l.strip().startswith(("{", "["))]
        if json_lines:
            return json.loads("\n".join(json_lines))
        return {"success": False, "error": stdout[:500]}
    except subprocess.TimeoutExpired:
        return {"success": False, "error": "MCP调用超时（60秒）"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_read_board() -> dict:
    from memory import MemoryStore
    try:
        memories = MemoryStore.get_recent(limit=10)
        return {"success": True, "messages": memories, "total": len(memories)}
    except Exception as e:
        return {"success": False, "error": str(e)}


def execute_write_board(content: str, model: str, gear: int) -> dict:
    from memory import MemoryStore
    return MemoryStore.add(content=content, msg_type="manual_note", model=model, gear=gear)


def dispatch_tool(tool_name: str, arguments: dict) -> dict:
    if tool_name == "run_shell":
        return execute_shell(arguments.get("command", ""))
    elif tool_name == "http_request":
        return execute_http(
            method=arguments.get("method", "GET"),
            url=arguments.get("url", ""),
            headers=arguments.get("headers"),
            body=arguments.get("body"),
        )
    elif tool_name == "read_board":
        return execute_read_board()
    elif tool_name == "write_board":
        return execute_write_board(
            arguments.get("content", ""),
            arguments.get("model", "unknown"),
            arguments.get("gear", 0),
        )
    elif tool_name == "mcp_call":
        return execute_mcp(
            arguments.get("server", ""),
            arguments.get("tool_name", ""),
            arguments.get("arguments", {}),
        )
    elif tool_name == "list_mcp_tools":
        return execute_list_mcp_tools(arguments.get("server", ""))
    else:
        return {"success": False, "error": f"未知工具：{tool_name}"}
