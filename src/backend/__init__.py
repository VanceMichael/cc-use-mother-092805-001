"""安置与家庭团聚后端。

- store：SQLite 持久层与只追加事件日志
- service：领域服务（登记、核验、合并、转运、物资、回执）
- access：岗位与敏感资料访问控制
- recovery：重启后四类待办续接
- handover：家庭经历还原与班次交接摘要
- api：演示用 HTTP 接口
"""

from .access import Role, Sensitivity
from .errors import (
    AccessDeniedError,
    ConflictError,
    ConservationError,
    DomainError,
    NotFoundError,
    ValidationError,
)
from .handover import family_story, shift_handover
from .recovery import recover_pending
from .service import ShelterService
from .store import Store

__all__ = [
    "AccessDeniedError",
    "ConflictError",
    "ConservationError",
    "DomainError",
    "NotFoundError",
    "Role",
    "Sensitivity",
    "ShelterService",
    "Store",
    "ValidationError",
    "family_story",
    "recover_pending",
    "shift_handover",
]
