from __future__ import annotations

import os
from configparser import ConfigParser
from io import StringIO
from pathlib import Path
from typing import Any

from .core import (
    APT_TIMEOUT,
    DNSMASQ_RULESET_PATH,
    NETPLAN_DIR,
    NETWORKMANAGER_CONFIG_PATH,
    NETWORKMANAGER_CONF_DIR,
    REQUIRED_PACKAGES,
    ROUTER_PANEL_SYSCTL_PATH,
    SYSTEM_COMMAND_TIMEOUT,
    CommandResult,
    atomic_write_text,
    command_exists,
    file_update_lock,
    is_service_active,
    is_service_enabled,
    package_installed,
    python_module_available,
    read_text,
    run_command,
)
from .firewall import apply_ruleset, get_firewall_status, render_nftables_ruleset
from .network import gather_wired_network_info
from .pd import PD_SERVICE_NAME, enable_pd, get_pd_status, install_pd_client
from .routing import (
    IPV6_MODE_PD,
    build_dnsmasq_config,
    ensure_dnsmasq_include,
    ensure_forwarding,
    ensure_lan_connection,
    lan_gateway_ipv4,
    lan_ipv6_address,
    lan_ipv6_network,
    load_router_config,
    save_router_config,
)


def get_networkmanager_managed_setting() -> str:
    parser = ConfigParser(interpolation=None)
    paths = [NETWORKMANAGER_CONFIG_PATH]
    if NETWORKMANAGER_CONF_DIR.exists():
        paths.extend(sorted(NETWORKMANAGER_CONF_DIR.glob("*.conf")))
    parser.read([str(path) for path in paths if path.exists()], encoding="utf-8")
    return parser.get("ifupdown", "managed", fallback="").strip().lower()


def get_netplan_files() -> list[Path]:
    if not NETPLAN_DIR.exists():
        return []
    return sorted(
        [
            *NETPLAN_DIR.glob("*.yaml"),
            *NETPLAN_DIR.glob("*.yml"),
        ]
    )


