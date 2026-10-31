from __future__ import annotations

import os
import re
from typing import Any

from .core import (
    APT_TIMEOUT,
    DNSMASQ_RULESET_PATH,
    REQUIRED_PACKAGES,
    ROUTER_PANEL_SYSCTL_PATH,
    SYSTEM_COMMAND_TIMEOUT,
    CommandResult,
    atomic_write_text,
    dhcpcd_instances,
    ifupdown_managed_interfaces,
    is_service_active,
    is_service_enabled,
    package_installed,
    python_module_available,
    read_text,
    run_command,
)
from .firewall import apply_ruleset, get_firewall_status, render_nftables_ruleset
from . import networkd
from .routing import (
    IPV6_MODE_PD,
    build_dnsmasq_config,
    ensure_dnsmasq_include,
    ensure_forwarding,
    lan_gateway_ipv4,
    lan_ipv6_address,
    lan_ipv6_network,
    load_router_config,
    save_router_config,
)


def get_ip_forward_status() -> dict[str, str]:
    return {
        "ipv4": read_text("/proc/sys/net/ipv4/ip_forward") or "0",
        "ipv6": read_text("/proc/sys/net/ipv6/conf/all/forwarding") or "0",
        "persistent": read_text(str(ROUTER_PANEL_SYSCTL_PATH)),
    }


def get_router_nat_status() -> dict[str, Any]:
    """Compare the configured router setup with what nftables actually holds."""
    config = load_router_config()
    firewall = get_firewall_status()
    wan = str(config.get("wan_interface", "") or "")
    lan = str(config.get("lan_interface", "") or "")
    ipv6_nat_expected = bool(config.get("ipv6_nat")) and str(config.get("ipv6_mode", "")) == "ula"

    if not wan or not lan:
        return {
            "level": "warning",
            "summary": "尚未配置 WAN/LAN 接口",
            "details": "在“路由”页面选择接口后即可自动生成 NAT 规则",
            "action": "",
        }
    if not firewall["available"]:
        return {
            "level": "error",
            "summary": "系统缺少 nftables",
            "details": "",
            "action": "install_packages",
        }
    if not firewall["nat4"]:
        return {
            "level": "error",
            "summary": "IPv4 NAT 规则未加载",
            "details": f"{lan} → {wan} 缺少 masquerade 规则",
            "action": "apply_firewall",
        }
    if ipv6_nat_expected and not firewall["nat6"]:
        return {
            "level": "warning",
            "summary": "IPv6 NAT66 规则未加载",
            "details": "LAN 使用 ULA 前缀，需要 NAT66 才能访问 IPv6 外网",
            "action": "apply_firewall",
        }
    details = f"{lan} → {wan}，已转发 IPv4 {firewall['nat4_packets']} 个包"
    if ipv6_nat_expected:
        details += f"，IPv6 {firewall['nat6_packets']} 个包"
    return {
        "level": "ok",
        "summary": "nftables NAT 规则已加载",
        "details": details,
        "action": "",
    }


def restart_service(service_name: str) -> CommandResult:
    return run_command(["systemctl", "restart", service_name], timeout=SYSTEM_COMMAND_TIMEOUT)


def enable_service(service_name: str) -> CommandResult:
    return run_command(["systemctl", "enable", service_name], timeout=SYSTEM_COMMAND_TIMEOUT)


def disable_service(service_name: str) -> CommandResult:
    return run_command(
        ["systemctl", "disable", "--now", service_name],
        timeout=SYSTEM_COMMAND_TIMEOUT,
    )


def install_missing_packages() -> CommandResult:
    missing_packages = [package for package in REQUIRED_PACKAGES if not package_installed(package)]
    if not missing_packages:
        return CommandResult(True, "所有必需软件包已安装")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    update = run_command(["apt-get", "update"], timeout=APT_TIMEOUT, env=env)
    # A broken source (a stale cdrom:// line after a DVD install) makes update
    # fail while the real mirrors still work, so only warn and try the install.
    update_warning = "" if update.ok else "；apt-get update 有失败项，已忽略并继续"

    install = run_command(
        ["apt-get", "install", "-y", "--no-install-recommends", *missing_packages],
        timeout=APT_TIMEOUT,
        env=env,
    )
    if not install.ok:
        detail = install.output or "安装依赖失败"
        if not update.ok and update.output:
            detail += f"（apt-get update：{update.output.splitlines()[0] if update.output else ''}）"
        return CommandResult(False, detail)
    return CommandResult(True, f"已安装: {', '.join(missing_packages)}{update_warning}")


