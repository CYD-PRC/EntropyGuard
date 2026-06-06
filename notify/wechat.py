"""
Entropy Runtime · WeChat Notification via ServerChan
=====================================================
使用 Server酱 API 发送微信通知。

用法:
    from notify.wechat import send_notification
    send_notification("标题", "内容")
    send_notification("标题", "内容", sendkey="自定义key")  # 覆盖 .env 配置

环境变量:
    WECHAT_SERVERCHAN_KEY — Server酱 SendKey（必填）
"""
import json
import logging
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime

logger = logging.getLogger("entropyruntime.wechat")

# ========== 常量 ==========
_ENV_KEY = "WECHAT_SERVERCHAN_KEY"
_API_TEMPLATE = "https://sctapi.ftqq.com/{sendkey}.send"

# ========== 通知级别 ==========
_NOTIFY_EMOJI = {
    "info": "ℹ️",
    "success": "✅",
    "warning": "⚠️",
    "error": "❌",
    "critical": "🚨",
}

_NOTIFY_COLOR = {
    "info": "#3498db",
    "success": "#27ae60",
    "warning": "#f39c12",
    "error": "#e74c3c",
    "critical": "#c0392b",
}


def _get_sendkey() -> str:
    """从 /root/.env 读取 ServerChan SendKey"""
    env_path = "/root/.env"
    try:
        with open(env_path) as f:
            for line in f:
                ls = line.strip()
                if ls.startswith(_ENV_KEY) and "=" in ls:
                    return ls.split("=", 1)[1]
    except (FileNotFoundError, OSError):
        pass
    # 也检查环境变量
    key = os.environ.get(_ENV_KEY, "")
    return key


def send_notification(
    title: str,
    content: str,
    level: str = "info",
    sendkey: str = "",
) -> bool:
    """
    发送微信通知。

    Args:
        title: 通知标题（最多 256 字）
        content: 通知内容（支持 Markdown）
        level: 通知级别 — info/success/warning/error/critical
        sendkey: 可选，覆盖 .env 配置的 SendKey

    Returns:
        bool: 是否发送成功

    失败时静默降级（仅记录 warning 日志）。
    """
    if not sendkey:
        sendkey = _get_sendkey()

    if not sendkey:
        logger.warning("[WeChat] WECHAT_SERVERCHAN_KEY 未配置，跳过通知")
        return False

    # 截断标题（ServerChan 限制 256 字）
    title = title.strip()[:256]
    if not title:
        title = "Entropy Runtime 通知"

    # 添加 emoji 前缀
    emoji = _NOTIFY_EMOJI.get(level, "ℹ️")
    title = f"{emoji} {title}"

    # 构造 Markdown 内容
    if level in ("error", "critical"):
        # 错误通知：标题增加颜色
        title = f"{emoji} {title}"

    # 截断内容（ServerChan 限制 32KB）
    content = content.strip()
    if len(content) > 30000:
        content = content[:29900] + "\n\n...（内容已截断）"

    # 构造 POST 请求
    # 优先 HTTPS，失败后回退 HTTP（某些 ECS 环境有 HTTPS 出站限制）
    data = urllib.parse.urlencode({
        "title": title,
        "desp": content,
    }).encode("utf-8")

    for _scheme in ("https", "http"):
        url = f"{_scheme}://sctapi.ftqq.com/{sendkey}.send"
        try:
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/x-www-form-urlencoded")

            with urllib.request.urlopen(req, timeout=10) as resp:
                result = json.loads(resp.read().decode())

            if result.get("code") == 0 or result.get("data", {}).get("pushid"):
                logger.info(f"[WeChat] 通知发送成功: {title[:50]}...")
                return True
            else:
                logger.warning(f"[WeChat] ServerChan 返回异常: {result.get('message', '未知')}")
                return False

        except urllib.error.HTTPError as e:
            logger.warning(f"[WeChat] HTTP {e.code}: {e.read().decode()[:200]}")
            if _scheme == "http":
                return False
        except urllib.error.URLError as e:
            if _scheme == "http":
                logger.warning(f"[WeChat] 网络错误 ({_scheme}): {e.reason}. "
                               "请确认 ECS 安全组允许出站到 sctapi.ftqq.com:443")
                return False
        except Exception as e:
            if _scheme == "http":
                logger.warning(f"[WeChat] 发送失败 ({_scheme}): {e}")
                return False

    return False


def send_orchestrator_complete(goal: str, success: bool, task_count: int,
                                total_time: float, pass_rate: str) -> bool:
    """Orchestrator 任务完成通知"""
    status = "成功" if success else "部分失败"
    level = "success" if success else "warning"
    emoji = "✅" if success else "⚠️"

    title = f"Orchestrator {status}"
    content = f"""## Entropy Runtime Orchestrator 执行报告

**状态**: {emoji} {status}
**目标**: {goal[:100]}
**子任务数**: {task_count}
**耗时**: {total_time:.1f}s
**成功率**: {pass_rate}

---
*Entropy Runtime v8.0 • WeChat Notification*
"""
    return send_notification(title, content, level=level)


def send_test_complete(suite_name: str, total: int, passed: int,
                        failed: int, pass_rate: str) -> bool:
    """测试套件执行完成通知"""
    level = "success" if failed == 0 else ("warning" if failed < total // 2 else "error")
    emoji = "✅" if failed == 0 else ("⚠️" if failed < total // 2 else "❌")

    title = f"测试完成: {suite_name}"
    content = f"""## 🧪 测试结果

**套件**: {suite_name}
**总数**: {total}
**通过**: {passed}
**失败**: {failed}
**通过率**: {pass_rate}

---
*Entropy Runtime v8.0 • WeChat Notification*
"""
    return send_notification(title, content, level=level)


def send_retry_notification(task_id: str, agent: str, retry_count: int,
                             error: str) -> bool:
    """执行重试触发通知"""
    title = f"执行重试: {task_id}"
    content = f"""## 🔄 执行重试

**任务**: {task_id}
**当前 Agent**: {agent}
**重试次数**: {retry_count}/3
**错误**: {error[:200]}

---
*Entropy Runtime v8.0 • WeChat Notification*
"""
    return send_notification(title, content, level="warning")


def send_env_anomaly(anomaly_type: str, description: str,
                      metrics: dict = None) -> bool:
    """环境异常通知"""
    title = f"环境异常: {anomaly_type}"
    metrics_text = ""
    if metrics:
        metrics_text = "\n".join(f"- {k}: {v}" for k, v in metrics.items())

    content = f"""## 🚨 环境异常

**类型**: {anomaly_type}
**描述**: {description[:200]}
**时间**: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}

{metrics_text}

---
*Entropy Runtime v8.0 • WeChat Notification*
"""
    return send_notification(title, content, level="critical")


if __name__ == "__main__":
    # 直接运行时发送测试通知
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    title = sys.argv[1] if len(sys.argv) > 1 else "Entropy Runtime 测试通知"
    content = sys.argv[2] if len(sys.argv) > 2 else "如果收到此消息，说明 WeChat 通知集成正常 ✅"
    level = sys.argv[3] if len(sys.argv) > 3 else "success"
    result = send_notification(title, content, level=level)
    sys.exit(0 if result else 1)
