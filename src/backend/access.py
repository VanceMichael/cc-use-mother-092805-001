"""岗位与敏感资料访问控制。

儿童、老人和医疗资料只对相应岗位开放；主管可跨岗位核对。
这里的角色是演示用的岗位标识，生产环境应由认证层签发。
"""

from __future__ import annotations

from enum import Enum

from .errors import AccessDeniedError


class Role(str, Enum):
    RECEPTION = "reception"            # 接收登记人员
    CHILD_PROTECTION = "child_protection"  # 儿童保护专员
    ELDER_CARE = "elder_care"          # 老人照护专员
    MEDICAL = "medical"                # 医护人员
    LOGISTICS = "logistics"            # 物资后勤
    COORDINATOR = "coordinator"        # 省级协调员
    SUPERVISOR = "supervisor"          # 值班主管


class Sensitivity(str, Enum):
    GENERAL = "general"
    CHILD = "child"
    ELDERLY = "elderly"
    MEDICAL = "medical"


_ALL_ROLES = frozenset(Role)

# 各敏感度允许访问的岗位；主管始终在内，便于冲突核对与交接。
SENSITIVITY_ROLES: dict[Sensitivity, frozenset[Role]] = {
    Sensitivity.GENERAL: _ALL_ROLES,
    Sensitivity.CHILD: frozenset({Role.CHILD_PROTECTION, Role.SUPERVISOR}),
    Sensitivity.ELDERLY: frozenset({Role.ELDER_CARE, Role.SUPERVISOR}),
    Sensitivity.MEDICAL: frozenset({Role.MEDICAL, Role.SUPERVISOR}),
}

# 年龄段 -> 人员档案敏感度
_AGE_BAND_SENSITIVITY: dict[str, Sensitivity] = {
    "infant": Sensitivity.CHILD,
    "child": Sensitivity.CHILD,
    "teen": Sensitivity.CHILD,
    "adult": Sensitivity.GENERAL,
    "elderly": Sensitivity.ELDERLY,
}


def sensitivity_for_age_band(age_band: str) -> Sensitivity:
    """按年龄段推导档案敏感度，未知年龄段按一般资料处理。"""
    return _AGE_BAND_SENSITIVITY.get(age_band, Sensitivity.GENERAL)


def parse_role(value: str | Role) -> Role:
    try:
        return value if isinstance(value, Role) else Role(value)
    except ValueError as exc:
        raise AccessDeniedError(f"未知岗位: {value!r}") from exc


def require_role(role: str | Role, allowed: set[Role] | frozenset[Role], action: str) -> Role:
    """要求当前岗位属于允许集合，否则拒绝。"""
    parsed = parse_role(role)
    if parsed not in allowed:
        raise AccessDeniedError(f"岗位 {parsed.value} 无权执行: {action}")
    return parsed


def require_sensitivity_access(role: str | Role, sensitivity: str | Sensitivity, action: str) -> Role:
    """要求当前岗位可访问指定敏感度的资料。"""
    parsed = parse_role(role)
    level = sensitivity if isinstance(sensitivity, Sensitivity) else Sensitivity(sensitivity)
    if parsed not in SENSITIVITY_ROLES[level]:
        raise AccessDeniedError(f"岗位 {parsed.value} 无权访问{level.value}资料: {action}")
    return parsed


def visible_sensitivities(role: str | Role) -> frozenset[Sensitivity]:
    """该岗位可见的敏感度集合，用于列表过滤。"""
    parsed = parse_role(role)
    return frozenset(
        level for level, roles in SENSITIVITY_ROLES.items() if parsed in roles
    )
