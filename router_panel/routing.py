from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import secrets
from pathlib import Path
from typing import Any, Callable

from .core import (
    DEFAULT_LAN_NETWORK,
    DNSMASQ_CONFIG_PATH,
    DNSMASQ_CONF_DIR,
    DNSMASQ_LEASE_PATH,
    DNSMASQ_RULESET_PATH,
    LEGACY_NETWORK_CONFIG_PATH,
    ROUTER_CONFIG_PATH,
    ROUTER_PANEL_SYSCTL_PATH,
    SYSTEM_COMMAND_TIMEOUT,
    CommandResult,
    atomic_write_text,
    command_exists,
    is_service_active,
    is_service_enabled,
    load_os_release,
    normalize_lan_network,
    normalize_mac_address,
    read_text,
    run_command,
)
from .cake import apply_cake, cake_enabled, normalize_bandwidth
from .firewall import (
    FIREWALL_MODE_OPEN,
    FIREWALL_MODES,
    apply_ruleset,
    get_firewall_status,
    parse_port_list,
    render_nftables_ruleset,
)
from .network import (
    gather_wired_network_info,
    get_device_details,
    get_interface_addresses,
    get_interface_neighbors,
    list_all_interfaces,
)
from .network_parsers import parse_dnsmasq_leases
from . import networkd

ProgressCallback = Callable[[str], None]

IFNAME_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")
LEASE_RE = re.compile(r"^[0-9]+[smhd]?$")
IPV6_MODE_OFF = "off"
IPV6_MODE_ULA = "ula"
IPV6_MODE_STATIC = "static"
IPV6_MODE_PD = "pd"
IPV6_MODES = (IPV6_MODE_ULA, IPV6_MODE_PD, IPV6_MODE_STATIC, IPV6_MODE_OFF)
IPV6_MODE_LABELS = {
    IPV6_MODE_ULA: "ULA 私有前缀 + NAT66",
    IPV6_MODE_PD: "运营商 PD 自动（dhcpcd）",
    IPV6_MODE_STATIC: "运营商下发的全局前缀（纯路由）",
    IPV6_MODE_OFF: "关闭 IPv6",
}

DEFAULT_ROUTER_CONFIG: dict[str, Any] = {
    "wan_interface": "",
    "lan_interface": "",
    "wan_mode": networkd.WAN_MODE_DHCP,
    "pppoe_username": "",
    "pppoe_password": "",
    "pppoe_nic": "",
    "lan_network": DEFAULT_LAN_NETWORK,
    "dhcp_enabled": True,
    "dhcp_start": "",
    "dhcp_end": "",
    "dhcp_lease": "12h",
    "dns_upstream": "",
    "ipv6_mode": IPV6_MODE_ULA,
    "ipv6_prefix": "",
    "ipv6_nat": True,
    "wan_ipv6_auto": True,
    "mtu": "",
    "firewall_mode": FIREWALL_MODE_OPEN,
    "firewall_lan_tcp": "22,80",
    "firewall_wan_tcp": "",
    "firewall_wan_udp": "",
    "cake_enabled": False,
    "cake_upload_mbit": 0,
    "cake_download_mbit": 0,
    # [{"mac": "aa:bb:cc:dd:ee:ff", "ip": "192.168.50.10", "hostname": "nas"}]
    "static_leases": [],
    # {"AA:BB:CC:DD:EE:FF": "客厅电视"} - panel-side only, for recognising devices
    "device_labels": {},
}


# ---------------------------------------------------------------------------
# Config handling
# ---------------------------------------------------------------------------


def generate_ula_prefix() -> str:
    """Derive a stable per-machine ULA prefix (RFC 4193 style).

    Using the machine-id keeps the prefix identical across restarts and page loads
    without having to persist anything before the operator saves the form.
    """
    seed = b""
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            seed = Path(path).read_bytes().strip()
        except OSError:
            continue
        if seed:
            break
    digest = hashlib.sha256(seed).digest()[:5] if seed else secrets.token_bytes(5)
    raw = (
        f"fd{digest[0]:02x}:{digest[1]:02x}{digest[2]:02x}:"
        f"{digest[3]:02x}{digest[4]:02x}::/64"
    )
    # Canonical form, so a generated prefix equals the one re-read from the file.
    return str(ipaddress.ip_network(raw))


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default


def _lan_ipv4_network(config: dict[str, Any]) -> ipaddress.IPv4Network:
    network, _ = normalize_lan_network(str(config.get("lan_network", DEFAULT_LAN_NETWORK)))
    return network or ipaddress.IPv4Network(DEFAULT_LAN_NETWORK)


def lan_gateway_ipv4(config: dict[str, Any]) -> ipaddress.IPv4Address:
    return next(_lan_ipv4_network(config).hosts())


