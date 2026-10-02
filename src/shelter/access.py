"""岗位与敏感资料访问矩阵。

儿童、老人、医疗资料只对相应岗位开放；无障碍需求同时供安置点工作人员用于安排床位。
任何敏感读取都会留痕（包括被拒绝的读取）。
"""

from __future__ import annotations

# role -> 可见的敏感类别
ROLE_SENSITIVE: dict[str, frozenset[str]] = {
    "medical": frozenset({"medical"}),
    "child_protection": frozenset({"child"}),
    "elder_care": frozenset({"elder", "accessibility"}),
    "shelter_staff": frozenset({"accessibility"}),
    "logistics": frozenset(),
    "rescue": frozenset(),
    "coordinator": frozenset(),  # 主管处理线索与回执冲突，但不直接看诊疗内容
}

SENSITIVE_CATEGORIES = ("medical", "child", "elder", "accessibility")


def can_access(role: str, category: str) -> bool:
    return category in ROLE_SENSITIVE.get(role, frozenset())
