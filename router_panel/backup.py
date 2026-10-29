from __future__ import annotations

import json
import os
import re
import socket
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .cake import CAKE_SCRIPT_PATH, CAKE_SERVICE_PATH, CAKE_TIMER_PATH
from .core import (
    APT_TIMEOUT,
    AUTH_PATH,
    DATA_DIR,
    DNSMASQ_RULESET_PATH,
    NFTABLES_RULESET_PATH,
    REQUIRED_PACKAGES,
    ROUTER_CONFIG_PATH,
    ROUTER_PANEL_SYSCTL_PATH,
    SECRET_KEY_PATH,
    SYSTEM_COMMAND_TIMEOUT,
    CommandResult,
    atomic_write_text,
    chown_service_user,
    get_build_info,
    package_installed,
    run_command,
)
from .networkd import (
    BRIDGE_NETDEV_PATH,
    NETWORKD_DIR,
    DNS_SCRIPT_PATH,
    DNS_SERVICE_NAME,
    DNS_SERVICE_PATH,
    DNS_TIMER_NAME,
    DNS_TIMER_PATH,
    LAN_NETWORK_PATH,
    PPP_NETWORK_PATH,
    PPPOE_CHAP_PATH,
    PPPOE_IPUP_PATH,
    PPPOE_PAP_PATH,
    PPPOE_PEER_PATH,
    PPPOE_SERVICE_NAME,
    PPPOE_SERVICE_PATH,
    WAN_NETWORK_PATH,
)

BACKUP_FORMAT = "linux-router-backup"
BACKUP_VERSION = 1
MAX_BACKUP_BYTES = 2 * 1024 * 1024
# Optional packages that are only meaningful on some setups; the backup records the
# names so a restore can pull them again instead of shipping any .deb.
OPTIONAL_PACKAGES = ("ppp",)

SERVICE_MONITOR_CONFIG_PATH = DATA_DIR / "service-monitor.json"
# Per-port bridge members are generated from the configured NIC names, so they are
# allowlisted by pattern instead of by an explicit list.
PORT_NETWORK_RE = re.compile(
    r"^/etc/systemd/network/40-linux-router-lan-[A-Za-z0-9_.@-]{1,15}\.network$"
)
TAILSCALE_CONFIG_PATH = DATA_DIR / "tailscale.json"

_EXTRA_UNITS = (
    Path("/etc/systemd/system/linux-router-cake.service"),
    Path("/etc/systemd/system/linux-router-cake.timer"),
    Path("/etc/systemd/system/linux-router-pppoe.service"),
    Path("/etc/systemd/system/linux-router-dns.service"),
    Path("/etc/systemd/system/linux-router-dns.timer"),
)


def backup_paths() -> list[Path]:
    """Everything we generate or store, and nothing else.

    The same list doubles as the restore whitelist, so a hand-edited backup file
    can never write outside these paths.
    """
    candidates = [
        ROUTER_CONFIG_PATH,
        AUTH_PATH,
        SECRET_KEY_PATH,
        SERVICE_MONITOR_CONFIG_PATH,
        TAILSCALE_CONFIG_PATH,
        WAN_NETWORK_PATH,
        LAN_NETWORK_PATH,
        BRIDGE_NETDEV_PATH,
        PPP_NETWORK_PATH,
        PPPOE_PEER_PATH,
        PPPOE_CHAP_PATH,
        PPPOE_PAP_PATH,
        PPPOE_IPUP_PATH,
        DNSMASQ_RULESET_PATH,
        NFTABLES_RULESET_PATH,
        ROUTER_PANEL_SYSCTL_PATH,
        CAKE_SCRIPT_PATH,
        CAKE_SERVICE_PATH,
        CAKE_TIMER_PATH,
        PPPOE_SERVICE_PATH,
        DNS_SCRIPT_PATH,
        DNS_SERVICE_PATH,
        DNS_TIMER_PATH,
        *_EXTRA_UNITS,
    ]
    # Bridge member files only exist while more than one LAN port is configured.
    candidates.extend(sorted(NETWORKD_DIR.glob("40-linux-router-lan-*.network")))

    # Deduplicate while keeping a stable order.
    seen: set[str] = set()
    ordered: list[Path] = []
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(path)
    return ordered


def installed_package_names() -> list[str]:
    names = [package for package in REQUIRED_PACKAGES if package_installed(package)]
    for package in OPTIONAL_PACKAGES:
        if package_installed(package):
            names.append(package)
    return list(dict.fromkeys(names))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def create_backup() -> dict[str, Any]:
    files: dict[str, dict[str, str]] = {}
    for path in backup_paths():
        try:
            content = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        mode = path.stat().st_mode & 0o777
        files[str(path)] = {"content": content, "mode": f"{mode:04o}"}

    return {
        "format": BACKUP_FORMAT,
        "version": BACKUP_VERSION,
        "created_at": _utc_now(),
        "hostname": socket.gethostname(),
        "build": get_build_info(),
        "packages": installed_package_names(),
        "files": files,
    }


