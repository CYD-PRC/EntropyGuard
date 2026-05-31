"""
Entropy Runtime · 约束规则注册中心
支持来自不同参与方（human、AI、system）的约束提案，经确认后生效。
"""


class ConstraintRegistry:
    """约束规则注册中心。"""

    def __init__(self):
        self.constraints = {}

    def register(self, name: str, rule_fn, proposer: str = "system", description: str = ""):
        """注册一条约束规则。system 提案自动生效，其他提案状态为 pending。"""
        self.constraints[name] = {
            "rule_fn": rule_fn,
            "proposer": proposer,
            "description": description,
            "status": "active" if proposer == "system" else "pending",
            "accepted_by": ["system"] if proposer == "system" else [],
        }

    def accept(self, name: str, acceptor: str):
        """接受一条约束规则。human 和 system 都接受时规则激活。"""
        if name in self.constraints:
            c = self.constraints[name]
            if acceptor not in c["accepted_by"]:
                c["accepted_by"].append(acceptor)
            if "human" in c["accepted_by"] and "system" in c["accepted_by"]:
                c["status"] = "active"

    def reject(self, name: str, rejector: str):
        """拒绝一条约束规则。"""
        if name in self.constraints:
            self.constraints[name]["status"] = "rejected"

    def check(self, name: str, **kwargs) -> bool:
        """检查约束是否通过。True = 通过，False = 违反。未注册或未激活时默认通过。"""
        if name not in self.constraints:
            return True
        c = self.constraints[name]
        if c["status"] != "active":
            return True
        try:
            return bool(c["rule_fn"](**kwargs))
        except Exception as e:
            import logging
            logging.getLogger("entropyruntime").warning(f"Constraint '{name}' check raised exception: {e}")
            return False  # 规则执行异常时阻断（安全默认值）

    def list_active(self) -> list:
        """列出所有激活的约束。"""
        return [
            {"name": n, "proposer": c["proposer"], "description": c["description"]}
            for n, c in self.constraints.items()
            if c["status"] == "active"
        ]

    def list_pending(self) -> list:
        """列出所有待确认的约束。"""
        return [
            {"name": n, "proposer": c["proposer"], "description": c["description"]}
            for n, c in self.constraints.items()
            if c["status"] == "pending"
        ]