def install_package(package: str) -> CommandResult:
    """Install one optional package (ppp, for example) on demand."""
    if package_installed(package):
        return CommandResult(True, f"{package} 已安装")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    update = run_command(["apt-get", "update"], timeout=APT_TIMEOUT, env=env)
    update_warning = "" if update.ok else "；apt-get update 有失败项，已忽略并继续"
    install = run_command(
        ["apt-get", "install", "-y", "--no-install-recommends", package],
        timeout=APT_TIMEOUT,
        env=env,
    )
    if not install.ok:
        detail = install.output or f"安装 {package} 失败"
        if not update.ok and update.output:
            detail += f"（apt-get update：{update.output.splitlines()[0] if update.output else ''}）"
        return CommandResult(False, detail)
    return CommandResult(True, f"已安装 {package}{update_warning}")


def get_dependency_rows() -> list[dict[str, Any]]:
    config = load_router_config()
    active_networkd = is_service_active(networkd.NETWORKD_SERVICE)
    enabled_networkd = is_service_enabled(networkd.NETWORKD_SERVICE)
    active_panel = is_service_active("router-panel.service")
    enabled_panel = is_service_enabled("router-panel.service")
    active_dnsmasq = is_service_active("dnsmasq.service")
    enabled_dnsmasq = is_service_enabled("dnsmasq.service")
    forwarding = get_ip_forward_status()
    nat = get_router_nat_status()
    firewall = get_firewall_status()
    lan_configured = _lan_has_address(config)
    protected = str(config.get("firewall_mode", "")) == "protect"
    native_ipv6 = str(config.get("ipv6_mode", "")) in {IPV6_MODE_PD, "static"}
    wan_mode = str(config.get("wan_mode", networkd.WAN_MODE_DHCP) or networkd.WAN_MODE_DHCP)

    wan_name = str(config.get("wan_interface", "") or "")
    # Only the WAN is checked now: a fresh Debian 13 box has no NetworkManager,
    # dhcpcd or ifupdown at all, so a "legacy stack" row was pure noise.
    ifupdown_map = ifupdown_managed_interfaces()
    instances = dhcpcd_instances()
    wan_legacy = wan_name in ifupdown_map or (wan_name and wan_name in instances)
    backend = networkd.get_networkd_status(config)
    wan_dns = networkd.get_link_dns(wan_name)
    upstream_dns = networkd.get_dns_servers()
    package_details = {
        "python3-flask": "提供网页运行框架",
        "gunicorn": "运行网页服务",
        "dnsmasq": "提供 LAN 的 DHCP / DNS / RA",
        "systemd": "提供 systemd-networkd 网络后端",
        "nftables": "提供 NAT 与转发规则",
        "iproute2": "读取 IP 和路由",
        "udev": "读取硬件信息",
    }

    rows = [
        {
            "group": "基础软件",
            "name": "Flask Python 模块",
            "status": "无异常" if python_module_available("flask") else "缺失",
            "level": "ok" if python_module_available("flask") else "error",
            "detail": "运行网页管理面板",
            "action": "install_packages" if not python_module_available("flask") else "",
            "action_label": "安装依赖",
        },
    ]

    for package_name in REQUIRED_PACKAGES:
        installed = package_installed(package_name)
        rows.append(
            {
                "group": "基础软件",
                "name": package_name,
                "status": "已安装" if installed else "未安装",
                "level": "ok" if installed else "error",
                "detail": package_details.get(package_name, ""),
                "action": "install_packages" if not installed else "",
                "action_label": "安装依赖",
            }
        )

    rows.extend(
        [
            {
                "group": "服务状态",
                "name": "systemd-networkd",
                "status": "无异常" if active_networkd and enabled_networkd else "有异常",
                "level": "ok" if active_networkd and enabled_networkd else "error",
                "detail": "管理系统网络连接（WAN / LAN / PPPoE）",
                "action": "" if (active_networkd and enabled_networkd) else "switch_to_networkd",
                "action_label": "启用 networkd",
            },
            {
                "group": "服务状态",
                "name": "dnsmasq.service",
                "status": "无异常" if active_dnsmasq and enabled_dnsmasq else "有异常",
                "level": "ok" if active_dnsmasq and enabled_dnsmasq else "error",
                "detail": "为 LAN 提供 DHCPv4 / DNS",
                "action": "restart_dnsmasq" if not (active_dnsmasq and enabled_dnsmasq) else "",
                "action_label": "启动服务",
            },
            {
                "group": "服务状态",
                "name": "nftables.service",
                "status": "无异常" if firewall["service_active"] and firewall["service_enabled"] else "有异常",
                "level": "ok" if firewall["service_active"] and firewall["service_enabled"] else "error",
                "detail": "开机加载 NAT 与转发规则",
                "action": "apply_firewall" if not (firewall["service_active"] and firewall["service_enabled"]) else "",
                "action_label": "重新加载规则",
            },
            {
                "group": "服务状态",
                "name": "router-panel.service",
                "status": "无异常" if active_panel and enabled_panel else "有异常",
                "level": "ok" if active_panel and enabled_panel else "error",
                "detail": "提供路由管理面板",
                "action": "enable_router_panel_service" if not enabled_panel else "",
                "action_label": "设为开机自启",
            },
            {
                "group": "网络接管",
                "name": "WAN 接管情况",
                "status": "未接管" if wan_legacy else "已接管",
                "level": "warning" if wan_legacy else "ok",
                "detail": (
                    f"{wan_name} 仍由旧网络栈管理，面板对 WAN 的设置不会生效"
                    if wan_legacy
                    else f"WAN {wan_name} 由 systemd-networkd 管理"
                ),
                "extra_details": (
                    [f"iface {wan_name} inet {ifupdown_map[wan_name]}"] if wan_legacy else []
                ),
                "action": "",
                "action_label": "",
            },
            {
                "group": "网络接管",
                "name": "上游 DNS",
                "status": "已配置" if upstream_dns else "缺失",
                "level": "ok" if upstream_dns else "warning",
                "detail": "面板按 WAN 下发的 DNS 维护 /etc/resolv.conf，dnsmasq 读它做上游",
                "extra_details": (
                    [f"/etc/resolv.conf: {', '.join(upstream_dns)}"]
                    + (
                        [f"WAN 学习到: {', '.join(wan_dns)}"]
                        if wan_dns and wan_dns != upstream_dns
                        else []
                    )
                ),
                "action": "refresh_dns" if not upstream_dns else "",
                "action_label": "重新获取",
            },
            {
                "group": "路由转发",
                "name": "IPv4 转发",
                "status": "已启用" if forwarding["ipv4"] == "1" else "未启用",
                "level": "ok" if forwarding["ipv4"] == "1" else "error",
                "detail": "允许 LAN 设备访问 IPv4 外网",
                "action": "fix_forwarding" if forwarding["ipv4"] != "1" else "",
                "action_label": "启用转发",
            },
            {
                "group": "路由转发",
                "name": "IPv6 转发",
                "status": "已启用" if forwarding["ipv6"] == "1" else "未启用",
                "level": "ok" if forwarding["ipv6"] == "1" else "error",
                "detail": "允许 LAN 设备访问 IPv6 外网",
                "action": "fix_forwarding" if forwarding["ipv6"] != "1" else "",
                "action_label": "启用转发",
            },
            {
                "group": "路由转发",
                "name": "nftables NAT/FORWARD",
                "status": "无异常" if nat["level"] == "ok" else "需处理",
                "level": nat["level"],
                "detail": nat["summary"],
                "extra_details": [nat["details"]] if nat["details"] else [],
                "action": nat["action"],
                "action_label": "重新加载规则" if nat["action"] == "apply_firewall" else ("安装依赖" if nat["action"] == "install_packages" else ""),
            },
            {
                "group": "路由转发",
                "name": "入站访问控制",
                "status": "保护中" if protected else "开放",
                "level": "ok" if (protected or not native_ipv6) else "warning",
                "detail": (
                    "丢弃来自 WAN 的新入站连接，只放行本机服务与已建立的连接"
                    if protected
                    else (
                        "LAN 使用公网前缀但未做入站过滤，外部可直接连入内网设备"
                        if native_ipv6
                        else "未做入站过滤；ULA + NAT66 模式下外部本就无法主动连入"
                    )
                ),
                "action": "",
                "action_label": "",
            },
            {
                "group": "路由转发",
                "name": "LAN 接口地址",
                "status": "已配置" if lan_configured else "未配置",
                "level": "ok" if lan_configured else "warning",
                "detail": _lan_address_summary(config),
                "action": "" if lan_configured else "fix_lan_interface",
                "action_label": "写入 LAN 配置",
            },
        ]
    )
    if wan_mode == networkd.WAN_MODE_PPPOE:
        rows.extend(
            [
                {
                    "group": "PPPoE 拨号",
                    "name": "pppd（PPPoE 客户端）",
                    "status": "已安装" if backend["ppp_installed"] else "未安装",
                    "level": "ok" if backend["ppp_installed"] else "error",
                    "detail": "systemd-networkd 不会拨号，PPPoE 由 pppd 建立会话",
                    "action": "" if backend["ppp_installed"] else "install_ppp",
                    "action_label": "安装 ppp",
                },
                {
                    "group": "PPPoE 拨号",
                    "name": "linux-router-pppoe.service",
                    "status": "无异常" if backend["pppoe_service_active"] else "未运行",
                    "level": "ok" if backend["pppoe_service_active"] else "error",
                    "detail": "在物理网口上建立 PPPoE 会话并拿到 ppp0",
                    "action": "" if backend["pppoe_service_active"] else "restart_pppoe",
                    "action_label": "重新拨号",
                },
            ]
        )

    if config.get("ipv6_mode") == IPV6_MODE_PD:
        pd_details = []
        if backend["delegated"]:
            pd_details.append(f"LAN 上的公网前缀：{backend['delegated']}")
        if backend["wan_link"]:
            addresses = backend["wan_link"].get("ipv6") or []
            if addresses:
                pd_details.append(f"WAN IPv6：{', '.join(addresses[:2])}")
        rows.extend(
            [
                {
                    "group": "IPv6 前缀委派",
                    "name": "DHCPPrefixDelegation",
                    "status": "已下发" if backend["delegated"] else "等待下发",
                    "level": "ok" if backend["delegated"] else "warning",
                    "detail": "由 systemd-networkd 请求前缀并在 LAN 上通告，无需额外守护进程",
                    "extra_details": pd_details,
                    "action": "",
                    "action_label": "",
                },
            ]
        )
    return rows


