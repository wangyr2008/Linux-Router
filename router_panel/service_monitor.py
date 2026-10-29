from __future__ import annotations

import json
import re
from typing import Any

from .core import DATA_DIR, CommandResult, atomic_write_text, run_command


SERVICE_MONITOR_CONFIG_PATH = DATA_DIR / "service-monitor.json"
DEFAULT_SERVICE_MONITOR_CONFIG = {"services": []}
SERVICE_ACTIONS = {"start", "stop", "restart"}
# `systemctl is-enabled` answers. "static"/"indirect"/"generated" mean the unit has
# no [Install] section, so there is nothing to enable rather than a problem.
ENABLED_LABELS = {
    "enabled": "开机自启",
    "enabled-runtime": "仅本次运行",
    "disabled": "未自启",
    "static": "静态（无自启项）",
    "indirect": "间接",
    "generated": "自动生成",
    "alias": "别名",
    "linked": "链接",
    "masked": "已屏蔽",
    "masked-runtime": "已屏蔽",
    "not-found": "不存在",
}
ENABLED_OK = {"enabled", "static", "indirect", "generated", "alias", "linked"}
ENABLED_WARNING = {"disabled", "enabled-runtime"}
SERVICE_NAME_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,128}\.service$")
PROTECTED_SERVICE_NAMES = {
    "router-panel.service",
    "router-panel-agent.service",
}


def normalize_service_name(value: str) -> tuple[str, str | None]:
    name = value.strip()
    if not name:
        return "", None
    if "." not in name:
        name = f"{name}.service"
    if not SERVICE_NAME_RE.fullmatch(name):
        return "", f"服务名称无效：{value.strip()}"
    if name in PROTECTED_SERVICE_NAMES:
        return "", f"不能监控当前面板自身服务：{name}"
    return name, None


def normalize_service_list(value: str | list[Any]) -> tuple[list[str], str | None]:
    if isinstance(value, list):
        raw_items = [str(item) for item in value]
    else:
        raw_items = str(value).replace("\n", ",").replace(";", ",").split(",")

    services: list[str] = []
    for raw_item in raw_items:
        service, error = normalize_service_name(raw_item)
        if error:
            return [], error
        if service:
            services.append(service)
    return list(dict.fromkeys(services)), None


def normalize_service_monitor_config(payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    services, error = normalize_service_list(payload.get("services", []))
    if error:
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy(), error
    return {"services": services}, None


def load_service_monitor_config() -> dict[str, Any]:
    try:
        payload = json.loads(SERVICE_MONITOR_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy()
    if not isinstance(payload, dict):
        return DEFAULT_SERVICE_MONITOR_CONFIG.copy()
    config, error = normalize_service_monitor_config(payload)
    return DEFAULT_SERVICE_MONITOR_CONFIG.copy() if error else config


def save_service_monitor_config(config: dict[str, Any]) -> None:
    atomic_write_text(
        SERVICE_MONITOR_CONFIG_PATH,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


def _first_line(result: CommandResult) -> str:
    output = (result.output or result.stdout or "").strip()
    return output.splitlines()[0] if output else ""


def get_service_state(service: str) -> dict[str, str]:
    normalized, error = normalize_service_name(service)
    if error:
        return {
            "name": service,
            "status": "unknown",
            "level": "error",
            "enabled": "unknown",
            "enabled_label": "未知",
            "enabled_level": "error",
        }

    active_result = run_command(["systemctl", "is-active", normalized], timeout=8)
    state = _first_line(active_result) or "unknown"
    level = "ok" if state == "active" else "error"

    enabled_result = run_command(["systemctl", "is-enabled", normalized], timeout=8)
    enabled = _first_line(enabled_result) or "unknown"
    if enabled in ENABLED_OK:
        enabled_level = "ok"
    elif enabled in ENABLED_WARNING:
        enabled_level = "warning"
    else:
        enabled_level = "error"

    return {
        "name": normalized,
        "status": state,
        "level": level,
        "enabled": enabled,
        "enabled_label": ENABLED_LABELS.get(enabled, enabled),
        "enabled_level": enabled_level,
    }


def gather_service_monitor_status() -> dict[str, Any]:
    config = load_service_monitor_config()
    services = [get_service_state(service) for service in config["services"]]
    return {"services": services, "errors": []}


def run_service_action(service: str, action: str) -> CommandResult:
    normalized, error = normalize_service_name(service)
    if error:
        return CommandResult(False, error)
    if action not in SERVICE_ACTIONS:
        return CommandResult(False, "不支持的服务操作")
    return run_command(["systemctl", action, normalized], timeout=30)


__all__ = [
    "DEFAULT_SERVICE_MONITOR_CONFIG",
    "ENABLED_LABELS",
    "SERVICE_MONITOR_CONFIG_PATH",
    "gather_service_monitor_status",
    "load_service_monitor_config",
    "normalize_service_list",
    "normalize_service_monitor_config",
    "normalize_service_name",
    "run_service_action",
    "save_service_monitor_config",
]