def get_netplan_renderer_summary() -> dict[str, Any]:
    if not command_exists("netplan"):
        return {
            "ok": True,
            "level": "ok",
            "summary": "系统未安装 netplan，跳过 renderer 检查",
            "details": [],
        }

    files = get_netplan_files()
    if not files:
        return {
            "ok": True,
            "level": "ok",
            "summary": "未找到 netplan 配置文件（Debian 最小安装通常不使用 netplan）",
            "details": [],
        }

    details: list[dict[str, str]] = []
    normalized_renderers: set[str] = set()
    for path in files:
        renderer = ""
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.split("#", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                key, value = line.split(":", 1)
                if key.strip() != "renderer":
                    continue
                renderer = value.strip()
                break
        except OSError:
            renderer = ""

        details.append({"path": str(path), "renderer": renderer or "未设置"})
        if renderer:
            normalized_renderers.add(renderer.lower())

    if normalized_renderers == {"networkmanager"}:
        return {
            "ok": True,
            "level": "ok",
            "summary": "netplan renderer 已设置为 NetworkManager",
            "details": details,
        }
    if "networkmanager" in normalized_renderers:
        return {
            "ok": False,
            "level": "warning",
            "summary": "netplan renderer 存在混合配置",
            "details": details,
        }
    return {
        "ok": False,
        "level": "error",
        "summary": "netplan 未切到 NetworkManager",
        "details": details,
    }


def get_ip_forward_status() -> dict[str, str]:
    return {
        "ipv4": read_text("/proc/sys/net/ipv4/ip_forward") or "0",
        "ipv6": read_text("/proc/sys/net/ipv6/conf/all/forwarding") or "0",
        "persistent": read_text(str(ROUTER_PANEL_SYSCTL_PATH)),
    }


def get_default_route_interface() -> str:
    result = run_command(["ip", "-4", "route", "show", "default"], timeout=5)
    if not result.ok or not result.output:
        return ""
    for line in result.output.splitlines():
        parts = line.split()
        for index, part in enumerate(parts):
            if part == "dev" and index + 1 < len(parts):
                return parts[index + 1].strip()
    return ""


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


def ensure_networkmanager_managed_ifupdown() -> CommandResult:
    with file_update_lock(NETWORKMANAGER_CONFIG_PATH) as acquired:
        if not acquired:
            return CommandResult(False, "NetworkManager 配置正在更新")

        parser = ConfigParser(interpolation=None)
        if NETWORKMANAGER_CONFIG_PATH.exists():
            try:
                with NETWORKMANAGER_CONFIG_PATH.open(encoding="utf-8") as handle:
                    parser.read_file(handle)
            except OSError as exc:
                return CommandResult(False, str(exc))

        if not parser.has_section("ifupdown"):
            parser.add_section("ifupdown")
        parser.set("ifupdown", "managed", "true")

        output = StringIO()
        parser.write(output)
        try:
            atomic_write_text(
                NETWORKMANAGER_CONFIG_PATH,
                output.getvalue(),
                mode=0o644,
            )
        except OSError as exc:
            return CommandResult(False, str(exc))
    return CommandResult(True, "已写入 NetworkManager managed=true")


def _netplan_with_networkmanager_renderer(content: str) -> tuple[str, bool]:
    lines = content.splitlines(keepends=True)
    changed = False
    updated_lines: list[str] = []

    for line in lines:
        newline = ""
        body = line
        if body.endswith("\r\n"):
            body = body[:-2]
            newline = "\r\n"
        elif body.endswith("\n"):
            body = body[:-1]
            newline = "\n"

        uncommented, hash_mark, comment = body.partition("#")
        key, delimiter, _value = uncommented.partition(":")
        if delimiter and key.strip() == "renderer":
            updated = f"{key}: NetworkManager"
            if hash_mark:
                updated = f"{updated}  #{comment.lstrip()}"
            updated = f"{updated}{newline}"
            if updated != line:
                changed = True
            updated_lines.append(updated)
            continue

        updated_lines.append(line)

    return "".join(updated_lines), changed


def ensure_netplan_networkmanager_renderer() -> CommandResult:
    if not command_exists("netplan"):
        return CommandResult(True, "系统未安装 netplan，已跳过 netplan 配置")

    NETPLAN_DIR.mkdir(parents=True, exist_ok=True)
    target = NETPLAN_DIR / "90-linux-router.yaml"
    content = (
        "network:\n"
        "  version: 2\n"
        "  renderer: NetworkManager\n"
    )
    updated_paths: list[str] = []
    try:
        for path in get_netplan_files():
            try:
                existing = path.read_text(encoding="utf-8")
            except OSError as exc:
                return CommandResult(False, str(exc))
            updated, changed = _netplan_with_networkmanager_renderer(existing)
            if changed:
                atomic_write_text(path, updated)
                updated_paths.append(str(path))

        atomic_write_text(target, content, mode=0o600)
        if str(target) not in updated_paths:
            updated_paths.append(str(target))
    except OSError as exc:
        return CommandResult(False, str(exc))
    return CommandResult(True, f"已写入 {', '.join(updated_paths)}")


def apply_netplan() -> CommandResult:
    if not command_exists("netplan"):
        return CommandResult(True, "系统未安装 netplan，已跳过 netplan apply")

    generate = run_command(["netplan", "generate"], timeout=SYSTEM_COMMAND_TIMEOUT)
    if not generate.ok:
        return generate
    return run_command(["netplan", "apply"], timeout=SYSTEM_COMMAND_TIMEOUT)


def _snapshot_paths(paths: list[Path]) -> dict[Path, tuple[str | None, int]]:
    snapshot: dict[Path, tuple[str | None, int]] = {}
    for path in dict.fromkeys(paths):
        content = path.read_text(encoding="utf-8") if path.exists() else None
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        snapshot[path] = (content, mode)
    return snapshot


def _snapshot_network_configuration() -> dict[Path, tuple[str | None, int]]:
    return _snapshot_paths(
        [NETWORKMANAGER_CONFIG_PATH, *get_netplan_files(), NETPLAN_DIR / "90-linux-router.yaml"]
    )


def _restore_network_configuration(snapshot: dict[Path, tuple[str | None, int]]) -> list[str]:
    errors: list[str] = []
    for path, (content, mode) in snapshot.items():
        try:
            if content is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, content, mode=mode)
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors


