from __future__ import annotations

import json
from typing import Any

from .core import (
    get_network_interface_hardware,
    normalize_mac_address,
    read_text,
    run_command,
)
from .network_parsers import (
    normalize_nmcli_general_state,
    parse_nmcli_lines,
    translate_connection_type,
    translate_device_state,
)


def get_active_connections() -> list[dict[str, str]]:
    active_connections = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "NAME,TYPE,DEVICE",
            "connection",
            "show",
            "--active",
        ]
    )
    active_items = (
        parse_nmcli_lines(active_connections.output, ["name", "type", "device"])
        if active_connections.ok and active_connections.output
        else []
    )
    return [
        {
            **item,
            "type_label": translate_connection_type(item.get("type", "")),
        }
        for item in active_items
        if item.get("device") != "lo"
    ]


def get_device_status_item(ifname: str) -> dict[str, str]:
    if not ifname:
        return {}

    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        ]
    )
    if not result.ok or not result.output:
        return {}

    for item in parse_nmcli_lines(result.output, ["device", "type", "state", "connection"]):
        if item.get("device") == ifname:
            return item
    return {}


def get_filtered_device_status(allowed_types: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "DEVICE,TYPE,STATE,CONNECTION",
            "device",
            "status",
        ]
    )
    if not result.ok or not result.output:
        return [], [result.output or "无法读取网络状态"]

    devices: list[dict[str, str]] = []
    for item in parse_nmcli_lines(result.output, ["device", "type", "state", "connection"]):
        device_type = item.get("type", "").strip()
        device_name = item.get("device", "").strip()
        if device_type not in allowed_types:
            continue
        if not device_name or device_name == "lo" or device_name.startswith("p2p-dev-"):
            continue
        devices.append(item)
    return devices, []


def get_device_details(ifname: str) -> dict[str, Any]:
    """Read layer-3 details of one interface, including both address families."""
    empty: dict[str, Any] = {
        "mac": "",
        "ipv4": [],
        "ipv6": [],
        "gateway": "",
        "gateway6": "",
        "dns": [],
    }
    if not ifname:
        return empty

    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.GATEWAY,IP6.DNS",
            "device",
            "show",
            ifname,
        ]
    )
    details = dict(empty)
    details["ipv4"] = []
    details["ipv6"] = []
    details["dns"] = []
    if not result.ok or not result.output:
        return details

    for line in result.output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        if not value or value == "--":
            continue
        if key == "GENERAL.HWADDR":
            details["mac"] = value
        elif key.startswith("IP4.ADDRESS"):
            details["ipv4"].append(value)
        elif key == "IP4.GATEWAY":
            details["gateway"] = value
        elif key.startswith("IP4.DNS"):
            if value not in details["dns"]:
                details["dns"].append(value)
        elif key.startswith("IP6.ADDRESS"):
            # Skip IPv6 link-local addresses, they are noise in the UI.
            if value.lower().startswith("fe80:"):
                continue
            details["ipv6"].append(value)
        elif key == "IP6.GATEWAY":
            details["gateway6"] = value
        elif key.startswith("IP6.DNS"):
            if value not in details["dns"]:
                details["dns"].append(value)
    return details


def get_interface_addresses(ifname: str) -> dict[str, list[str]]:
    """Addresses straight from the kernel, so the UI still works when NetworkManager
    is not managing the interface (for example a LAN interface configured by hand)."""
    result = run_command(["ip", "-j", "address", "show", "dev", ifname], timeout=5)
    addresses: dict[str, list[str]] = {"ipv4": [], "ipv6": []}
    if not result.ok or not result.output:
        return addresses

    try:
        interfaces = json.loads(result.output)
    except json.JSONDecodeError:
        return addresses

    for interface in interfaces:
        for address in interface.get("addr_info", []):
            local = str(address.get("local", "")).strip()
            family = address.get("family", "")
            if not local or address.get("scope") == "host":
                continue
            prefixlen = address.get("prefixlen")
            formatted = f"{local}/{prefixlen}" if prefixlen is not None else local
            if family == "inet":
                addresses["ipv4"].append(formatted)
            elif family == "inet6":
                if local.lower().startswith("fe80:"):
                    continue
                addresses["ipv6"].append(formatted)
    return addresses


