from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .core import normalize_mac_address, read_text, run_command
from .network_parsers import translate_connection_type, translate_device_state

ETHERNET_TYPE = "802-3-ethernet"
PPP_TYPE = "ppp"
BRIDGE_PREFIX = "br-"


def is_bridge(ifname: str) -> bool:
    """Bridges are never a WAN candidate: the LAN bridge is our own construct."""
    if not ifname:
        return False
    if ifname.startswith(BRIDGE_PREFIX):
        return True
    return Path(f"/sys/class/net/{ifname}/bridge").is_dir()


def link_type(ifname: str) -> str:
    """Coarse device type without asking any network manager."""
    if not ifname:
        return ""
    if ifname.startswith(("ppp", "tun", "tap", "wg")):
        return PPP_TYPE if ifname.startswith("ppp") else ifname.rstrip("0123456789") or "tunnel"
    if Path(f"/sys/class/net/{ifname}/bridge").exists():
        return "bridge"
    if Path(f"/sys/class/net/{ifname}/device").exists():
        return ETHERNET_TYPE
    return "unknown"


def _ip_json(*args: str) -> list[dict[str, Any]]:
    result = run_command(["ip", "-j", *args], timeout=8)
    if not result.ok or not result.output:
        return []
    try:
        payload = json.loads(result.output)
    except json.JSONDecodeError:
        return []
    return payload if isinstance(payload, list) else []


def _addresses(interface: dict[str, Any]) -> tuple[list[str], list[str]]:
    ipv4: list[str] = []
    ipv6: list[str] = []
    for address in interface.get("addr_info", []):
        local = str(address.get("local", "") or "")
        if not local or address.get("scope") == "host":
            continue
        prefixlen = address.get("prefixlen")
        formatted = f"{local}/{prefixlen}" if prefixlen is not None else local
        if address.get("family") == "inet":
            ipv4.append(formatted)
        elif address.get("family") == "inet6" and not local.lower().startswith("fe80:"):
            ipv6.append(formatted)
    return ipv4, ipv6


def _operstate(interface: dict[str, Any]) -> str:
    state = str(interface.get("operstate", "") or "").lower()
    if state in {"up", "unknown"}:
        return "connected"
    return "disconnected"


def get_active_connections() -> list[dict[str, str]]:
    """One entry per interface that currently has an address."""
    items: list[dict[str, str]] = []
    for interface in _ip_json("address", "show"):
        ifname = str(interface.get("ifname", "") or "")
        if not ifname or ifname == "lo":
            continue
        ipv4, ipv6 = _addresses(interface)
        if not ipv4 and not ipv6:
            continue
        kind = link_type(ifname)
        items.append(
            {
                "name": ifname,
                "type": kind,
                "device": ifname,
                "type_label": translate_connection_type(kind),
                "ipv4_addresses": ipv4,
                "ipv6_addresses": ipv6,
            }
        )
    return items


def get_device_status_item(ifname: str) -> dict[str, str]:
    if not ifname:
        return {}
    for interface in _ip_json("address", "show", "dev", ifname):
        kind = link_type(ifname)
        state = _operstate(interface)
        return {
            "device": ifname,
            "type": kind,
            "state": state,
            "connection": ifname if state == "connected" else "",
        }
    return {}


def get_filtered_device_status(allowed_types: set[str]) -> tuple[list[dict[str, str]], list[str]]:
    devices: list[dict[str, str]] = []
    for interface in _ip_json("address", "show"):
        ifname = str(interface.get("ifname", "") or "")
        if not ifname or ifname == "lo":
            continue
        kind = link_type(ifname)
        if allowed_types and kind not in allowed_types:
            continue
        state = _operstate(interface)
        devices.append(
            {
                "device": ifname,
                "type": kind,
                "state": state,
                "connection": ifname if state == "connected" else "",
            }
        )
    return devices, []


