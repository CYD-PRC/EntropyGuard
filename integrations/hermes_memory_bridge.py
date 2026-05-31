"""
Entropy Runtime · Hermes Memory Bridge
========================================

桥接 Entropy Runtime MessageBoard 和 Hermes Agent 记忆系统。

核心流程:
  1. Hermes 启动时 → 从 MessageBoard 读取 persona/fact/skill 记忆 → 构建 system prompt 上下文
  2. Hermes 会话中  → 通过 HTTP API 实时读写记忆
  3. Hermes 会话结束 → 自动将关键信息写入 MessageBoard（类型=episode）

使用方法:
    from integrations.hermes_memory_bridge import HermesMemoryBridge

    bridge = HermesMemoryBridge(base_url="http://127.0.0.1:8000")
    context = bridge.get_context()          # 获取 system prompt 上下文
    bridge.save_episode(session_summary)    # 保存会话摘要
"""

import json
import logging
from datetime import datetime
from typing import Any, Dict, List, Optional
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

logger = logging.getLogger("hermes.memory_bridge")


class HermesMemoryBridge:
    """
    Hermes 与 Entropy Runtime MessageBoard 之间的记忆桥接器。

    提供两类接口:
      读取端: get_context(), get_persona(), get_facts(), get_skills()
      写入端: save_fact(), save_persona(), save_skill(), save_episode()
    """

    MEMORY_TYPES = ("persona", "fact", "skill", "episode")

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:8000",
        api_key: Optional[str] = None,
        source: str = "hermes",
        timeout: int = 10,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.source = source
        self.timeout = timeout
        self._auth_header = f"Authorization: Bearer {api_key}" if api_key else None

    # ==================== 读取端 ====================

    def get_context(self) -> dict:
        """
        获取 Hermes 系统提示注入上下文。

        返回: {
            "persona": [...],  # 用户画像
            "fact": [...],     # 环境事实
            "skill": [...],    # 程序性知识
        }
        调用 GET /api/messageboard/memory/context
        """
        raw = self._http_get("/api/messageboard/memory/context")
        if not raw:
            return {"persona": [], "fact": [], "skill": []}
        ctx = raw.get("context", {})
        for t in ("persona", "fact", "skill"):
            if t not in ctx:
                ctx[t] = []
        return ctx

    def get_persona(self) -> List[str]:
        """获取用户画像记忆列表"""
        ctx = self.get_context()
        return ctx.get("persona", [])

    def get_facts(self) -> List[str]:
        """获取环境事实列表"""
        ctx = self.get_context()
        return ctx.get("fact", [])

    def get_skills(self) -> List[str]:
        """获取技能/工作流列表"""
        ctx = self.get_context()
        return ctx.get("skill", [])

    def get_by_type(self, memory_type: str, limit: int = 10) -> List[Dict[str, Any]]:
        """
        查询指定类型的原始记忆数据。
        调用 GET /api/messageboard/memory/{memory_type}
        """
        if memory_type not in self.MEMORY_TYPES:
            logger.warning(f"[Bridge] 未知记忆类型: {memory_type}")
            return []
        raw = self._http_get(f"/api/messageboard/memory/{memory_type}?limit={limit}")
        if not raw:
            return []
        return raw.get("memories", [])

    # ==================== 写入端 ====================

    def save_persona(self, text: str, **metadata) -> Optional[str]:
        """写入用户画像"""
        return self._write("persona", text, **metadata)

    def save_fact(self, text: str, **metadata) -> Optional[str]:
        """写入环境事实"""
        return self._write("fact", text, **metadata)

    def save_skill(self, text: str, **metadata) -> Optional[str]:
        """写入技能/工作流"""
        return self._write("skill", text, **metadata)

    def save_episode(self, text: str, **metadata) -> Optional[str]:
        """写入会话情节摘要"""
        return self._write("episode", text, **metadata)

    def _write(self, memory_type: str, text: str, **metadata) -> Optional[str]:
        """
        内部：向 MessageBoard 发送一条记忆写入请求。

        POST /api/messageboard/memory
        Body: {"memory_type": ..., "content": {"text": ..., **metadata}, "source": ...}
        """
        content = {"text": text}
        content.update(metadata)
        payload = {
            "memory_type": memory_type,
            "content": content,
            "source": self.source,
        }
        raw = self._http_post("/api/messageboard/memory", payload)
        if raw and raw.get("success"):
            mid = raw.get("memory_id")
            logger.info(f"[Bridge] 已写入 {memory_type}: {mid}")
            return mid
        logger.warning(f"[Bridge] 写入 {memory_type} 失败: {raw}")
        return None

    # ==================== 构建 system prompt ====================

    def build_system_context(self) -> str:
        """
        构建可直接注入 system prompt 的上下文文本。

        返回类似:
            [MEMORY: PERSONA]
            - 用户偏好：喜欢简洁的回复
            ...

            [MEMORY: FACTS]
            - 项目路径：/root/EntropyGuard
            ...

            [MEMORY: SKILLS]
            - 工作流：部署步骤 ...
        """
        ctx = self.get_context()
        parts = []

        if ctx.get("persona"):
            parts.append("[MEMORY: PERSONA]")
            for item in ctx["persona"]:
                parts.append(f"- {item}")
            parts.append("")

        if ctx.get("fact"):
            parts.append("[MEMORY: FACTS]")
            for item in ctx["fact"]:
                parts.append(f"- {item}")
            parts.append("")

        if ctx.get("skill"):
            parts.append("[MEMORY: SKILLS]")
            for item in ctx["skill"]:
                parts.append(f"- {item}")

        return "\n".join(parts)

    # ==================== HTTP 客户端 ====================

    def _http_get(self, path: str) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        req = Request(url, method="GET")
        return self._do_request(req)

    def _http_post(self, path: str, data: dict) -> Optional[dict]:
        url = f"{self.base_url}{path}"
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        req = Request(url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._do_request(req)

    def _do_request(self, req: Request) -> Optional[dict]:
        if self._auth_header:
            req.add_header("Authorization", self._auth_header.split(": ", 1)[1])
        try:
            with urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw)
        except HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:200]
            logger.error(f"[Bridge] HTTP {e.code} on {req.full_url}: {body}")
            return None
        except URLError as e:
            logger.error(f"[Bridge] URL error on {req.full_url}: {e.reason}")
            return None
        except Exception as e:
            logger.error(f"[Bridge] Request failed: {e}")
            return None


