from __future__ import annotations

import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor

from .core import (
    SYSTEM_STATIC_CACHE_TTL,
    format_bytes,
    format_uptime,
    get_cpu_model,
    get_cpu_temperature,
    get_network_interface_hardware,
    get_timed_cache,
    load_os_release,
    read_text,
    run_command,
    set_timed_cache,
)
from .contracts import SystemInfo
from .network import get_active_connections
from .routing import load_router_config


def summarize_network_status(
    active_connections: list[dict[str, str]],
    router_config: dict | None = None,
) -> dict[str, bool]:
    router_config = router_config or {}
    wan_interface = str(router_config.get("wan_interface", "") or "")
    lan_interface = str(router_config.get("lan_interface", "") or "")
    ethernet_devices = {
        connection.get("device", "")
        for connection in active_connections
        if connection.get("type") == "802-3-ethernet"
    }
    return {
        "wan": bool(wan_interface and wan_interface in ethernet_devices),
        "lan": bool(lan_interface and lan_interface in ethernet_devices),
        "wired": bool(ethernet_devices),
    }


def get_static_system_info() -> dict[str, str]:
    cached = get_timed_cache("system:static", SYSTEM_STATIC_CACHE_TTL)
    if cached is not None:
        return cached.copy()

    os_release = load_os_release()
    uname = os.uname()
    meminfo = read_text("/proc/meminfo")
    mem_values: dict[str, int] = {}
    for line in meminfo.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        mem_values[key] = int(value.strip().split()[0]) * 1024

    stat = os.statvfs("/")
    disk_total = stat.f_frsize * stat.f_blocks

    static_info = {
        "hostname": socket.gethostname(),
        "os_name": os_release.get("PRETTY_NAME", "未知"),
        "kernel": uname.release,
        "architecture": uname.machine,
        "cpu_model": get_cpu_model(),
        "memory_total": format_bytes(mem_values.get("MemTotal", 0)),
        "disk_total": format_bytes(disk_total),
    }
    set_timed_cache("system:static", SYSTEM_STATIC_CACHE_TTL, static_info.copy())
    return static_info


def get_interface_addresses() -> tuple[list[str], dict[str, dict[str, list[str]]]]:
    result = run_command(["ip", "-j", "address", "show"])
    if not result.ok or not result.output:
        return [], {}

    try:
        interfaces = json.loads(result.output)
    except json.JSONDecodeError:
        return [], {}

    ip_addresses: list[str] = []
    addresses_by_interface: dict[str, dict[str, list[str]]] = {}
    for interface in interfaces:
        ifname = str(interface.get("ifname", "")).strip()
        if not ifname or ifname == "lo":
            continue

        for address in interface.get("addr_info", []):
            local = str(address.get("local", "")).strip()
            family = address.get("family", "")
            scope = address.get("scope", "")
            if not local:
                continue

            if scope == "global" and local not in ip_addresses:
                ip_addresses.append(local)

            if scope == "host":
                continue
            prefixlen = address.get("prefixlen")
            formatted = f"{local}/{prefixlen}" if prefixlen is not None else local
            bucket = addresses_by_interface.setdefault(ifname, {"ipv4": [], "ipv6": []})
            if family == "inet":
                bucket["ipv4"].append(formatted)
            elif family == "inet6" and not local.lower().startswith("fe80:"):
                bucket["ipv6"].append(formatted)

    return ip_addresses, addresses_by_interface


def gather_system_info() -> SystemInfo:
    meminfo = read_text("/proc/meminfo")
    mem_values: dict[str, int] = {}
    for line in meminfo.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        mem_values[key] = int(value.strip().split()[0]) * 1024

    stat = os.statvfs("/")
    disk_free = stat.f_frsize * stat.f_bavail
    load_averages = os.getloadavg()

    with ThreadPoolExecutor(max_workers=4) as executor:
        static_info_future = executor.submit(get_static_system_info)
        active_connections_future = executor.submit(get_active_connections)
        interface_addresses_future = executor.submit(get_interface_addresses)
        network_interfaces_future = executor.submit(get_network_interface_hardware)

        static_info = static_info_future.result()
        active_connections = active_connections_future.result()
        ip_addresses, addresses_by_interface = interface_addresses_future.result()
        network_interfaces = network_interfaces_future.result()

    router_config = load_router_config()
    active_connections = [
        {
            **connection,
            "ipv4_addresses": addresses_by_interface.get(connection.get("device", ""), {}).get("ipv4", []),
            "ipv6_addresses": addresses_by_interface.get(connection.get("device", ""), {}).get("ipv6", []),
        }
        for connection in active_connections
    ]

    return {
        **static_info,
        "uptime": format_uptime(read_text("/proc/uptime")),
        "load_average": ", ".join(f"{value:.2f}" for value in load_averages),
        "cpu_temperature": get_cpu_temperature(),
        "memory_available": format_bytes(mem_values.get("MemAvailable", 0)),
        "disk_free": format_bytes(disk_free),
        "ip_addresses": ip_addresses,
        "active_connections": active_connections,
        "network_status": summarize_network_status(active_connections, router_config),
        "network_interfaces": network_interfaces,
    }


__all__ = [
    "get_static_system_info",
    "get_interface_addresses",
    "summarize_network_status",
    "gather_system_info",
]
