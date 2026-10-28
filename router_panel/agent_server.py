from __future__ import annotations

import grp
import copy
import json
import logging
import os
import queue
import socketserver
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .core import (
    CommandResult,
    get_network_interface_hardware,
    request_system_reboot,
)
from .dependencies import gather_dependency_status, run_dependency_action
from .network import gather_wired_network_info, list_all_interfaces
from .routing import (
    apply_router_config,
    gather_client_status,
    gather_router_status,
    load_router_config,
    normalize_router_config,
)
from .service_monitor import gather_service_monitor_status, run_service_action
from .system import gather_system_info
from .tailscale import (
    gather_tailscale_status,
    logout_tailscale,
    normalize_tailscale_config,
    start_tailscale_login,
)


SOCKET_PATH = Path(os.environ.get("LINUX_ROUTER_AGENT_SOCKET", "/run/linux-router/agent.sock"))
MAX_REQUEST_BYTES = 1024 * 1024
MAX_QUEUED_OPERATIONS = 32
MAX_OPERATION_HISTORY = 500
ProgressCallback = Callable[[str], None]

ROUTER_CONFIG_FIELDS = (
    "wan_interface",
    "lan_interface",
    "lan_network",
    "dhcp_enabled",
    "dhcp_start",
    "dhcp_end",
    "dhcp_lease",
    "dns_upstream",
    "ipv6_mode",
    "ipv6_prefix",
    "ipv6_nat",
    "wan_ipv6_auto",
    "mtu",
)