def _lan_has_address(config: dict[str, Any]) -> bool:
    lan = str(config.get("lan_interface", "") or "")
    if not lan:
        return False
    result = run_command(["ip", "-4", "address", "show", "dev", lan], timeout=5)
    if not result.ok:
        return False
    # Match `inet <addr>/` exactly: a plain substring test also matches
    # 192.168.50.1 inside 192.168.50.10/24.
    return re.search(rf"\binet {re.escape(str(lan_gateway_ipv4(config)))}/", result.output) is not None


def _lan_address_summary(config: dict[str, Any]) -> str:
    if not config.get("lan_interface"):
        return "尚未选择 LAN 接口"
    parts = [f"IPv4 {lan_gateway_ipv4(config)}"]
    ipv6_address = lan_ipv6_address(config)
    ipv6_network = lan_ipv6_network(config)
    if ipv6_address and ipv6_network:
        parts.append(f"IPv6 {ipv6_address}/{ipv6_network.prefixlen}")
    return "，".join(parts)


def gather_dependency_status() -> dict[str, Any]:
    rows = get_dependency_rows()
    groups: dict[str, list[dict[str, Any]]] = {}
    summary = {"ok": 0, "warning": 0, "error": 0}
    for row in rows:
        groups.setdefault(row["group"], []).append(row)
        summary[row["level"]] = summary.get(row["level"], 0) + 1
    return {
        "groups": groups,
        "summary": summary,
    }


