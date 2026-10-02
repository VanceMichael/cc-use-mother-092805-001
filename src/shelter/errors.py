"""领域错误。"""

from __future__ import annotations


class ShelterError(Exception):
    """所有可预期的业务规则冲突基类。"""


class NotFound(ShelterError):
    pass


class PermissionDenied(ShelterError):
    pass


class Conflict(ShelterError):
    pass


class InsufficientStock(Conflict):
    pass


class CapacityExceeded(Conflict):
    pass


class ReceiptConflict(Conflict):
    """回执关键内容不一致，需主管核对。"""