class ValidationError(ValueError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OperationRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._operations: dict[str, dict[str, Any]] = {}

    def create(self, action: str, scope: str, context: dict[str, Any]) -> dict[str, Any]:
        operation_id = str(uuid.uuid4())
        operation = {
            "id": operation_id,
            "action": action,
            "scope": scope,
            "context": copy.deepcopy(context),
            "status": "queued",
            "progress_message": "",
            "result": None,
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
        }
        with self._lock:
            completed = [
                item
                for item in self._operations.values()
                if item["status"] in {"succeeded", "failed"}
            ]
            completed.sort(key=lambda item: item["finished_at"] or "", reverse=True)
            for item in completed[MAX_OPERATION_HISTORY - 1 :]:
                self._operations.pop(item["id"], None)
            self._operations[operation_id] = operation
        return self.get(operation_id)

    def mark_running(self, operation_id: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            operation["status"] = "running"
            operation["started_at"] = _utc_now()

    def update_progress(self, operation_id: str, message: str) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            if operation["status"] not in {"queued", "running"}:
                return
            operation["progress_message"] = message.strip()[:120]

    def finish(self, operation_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            operation["status"] = "succeeded" if result.get("ok") else "failed"
            operation["result"] = copy.deepcopy(result)
            operation["progress_message"] = ""
            operation["finished_at"] = _utc_now()

    def get(self, operation_id: str) -> dict[str, Any]:
        try:
            normalized_id = str(uuid.UUID(operation_id))
        except ValueError as exc:
            raise ValidationError("无效的操作编号") from exc
        with self._lock:
            operation = self._operations.get(normalized_id)
            if operation is None:
                raise ValidationError("找不到该操作")
            return copy.deepcopy(operation)


def _require_string(
    params: dict[str, Any],
    name: str,
    *,
    maximum: int = 256,
    strip: bool = True,
) -> str:
    value = params.get(name, "")
    if not isinstance(value, str):
        raise ValidationError(f"{name} 参数无效")
    if len(value) > maximum:
        raise ValidationError(f"{name} 参数过长")
    return value.strip() if strip else value


def _validate_context(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or len(value) > 8:
        raise ValidationError("操作上下文无效")
    context: dict[str, Any] = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > 40:
            raise ValidationError("操作上下文无效")
        if not isinstance(item, (str, bool, int)) or isinstance(item, str) and len(item) > 256:
            raise ValidationError("操作上下文无效")
        context[key] = item
    return context


def _result(result: CommandResult, success_message: str, fallback_error: str) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "message": success_message if result.ok else (result.output or fallback_error),
    }


def available_router_interfaces() -> list[str]:
    """Wired NICs, used for the LAN selector."""
    return [
        str(item.get("name", ""))
        for item in get_network_interface_hardware()
        if item.get("name") and not item.get("wireless")
    ]


def available_wan_interfaces() -> list[str]:
    """Any interface, because a PPPoE uplink is ppp0 rather than the NIC itself."""
    return [str(item.get("name", "")) for item in list_all_interfaces()]


def _execute_router_apply(
    params: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    payload = {key: params[key] for key in ROUTER_CONFIG_FIELDS if key in params}
    if not payload:
        raise ValidationError("缺少路由配置参数")
    config, error = normalize_router_config(
        payload,
        existing=load_router_config(),
        available_interfaces=available_router_interfaces(),
        available_wan_interfaces=available_wan_interfaces(),
    )
    if error:
        raise ValidationError(error)
    return _result(apply_router_config(config, progress=progress), "路由配置已生效", "应用路由配置失败")


def _execute_dependency(params: dict[str, Any]) -> dict[str, Any]:
    action = _require_string(params, "action", maximum=64)
    return _result(run_dependency_action(action), "处置完成", "处置失败")


def _execute_reboot(params: dict[str, Any]) -> dict[str, Any]:
    return _result(request_system_reboot(), "设备正在重启", "设备重启失败")


def _execute_tailscale_login(params: dict[str, Any]) -> dict[str, Any]:
    config, error = normalize_tailscale_config(params)
    if error:
        raise ValidationError(error)
    return start_tailscale_login(config)


def _execute_tailscale_logout(params: dict[str, Any]) -> dict[str, Any]:
    return _result(logout_tailscale(), "已退出 Tailscale 登录", "退出 Tailscale 登录失败")


def _execute_service_monitor_action(params: dict[str, Any]) -> dict[str, Any]:
    service = _require_string(params, "service", maximum=128)
    action = _require_string(params, "action", maximum=16)
    labels = {
        "start": "服务已启动",
        "stop": "服务已停止",
        "restart": "服务已重启",
    }
    return _result(run_service_action(service, action), labels.get(action, "服务操作已完成"), "服务操作失败")


OPERATIONS: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
    "router_apply": _execute_router_apply,
    "dependency_fix": _execute_dependency,
    "system_reboot": _execute_reboot,
    "tailscale_login": _execute_tailscale_login,
    "tailscale_logout": _execute_tailscale_logout,
    "service_monitor_action": _execute_service_monitor_action,
}


def execute_query(name: str, params: dict[str, Any]) -> Any:
    if name == "health":
        return {"status": "ok"}
    if name == "system_info":
        return gather_system_info()
    if name == "dependency_status":
        return gather_dependency_status()
    if name == "wired_status":
        return gather_wired_network_info(load_router_config())
    if name == "router_status":
        return gather_router_status()
    if name == "client_status":
        return gather_client_status()
    if name == "tailscale_status":
        return gather_tailscale_status()
    if name == "service_monitor_status":
        return gather_service_monitor_status()
    raise ValidationError("不支持的查询")


class AgentRuntime:
    def __init__(self, registry: OperationRegistry | None = None) -> None:
        self.store = registry or OperationRegistry()
        self.queue: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue(MAX_QUEUED_OPERATIONS)
        self.submit_lock = threading.Lock()
        self.worker = threading.Thread(target=self._run_worker, name="router-agent-worker", daemon=True)
        self.worker.start()

    def submit(
        self,
        action: str,
        params: dict[str, Any],
        scope: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        if action not in OPERATIONS:
            raise ValidationError("不支持的系统操作")
        if not isinstance(params, dict) or len(params) > 24:
            raise ValidationError("操作参数无效")
        if scope not in {"router", "dependencies", "system", "tools"}:
            raise ValidationError("操作范围无效")
        with self.submit_lock:
            if self.queue.full():
                raise ValidationError("系统操作队列已满，请稍后再试")
            operation = self.store.create(action, scope, _validate_context(context))
            self.queue.put_nowait((operation["id"], action, params.copy()))
        return operation

    def _execute_operation(
        self,
        operation_id: str,
        action: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        if action == "router_apply":
            def progress(message: str) -> None:
                self.store.update_progress(operation_id, message)

            return _execute_router_apply(params, progress)

        return OPERATIONS[action](params)

    def _run_worker(self) -> None:
        while True:
            operation_id, action, params = self.queue.get()
            try:
                self.store.mark_running(operation_id)
                logging.info("operation started id=%s action=%s", operation_id, action)
                try:
                    result = self._execute_operation(operation_id, action, params)
                except ValidationError as exc:
                    result = {"ok": False, "message": str(exc)}
                except Exception as exc:
                    result = {"ok": False, "message": f"系统操作异常：{exc}"}
                self.store.finish(operation_id, result)
                logging.info(
                    "operation finished id=%s action=%s ok=%s",
                    operation_id,
                    action,
                    bool(result.get("ok")),
                )
            except Exception:
                logging.exception("operation state update failed id=%s action=%s", operation_id, action)
            finally:
                self.queue.task_done()


class AgentRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        raw = self.rfile.readline(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            self._respond({"ok": False, "error": "请求过大"})
            return
        try:
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValidationError("请求格式无效")
            method = payload.get("method")
            runtime: AgentRuntime = self.server.runtime  # type: ignore[attr-defined]
            if method == "query":
                name = payload.get("name", "")
                params = payload.get("params", {})
                if not isinstance(name, str) or not isinstance(params, dict):
                    raise ValidationError("查询参数无效")
                response = {"ok": True, "result": execute_query(name, params)}
            elif method == "submit":
                response = {
                    "ok": True,
                    "operation": runtime.submit(
                        payload.get("action", ""),
                        payload.get("params", {}),
                        payload.get("scope", ""),
                        payload.get("context", {}),
                    ),
                }
            elif method == "operation":
                operation_id = payload.get("operation_id", "")
                if not isinstance(operation_id, str):
                    raise ValidationError("无效的操作编号")
                response = {"ok": True, "operation": runtime.store.get(operation_id)}
            else:
                raise ValidationError("不支持的请求")
        except (json.JSONDecodeError, UnicodeDecodeError):
            response = {"ok": False, "error": "请求格式无效"}
        except ValidationError as exc:
            response = {"ok": False, "error": str(exc)}
        except Exception as exc:
            response = {"ok": False, "error": f"系统控制服务异常：{exc}"}
        self._respond(response)

    def _respond(self, response: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode("utf-8"))


class AgentServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, socket_path: Path, runtime: AgentRuntime) -> None:
        self.runtime = runtime
        super().__init__(str(socket_path), AgentRequestHandler)


def run_agent() -> None:
    if os.geteuid() != 0:
        raise SystemExit("router-panel-agent must run as root")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SOCKET_PATH.exists() or SOCKET_PATH.is_socket():
        SOCKET_PATH.unlink()
    runtime = AgentRuntime()
    server = AgentServer(SOCKET_PATH, runtime)
    os.chmod(SOCKET_PATH, 0o660)
    group_name = os.environ.get("LINUX_ROUTER_AGENT_GROUP", "router-panel")
    try:
        os.chown(SOCKET_PATH, 0, grp.getgrnam(group_name).gr_gid)
    except KeyError:
        pass
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()
        SOCKET_PATH.unlink(missing_ok=True)


__all__ = [
    "AgentRuntime",
    "OperationRegistry",
    "ValidationError",
    "execute_query",
    "run_agent",
]