def _run_dependency_action(action: str) -> CommandResult:
    if action == "install_packages":
        return install_missing_packages()
    if action == "enable_router_panel_service":
        return enable_service("router-panel.service")
    if action == "fix_forwarding":
        return ensure_forwarding()
    if action == "install_ppp":
        return install_package("ppp")
    if action == "restart_pppoe":
        return restart_service(networkd.PPPOE_SERVICE_NAME)
    if action == "switch_to_networkd":
        config = load_router_config()
        if not config.get("wan_interface") or not config.get("lan_interface"):
            return CommandResult(False, "请先在「路由」页面选择 WAN 和 LAN 接口")
        enable_service(networkd.NETWORKD_SERVICE)
        return networkd.apply_networkd(config)
    if action == "refresh_dns":
        config = load_router_config()
        return networkd.write_resolv_conf(config)
    if action == "apply_firewall":
        config = load_router_config()
        if not config.get("wan_interface") or not config.get("lan_interface"):
            return CommandResult(False, "请先在“路由”页面选择 WAN 和 LAN 接口")
        return apply_ruleset(render_nftables_ruleset(config))
    if action == "restart_dnsmasq":
        config = load_router_config()
        include = ensure_dnsmasq_include()
        if not include.ok:
            return include
        try:
            atomic_write_text(DNSMASQ_RULESET_PATH, build_dnsmasq_config(config), mode=0o644)
        except OSError as exc:
            return CommandResult(False, f"写入 dnsmasq 配置失败：{exc}")
        enable_service("dnsmasq.service")
        return restart_service("dnsmasq.service")
    if action == "fix_lan_interface":
        config = load_router_config()
        if not config.get("lan_interface"):
            return CommandResult(False, "请先在“路由”页面选择 LAN 接口")
        # LAN addresses live in systemd-networkd now; re-applying is the fix.
        result = networkd.apply_networkd(config)
        if result.ok:
            save_router_config(config)
        return result
    return CommandResult(False, "不支持的修复动作")


def run_dependency_action(action: str) -> CommandResult:
    return _run_dependency_action(action)


__all__ = [
    "get_ip_forward_status",
    "get_router_nat_status",
    "restart_service",
    "enable_service",
    "disable_service",
    "install_missing_packages",
    "get_dependency_rows",
    "gather_dependency_status",
    "run_dependency_action",
]