def validate_backup(payload: Any) -> tuple[dict[str, Any], str | None]:
    if not isinstance(payload, dict):
        return {}, "备份文件格式无效"
    if payload.get("format") != BACKUP_FORMAT:
        return {}, "这不是 Linux Router 的备份文件"
    try:
        version = int(payload.get("version", 0))
    except (TypeError, ValueError):
        return {}, "备份文件版本无效"
    if version > BACKUP_VERSION:
        return {}, f"备份文件版本 {version} 高于当前面板支持的 {BACKUP_VERSION}"
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        return {}, "备份文件里没有任何配置"

    allowed = {str(path) for path in backup_paths()}
    unknown = [
        name for name in files if name not in allowed and not PORT_NETWORK_RE.match(name)
    ]
    if unknown:
        return {}, f"备份文件包含未知路径：{unknown[0]}"

    for name, item in files.items():
        if not isinstance(item, dict) or not isinstance(item.get("content"), str):
            return {}, f"备份文件中 {name} 的内容无效"
    packages = payload.get("packages", [])
    if not isinstance(packages, list) or any(not isinstance(name, str) for name in packages):
        return {}, "备份文件中的软件包清单无效"
    return payload, None


def _install_missing_packages(packages: list[str]) -> CommandResult:
    missing = [name for name in packages if not package_installed(name)]
    if not missing:
        return CommandResult(True, "软件包均已安装")

    env = os.environ.copy()
    env["DEBIAN_FRONTEND"] = "noninteractive"
    update = run_command(["apt-get", "update"], timeout=APT_TIMEOUT, env=env)
    warning = "" if update.ok else "（apt-get update 有失败项，已忽略）"
    install = run_command(
        ["apt-get", "install", "-y", "--no-install-recommends", *missing],
        timeout=APT_TIMEOUT,
        env=env,
    )
    if not install.ok:
        return CommandResult(False, install.output or f"安装 {', '.join(missing)} 失败")
    return CommandResult(True, f"已安装 {', '.join(missing)}{warning}")


def _reload_services(restored: set[str]) -> list[str]:
    notes: list[str] = []
    run_command(["systemctl", "daemon-reload"], timeout=SYSTEM_COMMAND_TIMEOUT)

    if any(str(path).startswith("/etc/systemd/network/") for path in restored):
        run_command(["networkctl", "reload"], timeout=SYSTEM_COMMAND_TIMEOUT)
        notes.append("networkd 配置已重载")

    if str(DNSMASQ_RULESET_PATH) in restored:
        run_command(["systemctl", "restart", "dnsmasq.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
        notes.append("dnsmasq 已重启")

    if str(NFTABLES_RULESET_PATH) in restored:
        run_command(["systemctl", "reload", "nftables.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
        notes.append("nftables 规则已重载")

    if str(CAKE_SCRIPT_PATH) in restored:
        run_command(["systemctl", "restart", "linux-router-cake.service"], timeout=SYSTEM_COMMAND_TIMEOUT)
        notes.append("CAKE 已重新应用")

    if str(PPPOE_SERVICE_PATH) in restored:
        notes.append(f"PPPoE 配置已更新，必要时 systemctl restart {PPPOE_SERVICE_NAME}")

    # The DNS sync units are only useful once enabled; run one pass immediately so
    # /etc/resolv.conf matches the restored configuration without waiting a cycle.
    if str(DNS_TIMER_PATH) in restored or str(DNS_SCRIPT_PATH) in restored:
        run_command(["systemctl", "enable", DNS_SERVICE_NAME], timeout=SYSTEM_COMMAND_TIMEOUT)
        run_command(["systemctl", "enable", "--now", DNS_TIMER_NAME], timeout=SYSTEM_COMMAND_TIMEOUT)
        run_command(["systemctl", "start", DNS_SERVICE_NAME], timeout=SYSTEM_COMMAND_TIMEOUT)
        notes.append("上游 DNS 已重新同步")

    return notes


def restore_backup(payload: Any, install_packages: bool = True) -> CommandResult:
    backup, error = validate_backup(payload)
    if error:
        return CommandResult(False, error)

    files: dict[str, dict[str, str]] = backup["files"]
    restored: set[str] = set()
    for name, item in files.items():
        path = Path(name)
        try:
            mode = int(item.get("mode", "0644"), 8)
        except ValueError:
            mode = 0o644
        try:
            atomic_write_text(path, item["content"], mode=mode)
        except OSError as exc:
            return CommandResult(False, f"写入 {name} 失败：{exc}")
        # The agent runs as root; the panel reads secret_key/auth.json as its own
        # account, so files inside the data dir must be handed back to it.
        if DATA_DIR in path.parents:
            chown_service_user(path)
        restored.add(name)

    notes: list[str] = []
    if install_packages:
        packages = backup.get("packages", [])
        installed = _install_missing_packages(packages)
        if not installed.ok:
            return CommandResult(False, f"配置已恢复，但{installed.output}")
        if installed.output != "软件包均已安装":
            notes.append(installed.output)

    notes.extend(_reload_services(restored))
    summary = f"已恢复 {len(restored)} 个配置文件"
    if notes:
        summary += "；" + "，".join(notes)
    return CommandResult(True, summary)


def summarize_backup(payload: Any) -> dict[str, Any]:
    """Small metadata view for the UI, without dumping file contents."""
    backup, error = validate_backup(payload)
    if error:
        return {"ok": False, "error": error}
    return {
        "ok": True,
        "created_at": str(backup.get("created_at", "")),
        "hostname": str(backup.get("hostname", "")),
        "build": backup.get("build", {}),
        "files": len(backup["files"]),
        "packages": backup.get("packages", []),
    }


__all__ = [
    "BACKUP_FORMAT",
    "BACKUP_VERSION",
    "MAX_BACKUP_BYTES",
    "backup_paths",
    "installed_package_names",
    "create_backup",
    "validate_backup",
    "restore_backup",
    "summarize_backup",
]