def lan_ipv6_network(config: dict[str, Any]) -> ipaddress.IPv6Network | None:
    mode = str(config.get("ipv6_mode", IPV6_MODE_OFF))
    if mode == IPV6_MODE_OFF:
        return None
    raw = str(config.get("ipv6_prefix", "") or "").strip()
    if not raw:
        return None
    try:
        network = ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return None
    if not isinstance(network, ipaddress.IPv6Network):
        return None
    return network


def lan_ipv6_address(config: dict[str, Any]) -> ipaddress.IPv6Address | None:
    network = lan_ipv6_network(config)
    if network is None:
        return None
    return network.network_address + 1


def _dhcp_range(config: dict[str, Any]) -> tuple[str, str]:
    network = _lan_ipv4_network(config)
    first_host = int(network.network_address) + 1
    last_host = int(network.broadcast_address) - 1
    host_count = last_host - first_host + 1
    if host_count < 4:
        return "", ""
    default_start = first_host + host_count // 2 if host_count > 8 else first_host + 1
    start = str(config.get("dhcp_start", "") or "").strip() or str(ipaddress.IPv4Address(default_start))
    end = str(config.get("dhcp_end", "") or "").strip() or str(ipaddress.IPv4Address(last_host))
    return start, end


def _normalize_ipv6_prefix(raw: str, mode: str) -> tuple[str, str | None]:
    value = raw.strip()
    if mode == IPV6_MODE_OFF:
        return "", None
    if mode == IPV6_MODE_PD:
        # The prefix is delegated by the ISP; whatever the form sent is ignored.
        return "", None
    if not value:
        if mode == IPV6_MODE_ULA:
            return generate_ula_prefix(), None
        return "", "选择全局前缀模式时必须填写 IPv6 前缀，例如 2408:8207:1234:5600::/64"
    try:
        network = ipaddress.ip_network(value, strict=False)
    except ValueError:
        return "", f"IPv6 前缀格式无效：{value}"
    if not isinstance(network, ipaddress.IPv6Network):
        return "", "IPv6 前缀必须是 IPv6 网段"
    if network.prefixlen != 64:
        return "", "LAN 的 IPv6 前缀长度必须是 /64"
    if network.is_link_local:
        return "", "IPv6 前缀不能是链路本地地址"
    if network.is_multicast:
        return "", "IPv6 前缀不能是组播地址"
    if mode == IPV6_MODE_STATIC and network.is_private:
        return "", "全局前缀模式下请填写运营商分配的公网 IPv6 前缀（fd00::/8 属于私有 ULA）"
    return str(network), None