def _rollback_network_configuration(
    snapshot: dict[Path, tuple[str | None, int]],
    *,
    apply_netplan_config: bool,
    networkmanager_state: tuple[bool, bool] | None = None,
    restart_networkmanager: bool = True,
) -> str:
    errors = _restore_network_configuration(snapshot)
    if apply_netplan_config and command_exists("netplan"):
        generated = run_command(["netplan", "generate"], timeout=SYSTEM_COMMAND_TIMEOUT)
        if not generated.ok:
            errors.append(generated.output or "netplan generate 失败")
        else:
            applied = run_command(["netplan", "apply"], timeout=SYSTEM_COMMAND_TIMEOUT)
            if not applied.ok:
                errors.append(applied.output or "netplan apply 失败")
    if restart_networkmanager:
        restarted = restart_service("NetworkManager")
        if not restarted.ok:
            errors.append(restarted.output or "NetworkManager 重启失败")
    if networkmanager_state is not None:
        enabled, active = networkmanager_state
        if enabled:
            restored_enabled = enable_service("NetworkManager")
        else:
            restored_enabled = run_command(
                ["systemctl", "disable", "NetworkManager"],
                timeout=SYSTEM_COMMAND_TIMEOUT,
            )
        if not restored_enabled.ok:
            errors.append(restored_enabled.output or "NetworkManager 启用状态恢复失败")
        if active:
            restored_active = restart_service("NetworkManager")
        else:
            restored_active = run_command(
                ["systemctl", "stop", "NetworkManager"],
                timeout=SYSTEM_COMMAND_TIMEOUT,
            )
        if not restored_active.ok:
            errors.append(restored_active.output or "NetworkManager 运行状态恢复失败")
    return "; ".join(errors)


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
    if not update.ok:
        return CommandResult(False, update.output or "apt-get update 失败")

    install = run_command(
        ["apt-get", "install", "-y", "--no-install-recommends", *missing_packages],
        timeout=APT_TIMEOUT,
        env=env,
    )
    if not install.ok:
        return CommandResult(False, install.output or "安装依赖失败")
    return CommandResult(True, f"已安装: {', '.join(missing_packages)}")


