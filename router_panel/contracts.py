from __future__ import annotations

from typing import Any, TypedDict


class InterfaceStatus(TypedDict):
    interface: str
    state: str
    state_label: str
    connection: str
    link_speed: str
    ipv4: list[str]
    ipv6: list[str]
    gateway: str
    gateway6: str
    dns: list[str]
    online: bool


class RouterStatus(TypedDict):
    config: dict[str, Any]
    interfaces: list[dict[str, Any]]
    wan: InterfaceStatus
    lan: InterfaceStatus
    ipv6: dict[str, Any]
    dhcp: dict[str, Any]
    firewall: dict[str, Any]
    forwarding: dict[str, str]
    services: dict[str, dict[str, Any]]
    os_name: str
    errors: list[str]


class ClientStatus(TypedDict):
    clients: list[dict[str, Any]]
    total: int
    online: int
    lan_interface: str
    errors: list[str]


class WiredStatus(TypedDict):
    devices: list[dict[str, Any]]
    errors: list[str]


class SystemInfo(TypedDict, total=False):
    hostname: str
    os_name: str
    kernel: str
    architecture: str
    uptime: str
    load_average: str
    cpu_model: str
    cpu_temperature: str
    memory_total: str
    memory_available: str
    disk_total: str
    disk_free: str
    ip_addresses: list[str]
    active_connections: list[dict[str, Any]]
    network_status: dict[str, bool]
    network_interfaces: list[dict[str, Any]]


__all__ = [
    "InterfaceStatus",
    "RouterStatus",
    "ClientStatus",
    "WiredStatus",
    "SystemInfo",
]
