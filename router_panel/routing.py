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
    LAN_CONNECTION_NAME,
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
from . import pd as pd_client

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

    wan = get("wan_interface")
    lan = get("lan_interface")
    if wan and not IFNAME_RE.fullmatch(wan):
        return config, f"WAN 接口名称无效：{wan}"
    if lan and not IFNAME_RE.fullmatch(lan):
        return config, f"LAN 接口名称无效：{lan}"
    if wan and wan == lan:
        return config, "WAN 和 LAN 不能是同一块网卡"
    if available_wan_interfaces and wan and wan not in available_wan_interfaces:
        return config, f"WAN 接口 {wan} 不存在"
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

    config.update(
        {
            "wan_interface": wan,
            "lan_interface": lan,
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
        }
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
    lan = str(config.get("lan_interface", "") or "")
    network = _lan_ipv4_network(config)
    gateway = str(lan_gateway_ipv4(config))
    ipv6_network = lan_ipv6_network(config)
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
            "bind-dynamic",
            "domain-needed",
            "bogus-priv",
            "dhcp-authoritative",
            f"dhcp-leasefile={DNSMASQ_LEASE_PATH}",
            "cache-size=1000",
        ]
    )

    if config.get("dns_upstream"):
        lines.append("# 上游 DNS 只使用下面这几条（不再叠加 /etc/resolv.conf）")
        lines.append("no-resolv")
        for server in str(config["dns_upstream"]).split():
            lines.append(f"server={server}")

    if config.get("dhcp_enabled"):
        lines.extend(
            [
                f"dhcp-range={start},{end},{network.netmask},{config.get('dhcp_lease', '12h')}",
                f"dhcp-option=option:router,{gateway}",
                f"dhcp-option=option:dns-server,{gateway}",
            ]
        )

    if ipv6_network is not None and ipv6_address is not None:
        lines.extend(
            [
                "# IPv6：SLAAC 下发前缀，DHCPv6 只下发 DNS 等附加信息",
                "enable-ra",
                f"dhcp-range={ipv6_network.network_address},ra-stateless",
                f"dhcp-option=option6:dns-server,[{ipv6_address}]",
            ]
        )

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


def ensure_lan_connection(config: dict[str, Any]) -> CommandResult:
    """Point the LAN NIC at a fixed gateway address through NetworkManager.

    ``never-default`` keeps the LAN from competing with the WAN default route.
    """
    lan = str(config.get("lan_interface", "") or "")
    if not lan:
        return CommandResult(False, "尚未选择 LAN 接口")

    network = _lan_ipv4_network(config)
    gateway = lan_gateway_ipv4(config)
    ipv6_network = lan_ipv6_network(config)
    ipv6_address = lan_ipv6_address(config)

    exists = run_command(
        ["nmcli", "-t", "-f", "NAME", "connection", "show"], timeout=10
    )
    names = {line.strip() for line in (exists.output or "").splitlines()}
    create = LAN_CONNECTION_NAME not in names

    if create:
        command = [
            "nmcli", "connection", "add", "type", "ethernet",
            "ifname", lan, "con-name", LAN_CONNECTION_NAME,
        ]
    else:
        command = ["nmcli", "connection", "modify", LAN_CONNECTION_NAME]
    command.extend(["connection.interface-name", lan])
    command.extend(
        [
            "connection.autoconnect", "yes",
            "connection.autoconnect-priority", "50",
            "ipv4.method", "manual",
            "ipv4.addresses", f"{gateway}/{network.prefixlen}",
            "ipv4.gateway", "",
            "ipv4.never-default", "yes",
            "ipv4.dns", "",
            "ipv4.ignore-auto-dns", "yes",
        ]
    )
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        # dhcpcd owns the delegated address on this interface; NM must keep its
        # hands off IPv6 or it would flush the prefix on every reactivation.
        command.extend(["ipv6.method", "ignore"])
    elif ipv6_network is not None and ipv6_address is not None:
        command.extend(
            [
                "ipv6.method", "manual",
                "ipv6.addresses", f"{ipv6_address}/{ipv6_network.prefixlen}",
                "ipv6.gateway", "",
                "ipv6.never-default", "yes",
                "ipv6.ip6-privacy", "0",
            ]
        )
    else:
        command.extend(["ipv6.method", "disabled"])
    if config.get("mtu"):
        command.extend(["802-3-ethernet.mtu", str(config["mtu"])])

    result = run_command(command, timeout=30)
    if not result.ok:
        return CommandResult(False, f"写入 LAN 连接配置失败：{result.output or '未知错误'}")

    up = run_command(
        ["nmcli", "connection", "up", LAN_CONNECTION_NAME, "ifname", lan], timeout=40
    )
    if not up.ok:
        return CommandResult(False, f"启用 LAN 连接失败：{up.output or '未知错误'}")
    return CommandResult(True, f"LAN {lan} 已配置为 {gateway}/{network.prefixlen}")


