#!/usr/bin/env python3.11
"""Entropy Runtime MCP Client - connects to MCP servers via stdio JSON-RPC"""

import json
import sys
import asyncio
from typing import Any, Dict, List, Optional

try:
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
except ImportError:
    print(json.dumps({"error": "MCP SDK not installed"}))
    sys.exit(1)


async def connect_and_list_tools(server_command, server_args=None):
    server_params = StdioServerParameters(
        command=server_command[0],
        args=server_command[1:] + (server_args or [])
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            return {
                "success": True,
                "tools": [
                    {
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.inputSchema
                    }
                    for t in tools.tools
                ]
            }


async def call_tool(server_command, tool_name, arguments, server_args=None):
    server_params = StdioServerParameters(
        command=server_command[0],
        args=server_command[1:] + (server_args or [])
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
            return {
                "success": True,
                "tool": tool_name,
                "result": [
                    {"type": c.type, "text": c.text} if hasattr(c, "text") else {"type": c.type}
                    for c in result.content
                ]
            }


async def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            "usage": "python3.11 mcp_client.py <list|call> <server_cmd> [args...] [tool_name] [args_json]",
            "example_list": "python3.11 mcp_client.py list npx -y @modelcontextprotocol/server-filesystem /tmp",
            "example_call": "python3.11 mcp_client.py call npx -y @modelcontextprotocol/server-filesystem /tmp read_file {\"path\": \"/tmp/test.txt\"}"
        }, indent=2))
        return

    action = sys.argv[1]
    server_command = [sys.argv[2]]
    server_args = sys.argv[3:] if len(sys.argv) > 3 else []

    try:
        if action == "list":
            result = await connect_and_list_tools(server_command, server_args)
        elif action == "call":
            if len(server_args) < 2:
                print(json.dumps({"error": "call requires: ...server_args... tool_name [args_json]"}))
                return
            tool_name = server_args[-2] if len(server_args) >= 2 else server_args[0]
            arguments = json.loads(server_args[-1]) if len(server_args) >= 2 else {}
            actual_server_args = server_args[:-2] if len(server_args) >= 2 else []
            result = await call_tool(server_command, tool_name, arguments, actual_server_args)
        else:
            result = {"error": "Unknown action: " + action + ". Use list or call"}

        print(json.dumps(result, ensure_ascii=False, indent=2))
    except Exception as e:
        print(json.dumps({"error": str(e)}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main())