def get_dependency_rows() -> list[dict[str, Any]]:
    config = load_router_config()
    wired_info = gather_wired_network_info(config)
    wired_devices = wired_info.get("devices", [])
    unmanaged_wired = [device.get("device", "") for device in wired_devices if device.get("state") == "unmanaged"]
    active_nm = is_service_active("NetworkManager")
    enabled_nm = is_service_enabled("NetworkManager")
    active_panel = is_service_active("router-panel.service")
    enabled_panel = is_service_enabled("router-panel.service")
    active_dhcpcd = is_service_active("dhcpcd.service")
    active_resolved = is_service_active("systemd-resolved.service")
    active_dnsmasq = is_service_active("dnsmasq.service")
    enabled_dnsmasq = is_service_enabled("dnsmasq.service")
    nm_managed = get_networkmanager_managed_setting() == "true"
    netplan = get_netplan_renderer_summary()
    forwarding = get_ip_forward_status()
    nat = get_router_nat_status()
    firewall = get_firewall_status()
    lan_configured = _lan_has_address(config)
    package_details = {
        "python3-flask": "提供网页运行框架",
        "gunicorn": "运行网页服务",
        "network-manager": "管理系统网络连接",
        "dnsmasq": "提供 LAN 的 DHCP / DNS / RA",
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
                "name": "NetworkManager",
                "status": "无异常" if active_nm and enabled_nm else "有异常",
                "level": "ok" if active_nm and enabled_nm else "error",
                "detail": "管理系统网络连接",
                "action": "repair_network_stack" if not (active_nm and enabled_nm) else "restart_networkmanager",
                "action_label": "修复网络栈" if not (active_nm and enabled_nm) else "重启服务",
            },
            {
                "group": "服务状态",
                "name": "dnsmasq.service",
                "status": "无异常" if active_dnsmasq and enabled_dnsmasq else "有异常",
                "level": "ok" if active_dnsmasq and enabled_dnsmasq else "error",
                "detail": "为 LAN 提供 DHCP / DNS / RA",
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
                "name": "NetworkManager ifupdown.managed",
                "status": "已启用" if nm_managed else "未启用",
                "level": "ok" if nm_managed else "error",
                "detail": "接管传统网络接口",
                "action": "fix_nm_managed" if not nm_managed else "",
                "action_label": "写入配置",
            },
            {
                "group": "网络接管",
                "name": "dhcpcd 服务冲突",
                "status": "有异常" if active_dhcpcd else "无异常",
                "level": "error" if active_dhcpcd else "ok",
                "detail": "避免重复管理网卡",
                "action": "disable_dhcpcd" if active_dhcpcd else "",
                "action_label": "停止并禁用",
            },
            {
                "group": "网络接管",
                "name": "systemd-resolved 端口冲突",
                "status": "有异常" if active_resolved else "无异常",
                "level": "warning" if active_resolved else "ok",
                "detail": "resolved 与 dnsmasq 会争用 53 端口",
                "action": "",
                "action_label": "",
            },
            {
                "group": "网络接管",
                "name": "netplan renderer",
                "status": "无异常" if netplan["ok"] else "需处理",
                "level": netplan["level"],
                "detail": "统一网络配置后端",
                "extra_details": [f"{item['path']}: {item['renderer']}" for item in netplan["details"]],
                "action": "repair_network_stack" if not netplan["ok"] else "",
                "action_label": "修复网络栈",
            },
            {
                "group": "网络接管",
                "name": "有线网卡由 NetworkManager 管理",
                "status": "无异常" if wired_devices and not unmanaged_wired else ("未检测到有线网卡" if not wired_devices else "有异常"),
                "level": "ok" if wired_devices and not unmanaged_wired else ("warning" if not wired_devices else "error"),
                "detail": "管理有线网络接口",
                "action": "repair_network_stack" if unmanaged_wired else "",
                "action_label": "修复网络栈",
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
                "name": "LAN 接口地址",
                "status": "已配置" if lan_configured else "未配置",
                "level": "ok" if lan_configured else "warning",
                "detail": _lan_address_summary(config),
                "action": "" if lan_configured else "fix_lan_interface",
                "action_label": "写入 LAN 配置",
            },
        ]
    )
    if config.get("ipv6_mode") == IPV6_MODE_PD:
        pd_status = get_pd_status()
        pd_details = []
        if pd_status["delegated"]:
            pd_details.append(f"运营商委派：{pd_status['delegated']}")
        if pd_status["prefix"]:
            pd_details.append(f"LAN /64：{pd_status['prefix']}")
        if pd_status["updated_at"]:
            pd_details.append(f"更新于 {pd_status['updated_at']}")
        if pd_status.get("error"):
            pd_details.append(f"同步异常：{pd_status['error']}")
        rows.extend(
            [
                {
                    "group": "IPv6 前缀委派",
                    "name": "dhcpcd（PD 客户端）",
                    "status": "已安装" if pd_status["available"] else "未安装",
                    "level": "ok" if pd_status["available"] else "error",
                    "detail": "通过 ia_pd 向运营商申请 IPv6 前缀",
                    "action": "" if pd_status["available"] else "install_pd_client",
                    "action_label": "安装 PD 客户端",
                },
                {
                    "group": "IPv6 前缀委派",
                    "name": "linux-router-pd.service",
                    "status": "无异常" if pd_status["service_active"] else "未运行",
                    "level": "ok" if pd_status["service_active"] else "error",
                    "detail": "在 WAN 上请求前缀并分配给 LAN",
                    "action": "" if pd_status["service_active"] else "restart_pd",
                    "action_label": "重启 PD 客户端",
                },
                {
                    "group": "IPv6 前缀委派",
                    "name": "已委派前缀",
                    "status": "已下发" if pd_status["prefix"] else "等待下发",
                    "level": "ok" if pd_status["prefix"] else "warning",
                    "detail": "PD 前缀会随运营商续租自动跟随",
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
    return result.ok and str(lan_gateway_ipv4(config)) in result.output


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
    if action == "fix_nm_managed":
        networkmanager_state = (
            is_service_enabled("NetworkManager"),
            is_service_active("NetworkManager"),
        )
        try:
            snapshot = _snapshot_network_configuration()
        except OSError as exc:
            return CommandResult(False, f"无法备份网络配置：{exc}")
        result = ensure_networkmanager_managed_ifupdown()
        if not result.ok:
            return result
        restart = restart_service("NetworkManager")
        if not restart.ok:
            rollback_error = _rollback_network_configuration(
                snapshot,
                apply_netplan_config=False,
                networkmanager_state=networkmanager_state,
            )
            if rollback_error:
                return CommandResult(
                    False,
                    f"{restart.output or 'NetworkManager 重启失败'}；网络配置回滚失败：{rollback_error}",
                )
            return restart
        return CommandResult(True, "已写入 managed=true 并重启 NetworkManager")
    if action == "apply_netplan":
        return apply_netplan()
    if action == "restart_networkmanager":
        return restart_service("NetworkManager")
    if action == "enable_router_panel_service":
        return enable_service("router-panel.service")
    if action == "disable_dhcpcd":
        return disable_service("dhcpcd.service")
    if action == "fix_forwarding":
        return ensure_forwarding()
    if action == "install_pd_client":
        result = install_pd_client()
        if not result.ok:
            return result
        config = load_router_config()
        if config.get("ipv6_mode") == IPV6_MODE_PD:
            started = enable_pd(config)
            if not started.ok:
                return CommandResult(True, f"{result.output}，但启动 PD 客户端失败：{started.output}")
        return result
    if action == "restart_pd":
        return restart_service(PD_SERVICE_NAME)
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
        result = ensure_lan_connection(config)
        if result.ok:
            save_router_config(config)
        return result
    if action == "repair_network_stack":
        networkmanager_state = (
            is_service_enabled("NetworkManager"),
            is_service_active("NetworkManager"),
        )
        try:
            snapshot = _snapshot_network_configuration()
        except OSError as exc:
            return CommandResult(False, f"无法备份网络配置：{exc}")
        for index, step in enumerate((
            ensure_networkmanager_managed_ifupdown,
            ensure_netplan_networkmanager_renderer,
            apply_netplan,
            lambda: enable_service("NetworkManager"),
        )):
            result = step()
            if not result.ok:
                if index == 0:
                    rollback_error = _rollback_network_configuration(
                        snapshot,
                        apply_netplan_config=False,
                        restart_networkmanager=False,
                    )
                    if rollback_error:
                        return CommandResult(
                            False,
                            f"{result.output or '网络修复失败'}；网络配置回滚失败：{rollback_error}",
                        )
                    return result
                rollback_error = _rollback_network_configuration(
                    snapshot,
                    apply_netplan_config=True,
                    networkmanager_state=networkmanager_state,
                )
                if rollback_error:
                    return CommandResult(
                        False,
                        f"{result.output or '网络修复失败'}；网络配置回滚失败：{rollback_error}",
                    )
                return result
        restart = restart_service("NetworkManager")
        if not restart.ok:
            rollback_error = _rollback_network_configuration(
                snapshot,
                apply_netplan_config=True,
                networkmanager_state=networkmanager_state,
            )
            if rollback_error:
                return CommandResult(
                    False,
                    f"{restart.output or 'NetworkManager 重启失败'}；网络配置回滚失败：{rollback_error}",
                )
            return restart
        return CommandResult(True, "已完成 NetworkManager 和 netplan 基础修复")
    return CommandResult(False, "不支持的修复动作")


def run_dependency_action(action: str) -> CommandResult:
    return _run_dependency_action(action)


__all__ = [
    "get_networkmanager_managed_setting",
    "get_netplan_files",
    "get_netplan_renderer_summary",
    "get_ip_forward_status",
    "get_default_route_interface",
    "get_router_nat_status",
    "ensure_networkmanager_managed_ifupdown",
    "ensure_netplan_networkmanager_renderer",
    "apply_netplan",
    "restart_service",
    "enable_service",
    "disable_service",
    "install_missing_packages",
    "get_dependency_rows",
    "gather_dependency_status",
    "run_dependency_action",
]