def ensure_wan_ipv6(config: dict[str, Any]) -> CommandResult:
    """Turn on SLAAC/DHCPv6 for the WAN profile so the uplink is dual stack."""
    wan = str(config.get("wan_interface", "") or "")
    if not wan or not config.get("wan_ipv6_auto"):
        return CommandResult(True, "跳过 WAN IPv6 设置")

    devices = gather_wired_network_info(config).get("devices", [])
    profile = next((item for item in devices if item.get("device") == wan), {})
    connection_name = str(profile.get("connection", "") or "")
    if not connection_name:
        return CommandResult(True, f"WAN {wan} 暂无连接配置，IPv6 设置留待系统自行处理")

    methods = str(profile.get("profile", {}).get("ipv6_method", ""))
    if methods in {"auto", "dhcp"}:
        return CommandResult(True, f"WAN {wan} 已启用自动 IPv6")

    result = run_command(
        ["nmcli", "connection", "modify", connection_name, "ipv6.method", "auto"],
        timeout=20,
    )
    if not result.ok:
        return CommandResult(False, f"WAN 启用 IPv6 失败：{result.output or '未知错误'}")
    run_command(["nmcli", "connection", "up", connection_name], timeout=40)
    return CommandResult(True, f"已为 WAN {wan} 启用自动 IPv6")


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

    report("配置 LAN 接口地址")
    lan_result = ensure_lan_connection(config)
    if not lan_result.ok:
        return lan_result

    report("写入 nftables 规则")
    firewall_result = apply_ruleset(render_nftables_ruleset(config))
    if not firewall_result.ok:
        return firewall_result

    report("启用 IPv4/IPv6 转发")
    forward_result = ensure_forwarding()
    if not forward_result.ok:
        return forward_result

    # Persist first: the PD hook re-reads the config to know the LAN interface.
    save_router_config(config)

    pd_result = CommandResult(True, "")
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        report("启动 PD 客户端")
        pd_result = pd_client.enable_pd(config)
        if not pd_result.ok:
            return pd_result
    else:
        pd_client.disable_pd()

    report("启动 dnsmasq")
    run_command(["systemctl", "enable", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
    restarted = run_command(["systemctl", "restart", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
    if not restarted.ok:
        return CommandResult(False, f"启动 dnsmasq 失败：{restarted.output or '未知错误'}")

    report("检查 WAN IPv6")
    wan_ipv6 = ensure_wan_ipv6(config)

    if config.get("ipv6_mode") == IPV6_MODE_PD:
        report("等待运营商下发前缀")
        pd_client.sync_from_system(reason="apply")

    message = (
        f"路由配置已生效：WAN {config['wan_interface']} → LAN {config['lan_interface']}"
        f"（{lan_gateway_ipv4(config)}/{_lan_ipv4_network(config).prefixlen}）"
    )
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        prefix = pd_client.lan_prefix()
        message += f"；PD 前缀 {prefix}" if prefix else "；PD 前缀尚未下发，面板会自动跟随"
    if not wan_ipv6.ok:
        message += f"；WAN IPv6 未启用：{wan_ipv6.output}"
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
    pd_status = pd_client.get_pd_status() if config.get("ipv6_mode") == IPV6_MODE_PD else {}

    return {
        "config": config,
        "interfaces": interfaces,
        "wan_candidates": wan_candidates,
        "wan": wan,
        "lan": lan,
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

    items = []
    for client in clients.values():
        client["ipv6"] = sorted(client["ipv6"])
        client["ipv6_label"] = ", ".join(client["ipv6"]) if client["ipv6"] else ""
        client["name"] = client["hostname"] or client["mac_address"] or client["ipv4"] or "未知设备"
        items.append(client)
    items.sort(key=lambda item: (item["ipv4"] == "", item["ipv4"] or item["ipv6_label"], item["name"]))

    return {
        "clients": items,
        "total": len(items),
        "online": len([item for item in items if item["online"] == "1"]),
        "lan_interface": lan_interface,
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
    "ensure_lan_connection",
    "ensure_wan_ipv6",
    "gather_client_status",
    "gather_router_status",
    "generate_ula_prefix",
    "lan_gateway_ipv4",
    "lan_ipv6_address",
    "lan_ipv6_network",
    "load_router_config",
    "normalize_router_config",
    "save_router_config",
]