def get_interface_neighbors(ifname: str) -> list[dict[str, str]]:
    """Neighbour (ARP/NDP) table entries for one interface, both families."""
    neighbors: list[dict[str, str]] = []
    for family, flag in (("ipv4", "-4"), ("ipv6", "-6")):
        result = run_command([flag, "neighbor", "show", "dev", ifname], timeout=5)
        if not result.ok or not result.output:
            continue
        for line in result.output.splitlines():
            parts = line.split()
            if not parts or "lladdr" not in parts:
                continue
            if family == "ipv6" and parts[0].lower().startswith("fe80:"):
                # Link-local neighbours are noise in the device list.
                continue
            try:
                mac_index = parts.index("lladdr") + 1
                mac_address = normalize_mac_address(parts[mac_index])
            except (ValueError, IndexError):
                continue
            if not mac_address:
                continue
            state = parts[-1].lower()
            if state in {"failed", "incomplete"}:
                continue
            neighbors.append(
                {
                    "family": family,
                    "ip_address": parts[0],
                    "mac_address": mac_address,
                    "state": state,
                    "online": "1" if state in {"reachable", "delay", "probe", "permanent"} else "0",
                }
            )
    return neighbors


def default_wired_profile(ifname: str) -> dict[str, Any]:
    return {
        "name": "",
        "active_device": ifname,
        "interface_name": ifname,
        "autoconnect": True,
        "ipv4_method": "auto",
        "ipv4_address": "",
        "ipv4_gateway": "",
        "ipv4_dns": "",
        "ipv6_method": "auto",
        "ipv6_address": "",
        "route_metric": "-1",
    }


def parse_link_speed_mbps(value: str) -> int:
    raw = value.strip()
    if not raw or raw in {"--", "-1"}:
        return 0
    try:
        speed = int(float(raw.split()[0]))
    except (ValueError, IndexError):
        return 0
    return speed if speed > 0 else 0


def format_link_speed(value: str) -> str:
    speed_mbps = parse_link_speed_mbps(value)
    if not speed_mbps:
        return "未知"
    if speed_mbps >= 1000:
        speed_gbps = speed_mbps / 1000
        return f"{speed_gbps:g} Gbps"
    return f"{speed_mbps} Mbps"


def get_wired_carrier(ifname: str) -> str:
    carrier = read_text(f"/sys/class/net/{ifname}/carrier")
    return carrier if carrier in {"0", "1"} else ""


def get_wired_sysfs_speed(ifname: str) -> str:
    return read_text(f"/sys/class/net/{ifname}/speed")


def normalize_wired_state(state: str, carrier: str) -> str:
    if state == "unavailable" and carrier == "0":
        return "disconnected"
    return state


def translate_ipv4_method(value: str) -> str:
    method = value.strip().lower()
    if method == "manual":
        return "静态地址"
    if method == "auto":
        return "DHCP"
    if method == "shared":
        return "共享"
    if method == "disabled":
        return "未启用"
    return "未知"


def translate_ipv6_method(value: str) -> str:
    method = value.strip().lower()
    if method == "manual":
        return "静态地址"
    if method in {"auto", "dhcp"}:
        return "自动 (SLAAC/DHCPv6)"
    if method == "disabled":
        return "未启用"
    if method == "link-local":
        return "仅链路本地"
    if method == "ignore":
        return "忽略"
    return "未知"


def get_all_connection_methods() -> dict[str, tuple[str, str]]:
    """One nmcli call for every profile, so page loads stay fast on multi-NIC hosts."""
    result = run_command(
        ["nmcli", "-t", "-f", "NAME,IPV4.METHOD,IPV6.METHOD", "connection", "show"],
        timeout=8,
    )
    methods: dict[str, tuple[str, str]] = {}
    if not result.ok or not result.output:
        return methods
    for item in parse_nmcli_lines(result.output, ["name", "ipv4", "ipv6"]):
        name = item.get("name", "").strip()
        if name:
            methods[name] = (item.get("ipv4", "").strip(), item.get("ipv6", "").strip())
    return methods


def get_connection_methods(connection_name: str) -> tuple[str, str]:
    if not connection_name:
        return "", ""
    return get_all_connection_methods().get(connection_name, ("", ""))


