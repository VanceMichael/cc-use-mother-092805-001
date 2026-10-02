"""HTTP 接口层（演示用）。

POST /commands/{command}   执行写操作，请求体为 kwargs JSON
GET  /queries/{name}?...   执行只读查询，参数走查询字符串

岗位经由 X-Actor-Id / X-Actor-Role 请求头传入；生产环境应前置真实
认证层签发岗位，这里仅用于联调与演示。
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

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

# 允许通过 HTTP 调用的写操作（命令）
COMMANDS = {
    "register_community",
    "register_shelter",
    "create_evacuation_batch",
    "register_person",
    "record_identity_step",
    "record_need",
    "create_family",
    "add_family_member",
    "record_clue",
    "confirm_clue",
    "reject_clue",
    "merge_persons",
    "plan_transfer",
    "confirm_transfer",
    "cancel_transfer",
    "discharge_person",
    "return_home",
    "reallocate",
    "register_supply_batch",
    "distribute",
    "plan_distribution",
    "execute_planned_distribution",
    "return_supplies",
    "hand_over",
    "transfer_stock",
    "reissue",
    "submit_receipt",
    "resolve_conflict",
    "complete_task",
}

# 只读查询；值是参数名列表之外的额外处理标志
_QUERIES = {
    "pending_tasks",
    "get_person",
    "reception_record",
    "list_needs",
    "batch_ledger",
    "verify_conservation",
    "family_story",
    "shift_handover",
    "recover_pending",
}

_STATUS_BY_ERROR = (
    (AccessDeniedError, 403),
    (NotFoundError, 404),
    (ValidationError, 400),
    (ConflictError, 409),
    (ConservationError, 409),
)


def _status_for(exc: DomainError) -> int:
    for error_type, status in _STATUS_BY_ERROR:
        if isinstance(exc, error_type):
            return status
    return 400


class _Handler(BaseHTTPRequestHandler):
    service: ShelterService
    lock: threading.Lock

    server_version = "ShelterBackend/0.1"

    def log_message(self, format: str, *args: Any) -> None:  # 静默访问日志
        return

    def _send(self, status: int, body: dict[str, Any]) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _identity(self) -> tuple[str, str]:
        actor = self.headers.get("X-Actor-Id", "").strip()
        role = self.headers.get("X-Actor-Role", "").strip()
        if not actor or not role:
            raise ValidationError("缺少 X-Actor-Id 或 X-Actor-Role 请求头")
        return actor, role

    def do_POST(self) -> None:  # noqa: N802（标准库命名）
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2 or parts[0] != "commands" or parts[1] not in COMMANDS:
            self._send(404, {"ok": False, "error": "未知命令"})
            return
        try:
            actor, role = self._identity()
            length = int(self.headers.get("Content-Length") or 0)
            kwargs = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(kwargs, dict):
                raise ValidationError("请求体必须是 JSON 对象")
            with self.lock:
                result = getattr(self.service, parts[1])(actor, role, **kwargs)
            self._send(200, {"ok": True, "result": result})
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
        except TypeError as exc:
            self._send(400, {"ok": False, "error": f"参数不匹配: {exc}"})
        except DomainError as exc:
            self._send(_status_for(exc), {"ok": False, "error": str(exc), "type": type(exc).__name__})

    def do_GET(self) -> None:  # noqa: N802（标准库命名）
        parsed = urlparse(self.path)
        parts = [p for p in parsed.path.split("/") if p]
        if len(parts) != 2 or parts[0] != "queries" or parts[1] not in _QUERIES:
            self._send(404, {"ok": False, "error": "未知查询"})
            return
        try:
            actor, role = self._identity()
            params = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            if "include_sensitive" in params:
                params["include_sensitive"] = params["include_sensitive"].lower() in {"1", "true"}
            with self.lock:
                result = self._run_query(parts[1], actor, role, params)
            self._send(200, {"ok": True, "result": result})
        except KeyError as exc:
            self._send(400, {"ok": False, "error": f"缺少查询参数: {exc}"})
        except DomainError as exc:
            self._send(_status_for(exc), {"ok": False, "error": str(exc), "type": type(exc).__name__})

    def _run_query(self, name: str, actor: str, role: str, params: dict[str, Any]) -> Any:
        service = self.service
        if name == "pending_tasks":
            return service.pending_tasks(actor, role)
        if name == "get_person":
            return service.get_person(
                actor, role, params["person_id"],
                include_sensitive=params.get("include_sensitive", False),
            )
        if name == "reception_record":
            return service.reception_record(actor, role, params["person_id"])
        if name == "list_needs":
            return service.list_needs(actor, role, params["person_id"])
        if name == "batch_ledger":
            return service.batch_ledger(actor, role, params["batch_id"])
        if name == "verify_conservation":
            return service.verify_conservation(actor, role, params["batch_id"])
        if name == "family_story":
            return family_story(service, actor, role, params["family_id"])
        if name == "shift_handover":
            return shift_handover(service, actor, role)
        if name == "recover_pending":
            return recover_pending(service)
        raise ValidationError(f"未知查询: {name}")


def make_server(service: ShelterService, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    """构建 HTTP 服务；单锁串行化写操作（SQLite 单连接）。"""
    handler = type("BoundHandler", (_Handler,), {"service": service, "lock": threading.Lock()})
    return ThreadingHTTPServer((host, port), handler)
