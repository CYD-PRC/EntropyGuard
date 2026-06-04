"""Entropy Runtime · 优先级规则引擎
v7.0: 根据健康度和任务类型决定任务优先级
"""
import logging

logger = logging.getLogger("entropyruntime.priority")

# 安全漏洞类关键词（无论健康度多低，都接受）
SECURITY_VULN_KEYWORDS = [
    "安全", "security", "漏洞", "vuln", "脆弱", "脆弱性",
    "cve", "cwe", "补丁", "patch", "修复", "fix", "hotfix",
    "溢出", "注入", "xss", "csrf", "ssrf", "rce", "lfi", "rfi",
    "提权", "绕过", "bypass", "渗透", "加固", "硬化", "harden",
    "认证", "授权", "session", "cookie", "token", "jwt",
    "加密", "解密", "hash", "哈希", "签名", "certificate",
    "审计", "audit", "安全检查", "安全扫描", "安全修复",
    "sql注入", "命令注入", "代码注入", "路径遍历", "path traversal",
    "输入校验", "output verification", "输出校验", "shell注入",
    "权限", "permission", "privilege", "sandbox", "沙箱",
    "恶意", "malicious", "木马", "trojan", "后门", "backdoor",
]

# 功能开发类关键词（健康度低时降级）
FEATURE_KEYWORDS = [
    "新功能", "feature", "新增", "添加", "add", "开发",
    "重构", "refactor", "优化", "optimize", "性能", "performance",
    "ui", "界面", "前端", "frontend", "样式", "style",
    "文档", "doc", "readme", "注释", "comment",
    "测试", "test", "单元测试", "集成测试",
]


class PriorityEngine:
    """优先级评估引擎，结合健康度和任务类型判断"""

    @staticmethod
    def _is_security_task(task_description: str) -> bool:
        """判断任务是否为安全修复类"""
        desc_lower = task_description.lower()
        for kw in SECURITY_VULN_KEYWORDS:
            if kw in desc_lower:
                return True
        return False

    @staticmethod
    def _is_feature_task(task_description: str) -> bool:
        """判断任务是否为功能开发类"""
        desc_lower = task_description.lower()
        for kw in FEATURE_KEYWORDS:
            if kw in desc_lower:
                return True
        return False

    def evaluate_task(self, task: dict | str, health_score: int) -> dict:
        """评估任务优先级。

        Args:
            task: 任务描述字符串，或包含 task_description 字段的 dict
            health_score: 健康度评分 (0-100)

        Returns:
            dict:
                - priority: str (CRITICAL / HIGH / LOW / REJECT)
                - health_score: int
                - reason: str
                - is_security: bool
        """
        # 提取任务描述
        if isinstance(task, dict):
            description = task.get("task_description", task.get("description", str(task)))
        else:
            description = str(task)

        # 安全漏洞类任务始终 CRITICAL
        if self._is_security_task(description):
            return {
                "priority": "CRITICAL",
                "health_score": health_score,
                "reason": "安全漏洞类任务，始终为最高优先级",
                "is_security": True,
            }

        # 根据健康度判断
        if health_score < 70:
            return {
                "priority": "REJECT",
                "health_score": health_score,
                "reason": f"系统健康度 {health_score} 分（低于 70），仅接受安全修复任务",
                "is_security": False,
            }
        elif health_score < 90:
            if self._is_feature_task(description):
                return {
                    "priority": "LOW",
                    "health_score": health_score,
                    "reason": f"系统健康度 {health_score} 分（70-90），功能开发降级为 LOW",
                    "is_security": False,
                }
            else:
                return {
                    "priority": "HIGH",
                    "health_score": health_score,
                    "reason": f"系统健康度 {health_score} 分（70-90），非功能开发任务正常接受",
                    "is_security": False,
                }
        else:
            return {
                "priority": "HIGH",
                "health_score": health_score,
                "reason": f"系统健康度 {health_score} 分（>90），全部正常接受",
                "is_security": False,
            }