def gather_wired_network_info(router_config: dict[str, Any] | None = None) -> dict[str, Any]:
    result = run_command(
        [
            "nmcli",
            "-t",
            "-f",
            (
                "GENERAL.DEVICE,GENERAL.TYPE,GENERAL.STATE,GENERAL.CONNECTION,"
                "GENERAL.HWADDR,IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,IP6.ADDRESS,IP6.GATEWAY"
            ),
            "device",
            "show",
        ]
    )
    if not result.ok or not result.output:
        return {
            "devices": [],
            "errors": [result.output or "无法读取有线网络状态"],
        }

    raw_devices: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for line in result.output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()

        if key == "GENERAL.DEVICE":
            if current:
                raw_devices.append(current)
            current = {
                "device": value,
                "type": "",
                "state": "",
                "connection": "",
                "mac": "",
                "ipv4": [],
                "ipv6": [],
                "gateway": "",
                "gateway6": "",
                "dns": [],
            }
            continue
        if current is None:
            continue

        if key == "GENERAL.TYPE":
            current["type"] = value
        elif key == "GENERAL.STATE":
            current["state"] = normalize_nmcli_general_state(value)
        elif key == "GENERAL.CONNECTION":
            current["connection"] = "" if value == "--" else value
        elif key == "GENERAL.HWADDR":
            current["mac"] = value
        elif key.startswith("IP4.ADDRESS"):
            current["ipv4"].append(value)
        elif key == "IP4.GATEWAY":
            current["gateway"] = value
        elif key.startswith("IP4.DNS"):
            if value and value not in current["dns"]:
                current["dns"].append(value)
        elif key.startswith("IP6.ADDRESS"):
            if value and not value.lower().startswith("fe80:"):
                current["ipv6"].append(value)
        elif key == "IP6.GATEWAY":
            current["gateway6"] = value
    if current:
        raw_devices.append(current)

    router_config = router_config or {}
    wan_interface = str(router_config.get("wan_interface", "") or "")
    lan_interface = str(router_config.get("lan_interface", "") or "")
    connection_methods = get_all_connection_methods()

    devices: list[dict[str, Any]] = []
    for device in sorted(raw_devices, key=lambda value: value.get("device", "")):
        if device.get("type") != "ethernet":
            continue
        ifname = device.get("device", "")
        connection_name = device.get("connection", "")
        carrier = get_wired_carrier(ifname)
        state = normalize_wired_state(device.get("state", ""), carrier)
        speed = "" if carrier == "0" else get_wired_sysfs_speed(ifname)
        profile = default_wired_profile(ifname)
        profile["name"] = connection_name
        ipv4_method, ipv6_method = connection_methods.get(connection_name, ("", ""))
        profile["ipv4_method"] = ipv4_method
        profile["ipv6_method"] = ipv6_method
        profile["ipv4_method_label"] = translate_ipv4_method(ipv4_method)
        profile["ipv6_method_label"] = translate_ipv6_method(ipv6_method)

        if ifname and ifname == wan_interface:
            role = "wan"
            role_label = "WAN 上行"
        elif ifname and ifname == lan_interface:
            role = "lan"
            role_label = "LAN 局域网"
        else:
            role = ""
            role_label = "未分配"

        devices.append(
            {
                "device": ifname,
                "role": role,
                "role_label": role_label,
                "state": state,
                "state_label": translate_device_state(state),
                "connection": connection_name or "未连接",
                "details": {
                    "mac": device.get("mac", ""),
                    "carrier": carrier,
                    "link_speed": format_link_speed(speed),
                    "ipv4": device.get("ipv4", []),
                    "ipv6": device.get("ipv6", []),
                    "gateway": device.get("gateway", ""),
                    "gateway6": device.get("gateway6", ""),
                    "dns": device.get("dns", []),
                },
                "profile": profile,
            }
        )

    return {
        "devices": devices,
        "errors": [],
    }


__all__ = [
    "get_active_connections",
    "get_device_status_item",
    "get_filtered_device_status",
    "get_device_details",
    "get_interface_addresses",
    "get_interface_neighbors",
    "get_all_connection_methods",
    "get_connection_methods",
    "default_wired_profile",
    "format_link_speed",
    "translate_ipv4_method",
    "translate_ipv6_method",
    "gather_wired_network_info",
]