def get_default_route_devs() -> dict[str, str]:
    """Interface holding the IPv4 / IPv6 default route (ppp0 for PPPoE uplinks)."""
    devs: dict[str, str] = {}
    for family in ("-4", "-6"):
        result = run_command(["ip", family, "route", "show", "default"], timeout=5)
        if not result.ok or not result.output:
            continue
        for line in result.output.splitlines():
            parts = line.split()
            for index, part in enumerate(parts):
                if part == "dev" and index + 1 < len(parts):
                    devs[family] = parts[index + 1].strip()
                    break
            if family in devs:
                break
    return devs


def get_device_details(ifname: str) -> dict[str, Any]:
    """Layer-3 details of one interface, including both address families."""
    details: dict[str, Any] = {
        "mac": "",
        "ipv4": [],
        "ipv6": [],
        "gateway": "",
        "gateway6": "",
        "dns": [],
    }
    if not ifname:
        return details

    for interface in _ip_json("address", "show", "dev", ifname):
        details["mac"] = str(interface.get("address", "") or "")
        details["ipv4"], details["ipv6"] = _addresses(interface)
        # On a point-to-point link (ppp0) the peer sits in `address` while
        # `local` is our own address; that peer is the gateway.
        for address in interface.get("addr_info", []):
            local = str(address.get("local", "") or "")
            peer = str(address.get("address", "") or "")
            if address.get("family") == "inet" and peer and peer != local:
                details["peer"] = peer

    for family, key in (("-4", "gateway"), ("-6", "gateway6")):
        result = run_command(["ip", family, "route", "show", "default", "dev", ifname], timeout=5)
        if not result.ok or not result.output:
            continue
        for line in result.output.splitlines():
            parts = line.split()
            if "via" in parts:
                index = parts.index("via")
                if index + 1 < len(parts):
                    details[key] = parts[index + 1]
                    break

    # `default dev ppp0` has no `via`, but the peer still is the next hop.
    if not details["gateway"] and details.get("peer"):
        details["gateway"] = details["peer"]

    details["dns"] = get_dns_servers()
    return details


def get_dns_servers() -> list[str]:
    servers: list[str] = []
    try:
        content = Path("/etc/resolv.conf").read_text(encoding="utf-8")
    except OSError:
        return servers
    for line in content.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "nameserver" and parts[1] not in servers:
            servers.append(parts[1])
    return servers


def get_interface_addresses(ifname: str) -> dict[str, list[str]]:
    """Addresses straight from the kernel, so the UI works with any backend."""
    addresses: dict[str, list[str]] = {"ipv4": [], "ipv6": []}
    for interface in _ip_json("address", "show", "dev", ifname):
        addresses["ipv4"], addresses["ipv6"] = _addresses(interface)
    return addresses


def get_interface_neighbors(ifname: str) -> list[dict[str, str]]:
    """Neighbour (ARP/NDP) table entries for one interface, both families."""
    neighbors: list[dict[str, str]] = []
    for family, flag in (("ipv4", "-4"), ("ipv6", "-6")):
        result = run_command(["ip", flag, "neighbor", "show", "dev", ifname], timeout=5)
        if not result.ok or not result.output:
            continue
        for line in result.output.splitlines():
            parts = line.split()
            if not parts or "lladdr" not in parts:
                continue
            if family == "ipv6" and parts[0].lower().startswith("fe80:"):
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
    if state == "connected" and carrier == "0":
        return "disconnected"
    return state


def list_all_interfaces() -> list[dict[str, Any]]:
    """Every interface except loopback, for the WAN selector.

    A PPPoE uplink is ``ppp0``, which is not an ethernet device and therefore
    never shows up in the wired listing.
    """
    default_devs = set(get_default_route_devs().values())
    items: list[dict[str, Any]] = []
    for interface in _ip_json("address", "show"):
        ifname = str(interface.get("ifname", "") or "")
        if not ifname or ifname == "lo" or is_bridge(ifname):
            continue
        ipv4, ipv6 = _addresses(interface)
        state = _operstate(interface)
        items.append(
            {
                "name": ifname,
                "state": state,
                "state_label": translate_device_state(state),
                "kind": str(interface.get("link_type", "")),
                "ipv4": ipv4,
                "ipv6": ipv6,
                "default_route": ifname in default_devs,
            }
        )
    items.sort(key=lambda item: (not item["default_route"], item["name"]))
    return items