def normalize_router_config(
    payload: dict[str, Any],
    existing: dict[str, Any] | None = None,
    available_interfaces: list[str] | None = None,
    available_wan_interfaces: list[str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    base = dict(DEFAULT_ROUTER_CONFIG)
    if existing:
        base.update({key: existing[key] for key in existing if key in DEFAULT_ROUTER_CONFIG})

    def get(name: str) -> str:
        value = payload.get(name, base.get(name, ""))
        if isinstance(value, bool):
            return "1" if value else ""
        return str(value if value is not None else "").strip()

    config: dict[str, Any] = dict(base)

    wan_mode = get("wan_mode").lower() or networkd.WAN_MODE_DHCP
    if wan_mode not in networkd.WAN_MODES:
        return config, "WAN 连接方式无效"

    wan = get("wan_interface")
    lan = get("lan_interface")
    pppoe_nic = get("pppoe_nic")
    pppoe_username = get("pppoe_username")
    pppoe_password = get("pppoe_password")

    if wan_mode == networkd.WAN_MODE_PPPOE:
        if not pppoe_username:
            return config, "PPPoE 账号不能为空"
        if not pppoe_nic or not IFNAME_RE.fullmatch(pppoe_nic):
            return config, "请选择 PPPoE 拨号所用的物理网口"
        if available_interfaces and pppoe_nic not in available_interfaces:
            return config, f"PPPoE 物理网口 {pppoe_nic} 不存在或不是有线网卡"
        if pppoe_nic == lan:
            return config, "PPPoE 物理网口不能和 LAN 是同一块网卡"
        wan = networkd.PPPOE_INTERFACE
    else:
        pppoe_nic = ""
        if wan and not IFNAME_RE.fullmatch(wan):
            return config, f"WAN 接口名称无效：{wan}"
        if available_wan_interfaces and wan and wan not in available_wan_interfaces:
            return config, f"WAN 接口 {wan} 不存在"

    if lan and not IFNAME_RE.fullmatch(lan):
        return config, f"LAN 接口名称无效：{lan}"
    if wan and wan == lan:
        return config, "WAN 和 LAN 不能是同一块网卡"
    if available_interfaces and lan and lan not in available_interfaces:
        return config, f"LAN 接口 {lan} 不存在或不是有线网卡"

    network, error = normalize_lan_network(get("lan_network"))
    if error or network is None:
        return config, error or "LAN 网段无效"

    mode = get("ipv6_mode").lower() or IPV6_MODE_ULA
    if mode not in IPV6_MODES:
        return config, "IPv6 模式无效"

    prefix, error = _normalize_ipv6_prefix(get("ipv6_prefix"), mode)
    if error:
        return config, error

    lease = get("dhcp_lease") or "12h"
    if not LEASE_RE.fullmatch(lease):
        return config, "DHCP 租期格式无效，例如 12h、1d、3600"

    mtu = get("mtu")
    if mtu:
        try:
            mtu_value = int(mtu)
        except ValueError:
            return config, "MTU 必须是数字"
        if not 576 <= mtu_value <= 9000:
            return config, "MTU 必须在 576 到 9000 之间"

    firewall_mode = get("firewall_mode").lower() or FIREWALL_MODE_OPEN
    if firewall_mode not in FIREWALL_MODES:
        return config, "访问控制模式无效"

    lan_tcp, error = parse_port_list(get("firewall_lan_tcp"))
    if error:
        return config, f"LAN 放行端口：{error}"
    wan_tcp, error = parse_port_list(get("firewall_wan_tcp"))
    if error:
        return config, f"WAN 放行 TCP 端口：{error}"
    wan_udp, error = parse_port_list(get("firewall_wan_udp"))
    if error:
        return config, f"WAN 放行 UDP 端口：{error}"

    cake_on = _as_bool(payload.get("cake_enabled", base.get("cake_enabled", False)), False)
    cake_upload, error = normalize_bandwidth(
        get("cake_upload_mbit"), required=cake_on, label="CAKE 上行带宽"
    )
    if error:
        return config, error
    cake_download, error = normalize_bandwidth(
        get("cake_download_mbit"), required=False, label="CAKE 下行带宽"
    )
    if error:
        return config, error

    config.update(
        {
            "wan_interface": wan,
            "lan_interface": lan,
            "wan_mode": wan_mode,
            "pppoe_username": pppoe_username,
            "pppoe_password": pppoe_password,
            "pppoe_nic": pppoe_nic,
            "lan_network": str(network),
            "dhcp_enabled": _as_bool(payload.get("dhcp_enabled", base.get("dhcp_enabled", True)), True),
            "dhcp_start": get("dhcp_start"),
            "dhcp_end": get("dhcp_end"),
            "dhcp_lease": lease,
            "dns_upstream": " ".join(
                part for part in re.split(r"[\s,;]+", get("dns_upstream")) if part
            ),
            "ipv6_mode": mode,
            "ipv6_prefix": prefix,
            "ipv6_nat": _as_bool(payload.get("ipv6_nat", base.get("ipv6_nat", True)), True),
            "wan_ipv6_auto": _as_bool(payload.get("wan_ipv6_auto", base.get("wan_ipv6_auto", True)), True),
            "mtu": mtu,
            "firewall_mode": firewall_mode,
            "firewall_lan_tcp": ",".join(lan_tcp),
            "firewall_wan_tcp": ",".join(wan_tcp),
            "firewall_wan_udp": ",".join(wan_udp),
            "cake_enabled": cake_on,
            "cake_upload_mbit": cake_upload,
            "cake_download_mbit": cake_download,
        }
    )

    # Bindings have their own endpoint instead of this form, so carry them over
    # from the loaded config rather than falling back to the default empty list.
    raw_leases = payload.get("static_leases", base.get("static_leases", []))
    config["static_leases"] = (
        [item for item in raw_leases if isinstance(item, dict)]
        if isinstance(raw_leases, list)
        else []
    )
    config["device_labels"] = normalize_device_labels(
        payload.get("device_labels", base.get("device_labels", {}))
    )

    if config["dhcp_enabled"]:
        start, end = _dhcp_range(config)
        try:
            start_ip = ipaddress.IPv4Address(start)
            end_ip = ipaddress.IPv4Address(end)
        except ValueError:
            return config, "DHCP 地址池格式无效"
        if start_ip not in network or end_ip not in network:
            return config, "DHCP 地址池必须在 LAN 网段内"
        if int(start_ip) > int(end_ip):
            return config, "DHCP 起始地址不能大于结束地址"
        if start_ip == lan_gateway_ipv4(config):
            return config, "DHCP 起始地址不能是网关地址"
        config["dhcp_start"] = str(start_ip)
        config["dhcp_end"] = str(end_ip)
    else:
        config["dhcp_start"], config["dhcp_end"] = _dhcp_range(config)

    if config["ipv6_mode"] in {IPV6_MODE_STATIC, IPV6_MODE_PD}:
        # Both modes route the ISP prefix natively; NAT66 would defeat the point.
        config["ipv6_nat"] = False

    return config, None


def load_router_config() -> dict[str, Any]:
    payload: dict[str, Any] = {}
    try:
        loaded = json.loads(ROUTER_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            payload = loaded
    except (OSError, json.JSONDecodeError):
        payload = {}

    if not payload:
        # Migrate the pre-dual-stack network.json (LAN subnet only) when present.
        try:
            legacy = json.loads(LEGACY_NETWORK_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(legacy, dict) and legacy.get("lan_network"):
                payload = {"lan_network": legacy["lan_network"]}
        except (OSError, json.JSONDecodeError):
            payload = {}

    config, _ = normalize_router_config(payload)
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        # In PD mode the LAN /64 is owned by the delegation, not by the config file.
        config["ipv6_prefix"] = pd_client.lan_prefix()
    return config


def save_router_config(config: dict[str, Any]) -> None:
    atomic_write_text(
        ROUTER_CONFIG_PATH,
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        mode=0o600,
    )


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def build_dnsmasq_config(config: dict[str, Any]) -> str:
    """dnsmasq serves DHCPv4 and DNS; systemd-networkd owns IPv6 and RAs.

    DNS is bound to the LAN addresses only (``bind-interfaces``) so it never takes
    the wildcard port and cannot shadow systemd-resolved's stub on 127.0.0.53.
    """
    lan = str(config.get("lan_interface", "") or "")
    network = _lan_ipv4_network(config)
    gateway = str(lan_gateway_ipv4(config))
    ipv6_address = lan_ipv6_address(config)
    start, end = _dhcp_range(config)

    lines = [
        "# Generated by Linux Router - do not edit by hand.",
        "# Service: systemctl restart dnsmasq",
    ]
    if lan:
        lines.append(f"interface={lan}")
    else:
        # Nothing configured yet: stay on loopback instead of serving the whole LAN.
        lines.extend(["# LAN 接口尚未配置，暂只在回环上提供 DNS", "interface=lo"])
    lines.extend(
        [
            "bind-interfaces",
            "domain-needed",
            "bogus-priv",
            "dhcp-authoritative",
            f"dhcp-leasefile={DNSMASQ_LEASE_PATH}",
            "cache-size=1000",
        ]
    )
    if lan:
        lines.append(f"listen-address={gateway}")
        # systemd-networkd assigns fe80::1 to the LAN and advertises it via RDNSS.
        lines.append("listen-address=fe80::1")
        if ipv6_address is not None:
            lines.append(f"listen-address={ipv6_address}")

    if config.get("dns_upstream"):
        lines.append("# 上游 DNS 只使用下面这几条（不再叠加 /etc/resolv.conf）")
        lines.append("no-resolv")
        for server in str(config["dns_upstream"]).split():
            lines.append(f"server={server}")
    else:
        # 面板会把 WAN 下发的 DNS 写进 /etc/resolv.conf，dnsmasq 直接读它。
        lines.append("# 上游 DNS 取自 /etc/resolv.conf（面板按 WAN 下发的 DNS 维护）")

    if config.get("dhcp_enabled"):
        lines.extend(
            [
                f"dhcp-range={start},{end},{network.netmask},{config.get('dhcp_lease', '12h')}",
                f"dhcp-option=option:router,{gateway}",
                f"dhcp-option=option:dns-server,{gateway}",
            ]
        )
        leases = config.get("static_leases") or []
        if leases:
            lines.append("# 静态地址绑定（MAC → 固定 IP）")
            for lease in leases:
                entry = f"dhcp-host={lease.get('mac', '')},{lease.get('ip', '')}"
                if lease.get("hostname"):
                    entry += f",{lease['hostname']}"
                lines.append(entry)

    return "\n".join(lines) + "\n"


def dnsmasq_config_includes_our_file() -> bool:
    content = read_text(str(DNSMASQ_CONFIG_PATH))
    if not content:
        return False
    for raw_line in content.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if line.startswith("conf-dir=") and DNSMASQ_CONF_DIR.name in line:
            return True
    return False


def ensure_dnsmasq_include() -> CommandResult:
    DNSMASQ_CONF_DIR.mkdir(parents=True, exist_ok=True)
    if dnsmasq_config_includes_our_file():
        return CommandResult(True, "dnsmasq.conf 已包含 /etc/dnsmasq.d")

    existing = read_text(str(DNSMASQ_CONFIG_PATH))
    line = f"conf-dir={DNSMASQ_CONF_DIR}/,*.conf"
    if not existing:
        content = (
            "# Generated by Linux Router: keep the packaged defaults in /etc/dnsmasq.d\n"
            f"{line}\n"
        )
        atomic_write_text(DNSMASQ_CONFIG_PATH, content, mode=0o644)
        return CommandResult(True, "已创建 /etc/dnsmasq.conf 并引入 /etc/dnsmasq.d")

    atomic_write_text(
        DNSMASQ_CONFIG_PATH,
        existing.rstrip("\n") + "\n\n# Linux Router\n" + line + "\n",
        mode=0o644,
    )
    return CommandResult(True, "已在 /etc/dnsmasq.conf 中引入 /etc/dnsmasq.d")


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def ensure_network_backend(config: dict[str, Any]) -> CommandResult:
    """Write the systemd-networkd configuration and bring the links up."""
    return networkd.apply_networkd(config)


def ensure_forwarding() -> CommandResult:
    """Persist and enable IPv4 + IPv6 forwarding."""
    content = (
        "# Generated by Linux Router\n"
        "net.ipv4.ip_forward=1\n"
        "net.ipv6.conf.all.forwarding=1\n"
    )
    try:
        atomic_write_text(ROUTER_PANEL_SYSCTL_PATH, content, mode=0o644)
    except OSError as exc:
        return CommandResult(False, f"写入转发配置失败：{exc}")

    for key in ("net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
        result = run_command(["sysctl", "-w", f"{key}=1"], timeout=10)
        if not result.ok:
            return CommandResult(False, f"启用 {key} 失败：{result.output or '未知错误'}")
    return CommandResult(True, "已启用 IPv4/IPv6 转发")


def apply_router_config(
    config: dict[str, Any],
    progress: ProgressCallback | None = None,
) -> CommandResult:
    def report(message: str) -> None:
        if progress:
            progress(message)

    if not config.get("wan_interface") or not config.get("lan_interface"):
        return CommandResult(False, "请先选择 WAN 和 LAN 接口")

    if not command_exists("dnsmasq"):
        return CommandResult(False, "系统缺少 dnsmasq，请先在依赖页面安装")
    if not command_exists("nft"):
        return CommandResult(False, "系统缺少 nftables，请先在依赖页面安装")

    report("写入 dnsmasq 配置")
    include = ensure_dnsmasq_include()
    if not include.ok:
        return include
    try:
        atomic_write_text(DNSMASQ_RULESET_PATH, build_dnsmasq_config(config), mode=0o644)
    except OSError as exc:
        return CommandResult(False, f"写入 dnsmasq 配置失败：{exc}")

    tested = run_command(["dnsmasq", "--test", f"--conf-file={DNSMASQ_RULESET_PATH}"], timeout=15)
    if not tested.ok:
        return CommandResult(False, f"dnsmasq 配置校验失败：{tested.output or '未知错误'}")

    report("写入 systemd-networkd 配置")
    backend_result = ensure_network_backend(config)
    if not backend_result.ok:
        return backend_result

    report("同步上游 DNS")
    dns_result = networkd.write_resolv_conf(config, retries=5)

    report("写入 nftables 规则")
    firewall_result = apply_ruleset(render_nftables_ruleset(config))
    if not firewall_result.ok:
        return firewall_result

    report("启用 IPv4/IPv6 转发")
    forward_result = ensure_forwarding()
    if not forward_result.ok:
        return forward_result

    save_router_config(config)

    report("启动 dnsmasq")
    run_command(["systemctl", "enable", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
    restarted = run_command(["systemctl", "restart", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
    if not restarted.ok:
        return CommandResult(False, f"启动 dnsmasq 失败：{restarted.output or '未知错误'}")

    report("应用 CAKE 限速")
    cake_result = apply_cake(config)

    message = (
        f"路由配置已生效：WAN {config['wan_interface']} → LAN {config['lan_interface']}"
        f"（{lan_gateway_ipv4(config)}/{_lan_ipv4_network(config).prefixlen}）"
    )
    if config.get("wan_mode") == networkd.WAN_MODE_PPPOE:
        message += f"；PPPoE 已拨号（{config.get('pppoe_nic', '')}）"
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        prefix = networkd.get_delegated_prefix(str(config.get("lan_interface", "") or ""))
        message += f"；PD 前缀 {prefix}" if prefix else "；PD 前缀尚未下发，networkd 会自动跟随"
    if backend_result.output:
        message += f"；{backend_result.output}"
    if dns_result.ok and dns_result.output and not dns_result.output.startswith("未获取"):
        message += f"；{dns_result.output}"
    if not cake_result.ok:
        message += f"；CAKE 未应用：{cake_result.output}"
    elif cake_enabled(config):
        message += f"；{cake_result.output}"
    return CommandResult(True, message)


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


def _interface_status(
    ifname: str,
    devices: list[dict[str, Any]],
    all_interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Works for ethernet NICs and for logical uplinks such as ppp0."""
    device = next((item for item in devices if item.get("device") == ifname), {})
    generic = next((item for item in all_interfaces if item.get("name") == ifname), {})
    details = get_device_details(ifname) if ifname else {}
    addresses = get_interface_addresses(ifname) if ifname else {"ipv4": [], "ipv6": []}
    ipv4 = addresses["ipv4"] or details.get("ipv4", [])
    ipv6 = addresses["ipv6"] or details.get("ipv6", [])
    state = device.get("state") or generic.get("state", "")
    state_label = device.get("state_label") or generic.get("state_label", "未知")
    return {
        "interface": ifname,
        "state": state,
        "state_label": state_label,
        "connection": device.get("connection", ""),
        "link_speed": device.get("details", {}).get("link_speed", "未知"),
        "ipv4": ipv4,
        "ipv6": ipv6,
        "gateway": details.get("gateway", ""),
        "gateway6": details.get("gateway6", ""),
        "dns": details.get("dns", []),
        "online": state == "connected" and bool(ipv4 or ipv6),
    }


def get_lease_count() -> int:
    try:
        return len(
            [
                line
                for line in DNSMASQ_LEASE_PATH.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        )
    except OSError:
        return 0


def gather_router_status() -> dict[str, Any]:
    config = load_router_config()
    wired = gather_wired_network_info(config)
    devices = wired.get("devices", [])
    errors = list(wired.get("errors", []))
    all_interfaces = list_all_interfaces()

    interfaces = [
        {
            "name": device.get("device", ""),
            "state": device.get("state", ""),
            "state_label": device.get("state_label", ""),
            "connection": device.get("connection", ""),
            "link_speed": device.get("details", {}).get("link_speed", ""),
            "mac": device.get("details", {}).get("mac", ""),
            "role": device.get("role", ""),
        }
        for device in devices
    ]

    # The WAN can be a logical interface (ppp0 for PPPoE), so it gets its own list.
    wan_candidates = [
        {
            "name": item["name"],
            "state": item["state"],
            "state_label": item["state_label"],
            "ipv4": item.get("ipv4", []),
            "ipv6": item.get("ipv6", []),
            "default_route": item.get("default_route", False),
        }
        for item in all_interfaces
    ]

    wan = _interface_status(str(config.get("wan_interface", "") or ""), devices, all_interfaces)
    lan = _interface_status(str(config.get("lan_interface", "") or ""), devices, all_interfaces)

    ipv6_network = lan_ipv6_network(config)
    ipv6_address = lan_ipv6_address(config)
    wan_ipv6_global = [
        address for address in wan["ipv6"] if not address.lower().startswith(("fe80:", "fd"))
    ]

    firewall = get_firewall_status()
    errors.extend(firewall.get("errors", []))

    forwarding = {
        "ipv4": read_text("/proc/sys/net/ipv4/ip_forward") or "0",
        "ipv6": read_text("/proc/sys/net/ipv6/conf/all/forwarding") or "0",
    }

    services = {
        "dnsmasq": {
            "active": is_service_active("dnsmasq.service"),
            "enabled": is_service_enabled("dnsmasq.service"),
            "installed": command_exists("dnsmasq"),
        },
        "nftables": {
            "active": firewall["service_active"],
            "enabled": firewall["service_enabled"],
            "installed": firewall["available"],
        },
    }

    if not services["dnsmasq"]["active"]:
        errors.append("dnsmasq 未运行，局域网设备将无法获取地址")

    start, end = _dhcp_range(config)
    os_release = load_os_release()
    wan_name = str(config.get("wan_interface", "") or "")
    backend = networkd.get_networkd_status(config)
    pd_status = {}
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        pd_status = {
            "available": backend["available"],
            "service": backend["service"],
            "service_active": backend["service_active"],
            "service_enabled": backend["service_enabled"],
            "prefix": backend["delegated"],
            "delegated": backend["delegated"],
            "wan": wan_name,
            "lan": str(config.get("lan_interface", "") or ""),
            "reason": "systemd-networkd DHCPPrefixDelegation",
            "updated_at": "",
            "error": "" if backend["delegated"] else "尚未收到委派前缀",
        }
    wan_owner = {
        "interface": wan_name,
        "managed_by": "systemd-networkd",
        "method": str(config.get("wan_mode", networkd.WAN_MODE_DHCP)),
        "dhcpcd_pid": 0,
    }

    return {
        "config": config,
        "interfaces": interfaces,
        "wan_candidates": wan_candidates,
        "wan": wan,
        "lan": lan,
        "wan_owner": wan_owner,
        "ipv6": {
            "mode": config.get("ipv6_mode", IPV6_MODE_OFF),
            "mode_label": IPV6_MODE_LABELS.get(str(config.get("ipv6_mode", "")), "未知"),
            "prefix": str(ipv6_network) if ipv6_network else "",
            "lan_address": f"{ipv6_address}/{ipv6_network.prefixlen}" if ipv6_network and ipv6_address else "",
            "nat": bool(config.get("ipv6_nat")) and config.get("ipv6_mode") == IPV6_MODE_ULA,
            "wan_addresses": wan_ipv6_global,
            "wan_online": bool(wan_ipv6_global),
        },
        "pd": pd_status,
        "backend": backend,
        "dhcp": {
            "enabled": bool(config.get("dhcp_enabled")),
            "start": start,
            "end": end,
            "lease": config.get("dhcp_lease", ""),
            "lease_count": get_lease_count(),
        },
        "firewall": firewall,
        "forwarding": forwarding,
        "services": services,
        "os_name": os_release.get("PRETTY_NAME", ""),
        "errors": errors,
    }


LABEL_MAX_LENGTH = 32


def normalize_device_labels(raw: Any) -> dict[str, str]:
    """MAC -> 备注. Keys are normalised so lookups match the lease table."""
    if not isinstance(raw, dict):
        return {}
    labels: dict[str, str] = {}
    for key, value in raw.items():
        mac = normalize_mac_address(str(key or ""))
        text = str(value or "").strip()[:LABEL_MAX_LENGTH]
        if mac and text:
            labels[mac] = text
    return labels


def set_device_label(
    config: dict[str, Any], mac: str, label: str
) -> tuple[dict[str, Any], str | None]:
    normalized_mac = normalize_mac_address(str(mac or ""))
    if not normalized_mac:
        return config, "MAC 地址格式无效（应形如 aa:bb:cc:dd:ee:ff）"

    text = str(label or "").strip()
    if len(text) > LABEL_MAX_LENGTH:
        return config, f"备注最多 {LABEL_MAX_LENGTH} 个字符"

    updated = dict(config)
    labels = dict(normalize_device_labels(config.get("device_labels")))
    if text:
        labels[normalized_mac] = text
    else:
        labels.pop(normalized_mac, None)
    updated["device_labels"] = labels
    return updated, None


HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _static_leases(config: dict[str, Any]) -> list[dict[str, str]]:
    raw = config.get("static_leases")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def validate_static_lease(
    config: dict[str, Any], mac: str, ip: str, hostname: str = ""
) -> tuple[dict[str, str] | None, str | None]:
    """Check one binding before it reaches dnsmasq.

    dnsmasq silently ignores a bad ``dhcp-host`` line, so a typo would look like
    "the setting did nothing" instead of an error.
    """
    normalized_mac = normalize_mac_address(str(mac or ""))
    if not normalized_mac:
        return None, "MAC 地址格式无效（应形如 aa:bb:cc:dd:ee:ff）"

    try:
        address = ipaddress.IPv4Address(str(ip or "").strip())
    except ValueError:
        return None, "IP 地址格式无效"

    network = _lan_ipv4_network(config)
    if address not in network:
        return None, f"IP 必须落在 LAN 网段 {network} 内"
    if address == network.network_address:
        return None, "不能使用网段地址"
    if address == network.broadcast_address:
        return None, "不能使用广播地址"
    if address == lan_gateway_ipv4(config):
        return None, "该地址是本机网关，请换一个"

    name = str(hostname or "").strip()
    if name and not HOSTNAME_RE.fullmatch(name):
        return None, "主机名只能包含字母、数字和短横线，且不超过 63 个字符"

    for lease in _static_leases(config):
        if lease.get("ip") == str(address) and lease.get("mac") != normalized_mac:
            return None, f"{address} 已绑定给 {lease.get('mac')}"
    return {"mac": normalized_mac, "ip": str(address), "hostname": name}, None


def set_static_lease(
    config: dict[str, Any], mac: str, ip: str, hostname: str = ""
) -> tuple[dict[str, Any], str | None]:
    entry, error = validate_static_lease(config, mac, ip, hostname)
    if error:
        return config, error

    updated = dict(config)
    leases = [lease for lease in _static_leases(config) if lease.get("mac") != entry["mac"]]
    leases.append(entry)
    leases.sort(key=lambda item: ipaddress.IPv4Address(item.get("ip", "0.0.0.0")))
    updated["static_leases"] = leases
    return updated, None


def remove_static_lease(config: dict[str, Any], mac: str) -> tuple[dict[str, Any], str | None]:
    normalized_mac = normalize_mac_address(str(mac or ""))
    if not normalized_mac:
        return config, "MAC 地址格式无效"
    updated = dict(config)
    remaining = [lease for lease in _static_leases(config) if lease.get("mac") != normalized_mac]
    if len(remaining) == len(_static_leases(config)):
        return config, "没有找到这条绑定"
    updated["static_leases"] = remaining
    return updated, None


def apply_static_leases(config: dict[str, Any]) -> CommandResult:
    """Persist the bindings and reload dnsmasq so they take effect immediately."""
    save_router_config(config)
    if not command_exists("dnsmasq"):
        return CommandResult(True, "已保存，但系统缺少 dnsmasq，配置暂未加载")

    include = ensure_dnsmasq_include()
    if not include.ok:
        return include
    try:
        atomic_write_text(DNSMASQ_RULESET_PATH, build_dnsmasq_config(config), mode=0o644)
    except OSError as exc:
        return CommandResult(False, f"写入 dnsmasq 配置失败：{exc}")

    tested = run_command(["dnsmasq", "--test", f"--conf-file={DNSMASQ_RULESET_PATH}"], timeout=15)
    if not tested.ok:
        return CommandResult(False, f"dnsmasq 配置校验失败：{tested.output or '未知错误'}")

    restarted = run_command(["systemctl", "restart", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
    if not restarted.ok:
        return CommandResult(False, f"重启 dnsmasq 失败：{restarted.output or '未知错误'}")
    return CommandResult(True, "静态地址绑定已生效")


def gather_client_status() -> dict[str, Any]:
    config = load_router_config()
    lan_interface = str(config.get("lan_interface", "") or "")
    errors: list[str] = []

    try:
        leases = parse_dnsmasq_leases(DNSMASQ_LEASE_PATH.read_text(encoding="utf-8"))
    except OSError:
        leases = []
        if DNSMASQ_LEASE_PATH.exists():
            errors.append("无法读取 DHCP 租约文件")

    clients: dict[str, dict[str, Any]] = {}

    def entry(key: str) -> dict[str, Any]:
        return clients.setdefault(
            key,
            {
                "mac_address": "",
                "duid": "",
                "hostname": "",
                "ipv4": "",
                "ipv6": [],
                "lease": "",
                "permanent": "0",
                "online": "0",
                "source": "lease",
            },
        )

    for lease in leases:
        mac_address = normalize_mac_address(lease.get("mac_address", ""))
        key = mac_address or lease.get("ip_address", "")
        client = entry(key)
        if mac_address:
            client["mac_address"] = mac_address
        if lease.get("duid"):
            client["duid"] = lease["duid"]
        if lease.get("hostname"):
            client["hostname"] = lease["hostname"]
        if lease.get("family") == "ipv4":
            client["ipv4"] = lease["ip_address"]
        else:
            if lease["ip_address"] not in client["ipv6"]:
                client["ipv6"].append(lease["ip_address"])
        client["lease"] = lease.get("expires_label", "")
        client["permanent"] = lease.get("permanent", "0")

    neighbors = get_interface_neighbors(lan_interface) if lan_interface else []
    for neighbor in neighbors:
        mac_address = normalize_mac_address(neighbor.get("mac_address", ""))
        key = mac_address or neighbor.get("ip_address", "")
        client = entry(key)
        if mac_address:
            client["mac_address"] = mac_address
        if neighbor.get("family") == "ipv4" and not client["ipv4"]:
            client["ipv4"] = neighbor["ip_address"]
            client["source"] = "neighbor"
        elif neighbor.get("family") == "ipv6" and neighbor["ip_address"] not in client["ipv6"]:
            client["ipv6"].append(neighbor["ip_address"])
            if not client["ipv4"]:
                client["source"] = "neighbor"
        if neighbor.get("online") == "1":
            client["online"] = "1"

    labels = normalize_device_labels(config.get("device_labels"))
    by_mac = {lease["mac"]: lease for lease in _static_leases(config)}

    items = []
    for client in clients.values():
        client["ipv6"] = sorted(client["ipv6"])
        client["ipv6_label"] = ", ".join(client["ipv6"]) if client["ipv6"] else ""
        client["label"] = labels.get(client["mac_address"], "")
        # A label the user typed beats a hostname the device reports.
        client["name"] = (
            client["label"]
            or client["hostname"]
            or client["mac_address"]
            or client["ipv4"]
            or "未知设备"
        )
        lease = by_mac.get(client["mac_address"])
        client["static_ip"] = str(lease.get("ip", "")) if lease else ""
        client["reserved"] = "1" if lease else "0"
        items.append(client)
    items.sort(key=lambda item: (item["ipv4"] == "", item["ipv4"] or item["ipv6_label"], item["name"]))

    # One row per known MAC: a label without a binding still needs to be editable,
    # and so does a binding whose device is offline.
    known = list(labels) + [lease.get("mac", "") for lease in _static_leases(config)]
    devices = []
    for mac in dict.fromkeys(known):
        if not mac:
            continue
        lease = by_mac.get(mac)
        match = next((item for item in items if item["mac_address"] == mac), None)
        devices.append(
            {
                "mac": mac,
                "label": labels.get(mac, ""),
                "ip": str(lease.get("ip", "")) if lease else "",
                "hostname": str(lease.get("hostname", "")) if lease else "",
                "current_ip": (match or {}).get("ipv4", ""),
                "online": (match or {}).get("online", "0"),
            }
        )
    devices.sort(key=lambda item: (ipaddress.IPv4Address(item["ip"] or "255.255.255.255"), item["mac"]))

    return {
        "clients": items,
        "total": len(items),
        "online": len([item for item in items if item["online"] == "1"]),
        "lan_interface": lan_interface,
        "devices": devices,
        "errors": errors,
    }


__all__ = [
    "DEFAULT_ROUTER_CONFIG",
    "IPV6_MODE_OFF",
    "IPV6_MODE_PD",
    "IPV6_MODE_STATIC",
    "IPV6_MODE_ULA",
    "IPV6_MODE_LABELS",
    "IPV6_MODES",
    "apply_router_config",
    "build_dnsmasq_config",
    "dnsmasq_config_includes_our_file",
    "ensure_dnsmasq_include",
    "ensure_forwarding",
    "ensure_network_backend",
    "gather_client_status",
    "gather_router_status",
    "generate_ula_prefix",
    "lan_gateway_ipv4",
    "lan_ipv6_address",
    "lan_ipv6_network",
    "load_router_config",
    "normalize_router_config",
    "save_router_config",
    "normalize_device_labels",
    "set_device_label",
    "validate_static_lease",
    "set_static_lease",
    "remove_static_lease",
    "apply_static_leases",
]
