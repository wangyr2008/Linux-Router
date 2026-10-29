from __future__ import annotations

import time
from typing import Any

from .core import normalize_mac_address


def split_escaped(line: str, separator: str = ":") -> list[str]:
    values: list[str] = []
    current: list[str] = []
    escaped = False

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == separator:
            values.append("".join(current))
            current = []
            continue

        current.append(char)

    values.append("".join(current))
    return values


def translate_device_state(value: str) -> str:
    mapping = {
        "connected": "已连接",
        "connected (externally)": "外部已连接",
        "disconnected": "未连接",
        "connecting": "连接中",
        "unavailable": "不可用",
        "unmanaged": "未托管",
    }
    return mapping.get(value, value or "未知")


def translate_connection_type(value: str) -> str:
    mapping = {
        "802-3-ethernet": "有线",
        "802-11-wireless": "无线",
        "wireguard": "WireGuard",
        "vpn": "VPN",
        "pppoe": "PPPoE",
    }
    return mapping.get(value, value or "未知")


def parse_csv_values(raw: str) -> list[str]:
    if not raw:
        return []
    values = raw.replace(";", ",").split(",")
    return [value.strip() for value in values if value.strip()]


def format_lease_expiry(timestamp: str) -> dict[str, str]:
    """Turn a dnsmasq lease expiry (unix time) into something readable."""
    try:
        expires_at = int(timestamp)
    except (TypeError, ValueError):
        return {"expires_at": "", "expires_label": "未知", "permanent": "0"}
    if expires_at == 0:
        return {"expires_at": "", "expires_label": "永久", "permanent": "1"}
    remaining = expires_at - int(time.time())
    if remaining <= 0:
        return {"expires_at": str(expires_at), "expires_label": "已过期", "permanent": "0"}
    hours, remainder = divmod(remaining, 3600)
    minutes = remainder // 60
    if hours >= 24:
        label = f"{hours // 24} 天 {hours % 24} 小时"
    elif hours:
        label = f"{hours} 小时 {minutes} 分钟"
    else:
        label = f"{minutes} 分钟"
    return {"expires_at": str(expires_at), "expires_label": label, "permanent": "0"}


def parse_dnsmasq_leases(raw: str) -> list[dict[str, Any]]:
    """Parse /var/lib/misc/dnsmasq.leases.

    IPv4 lines look like: ``1750000000 aa:bb:cc:dd:ee:ff 192.168.50.100 hostname clientid``
    IPv6 lines replace the MAC with the DHCPv6 DUID and carry an IPv6 address, so the
    second column is only a MAC address when it really parses as one.
    """
    leases: list[dict[str, Any]] = []
    for line in raw.splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        expiry, identifier, ip_address = fields[0], fields[1], fields[2]
        hostname = fields[3] if len(fields) > 3 and fields[3] not in {"", "*"} else ""
        family = "ipv6" if ":" in ip_address else "ipv4"
        mac_address = normalize_mac_address(identifier) if family == "ipv4" else ""
        leases.append(
            {
                "family": family,
                "ip_address": ip_address,
                "mac_address": mac_address,
                "duid": "" if family == "ipv4" else identifier,
                "hostname": hostname,
                **format_lease_expiry(expiry),
            }
        )
    return leases


__all__ = [
    "split_escaped",
    "translate_device_state",
    "translate_connection_type",
    "parse_csv_values",
    "format_lease_expiry",
    "parse_dnsmasq_leases",
]