# ==================== 便捷函数 ====================

def get_bridge(base_url: str = "http://127.0.0.1:8000",
               api_key: Optional[str] = None,
               source: str = "hermes") -> HermesMemoryBridge:
    """工厂函数：创建并返回 HermesMemoryBridge 实例"""
    return HermesMemoryBridge(
        base_url=base_url,
        api_key=api_key,
        source=source,
    )


# ==================== Hermes 集成钩子 ====================

def hermes_startup_hook() -> str:
    """
    Hermes 启动时调用的钩子函数。

    从 MessageBoard 读取 persona/fact/skill 记忆，
    返回格式化后的 system prompt 注入文本。

    用法（在 hermes config 中配置）:
        startup_hooks:
          - "integrations.hermes_memory_bridge.hermes_startup_hook"
    """
    import os
    api_key = os.environ.get("ENTROPY_RUNTIME_API_KEY", "")
    bridge = get_bridge(api_key=api_key)
    context = bridge.build_system_context()
    if context:
        logger.info("[MemoryBridge] 已注入记忆上下文到 system prompt")
    else:
        logger.info("[MemoryBridge] 无记忆数据，跳过注入")
    return context


def hermes_shutdown_hook(session_summary: str, **metadata):
    """
    Hermes 会话结束时调用的钩子函数。

    将会话摘要写入 MessageBoard（类型=episode）。

    用法:
        hermes_shutdown_hook("本次会话完成了漏洞修复...", session_id="xxx")
    """
    import os
    api_key = os.environ.get("ENTROPY_RUNTIME_API_KEY", "")
    bridge = get_bridge(api_key=api_key)
    mid = bridge.save_episode(session_summary, **metadata)
    if mid:
        logger.info(f"[MemoryBridge] 已保存会话情节: {mid}")
    return mid