def gather_wired_network_info(router_config: dict[str, Any] | None = None) -> dict[str, Any]:
    router_config = router_config or {}
    wan_interface = str(router_config.get("wan_interface", "") or "")
    lan_interface = str(router_config.get("lan_interface", "") or "")
    lan_ports = [str(item) for item in (router_config.get("lan_ports") or []) if str(item).strip()]
    pppoe_nic = str(router_config.get("pppoe_nic", "") or "")

    devices: list[dict[str, Any]] = []
    for interface in _ip_json("address", "show"):
        ifname = str(interface.get("ifname", "") or "")
        if not ifname or ifname == "lo":
            continue
        if link_type(ifname) != ETHERNET_TYPE:
            continue

        ipv4, ipv6 = _addresses(interface)
        carrier = get_wired_carrier(ifname)
        state = normalize_wired_state(_operstate(interface), carrier)
        speed = "" if carrier == "0" else get_wired_sysfs_speed(ifname)

        if ifname and ifname == pppoe_nic:
            role, role_label = "pppoe", "PPPoE 物理口"
        elif ifname and (ifname == lan_interface or ifname in lan_ports):
            bridged = len(lan_ports) > 1
            role, role_label = "lan", ("LAN 桥接成员" if bridged else "LAN 局域网")
        elif ifname and ifname == wan_interface:
            role, role_label = "wan", "WAN 上行"
        else:
            role, role_label = "", "未分配"

        gateway = ""
        gateway6 = ""
        for family, key in (("-4", "gateway"), ("-6", "gateway6")):
            result = run_command(
                ["ip", family, "route", "show", "default", "dev", ifname], timeout=5
            )
            if result.ok and result.output:
                parts = result.output.splitlines()[0].split()
                if "via" in parts:
                    index = parts.index("via")
                    if index + 1 < len(parts):
                        if key == "gateway":
                            gateway = parts[index + 1]
                        else:
                            gateway6 = parts[index + 1]

        # A bridge member or the PPPoE NIC carries no address of its own: the
        # address lives on br-lan / ppp0. Show that instead of an empty card.
        carried_by = ""
        if not ipv4 and not ipv6:
            if role == "lan" and lan_interface and lan_interface != ifname:
                carried_by = lan_interface
            elif role == "pppoe" and wan_interface:
                carried_by = wan_interface
        if carried_by:
            carried = get_device_details(carried_by)
            ipv4 = carried["ipv4"] or ipv4
            ipv6 = carried["ipv6"] or ipv6
            gateway = carried["gateway"] or gateway
            gateway6 = carried["gateway6"] or gateway6

        devices.append(
            {
                "device": ifname,
                "role": role,
                "role_label": role_label,
                "carried_by": carried_by,
                "state": state,
                "state_label": translate_device_state(state),
                "connection": ifname if state == "connected" else "未连接",
                "details": {
                    "mac": str(interface.get("address", "") or ""),
                    "carrier": carrier,
                    "link_speed": format_link_speed(speed),
                    "ipv4": ipv4,
                    "ipv6": ipv6,
                    "gateway": gateway,
                    "gateway6": gateway6,
                    "dns": get_dns_servers(),
                },
            }
        )

    devices.sort(key=lambda item: item["device"])
    return {"devices": devices, "errors": []}


__all__ = [
    "ETHERNET_TYPE",
    "PPP_TYPE",
    "link_type",
    "get_active_connections",
    "get_device_status_item",
    "get_filtered_device_status",
    "get_device_details",
    "get_interface_addresses",
    "get_interface_neighbors",
    "get_default_route_devs",
    "is_bridge",
    "list_all_interfaces",
    "get_dns_servers",
    "format_link_speed",
    "gather_wired_network_info",
]
