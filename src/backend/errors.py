"""安置与家庭团聚后端的领域异常。"""

from __future__ import annotations


class DomainError(Exception):
    """所有领域异常的基类。"""


class NotFoundError(DomainError):
    """引用的实体不存在。"""


class AccessDeniedError(DomainError):
    """当前岗位无权读取或写入该资料。"""


class ConflictError(DomainError):
    """当前状态与请求冲突（如重复证件、已确认的转运）。"""


class ValidationError(DomainError):
    """请求内容不合法。"""


class ConservationError(DomainError):
    """物资批次守恒校验失败。"""
