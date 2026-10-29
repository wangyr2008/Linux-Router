from __future__ import annotations

from ipaddress import ip_address, ip_network

from flask import flash, redirect, render_template, request, url_for

from .agent_client import AgentError, query_agent, submit_operation
from .firewall import FIREWALL_MODE_LABELS, FIREWALL_MODE_PROTECT, parse_port_list
from .networkd import WAN_MODE_LABELS
from .routing import IPV6_MODE_LABELS, load_router_config


def _port_entries_cover(port: str, entries: list[str]) -> bool:
    """True when `port` is in the list, including `1000-2000` style ranges."""
    try:
        target = int(port)
    except ValueError:
        return False
    for entry in entries:
        if "-" in entry:
            low, _, high = entry.partition("-")
            try:
                if int(low) <= target <= int(high):
                    return True
            except ValueError:
                continue
        elif entry.isdigit() and int(entry) == target:
            return True
    return False


def _client_is_on_lan(config: dict, client: str) -> bool:
    try:
        network = ip_network(str(config.get("lan_network", "")), strict=False)
        return ip_address(client) in network
    except ValueError:
        # Unknown LAN or unparsable client address: assume the safer case and let it through.
        return True


def _panel_port() -> str:
    return str(request.environ.get("SERVER_PORT", "") or "80")


def lockout_warning(params: dict) -> str:
    """Refuse a protect-mode apply that would cut off the browser doing the apply.

    Turning on protect drops every new inbound connection from the WAN, which
    includes the panel and SSH. If the request itself arrives from outside the
    LAN and the panel port is not allowed, applying it means losing access.
    """
    if str(params.get("firewall_mode", "")) != FIREWALL_MODE_PROTECT:
        return ""
    port = _panel_port()
    allowed, error = parse_port_list(params.get("firewall_wan_tcp", ""))
    if error or _port_entries_cover(port, allowed):
        return ""
    client = request.remote_addr or ""
    try:
        config = load_router_config()
    except OSError:
        config = {}
    if _client_is_on_lan(config, client):
        return ""
    return (
        f"你正在从 WAN 侧（{client}）访问面板，而保护模式会丢弃所有来自 WAN 的新入站连接。"
        f"如果直接应用，当前地址 {request.host} 会立刻无法访问。"
        f"请先把面板端口 {port} 填进「WAN 放行 TCP 端口」（从 WAN 侧用 SSH 管理的话把 22 也加上），再应用。"
    )


def register_routing_routes(app, login_required, is_async_request) -> None:
    def router_status() -> dict:
        return query_agent("router_status")

    def client_status() -> dict:
        return query_agent("client_status")

    def form_flag(name: str) -> str:
        return "1" if request.form.get(name, "").strip() in {"1", "on", "true", "yes"} else "0"

    def queued(operation, message: str, endpoint: str = "router_page"):
        if is_async_request():
            return {
                "ok": True,
                "pending": True,
                "message": message,
                "operation_id": operation["id"],
                "operation_url": url_for("operation_status", operation_id=operation["id"]),
            }, 202
        flash(message, "success")
        return redirect(url_for(endpoint))

    @app.route("/router")
    @login_required
    def router_page():
        try:
            status = router_status()
        except AgentError as exc:
            status = {
                "config": load_router_config(),
                "interfaces": [],
                "wan": {},
                "lan": {},
                "ipv6": {},
                "dhcp": {},
                "firewall": {},
                "forwarding": {},
                "services": {},
                "errors": [str(exc)],
            }
        return render_template(
            "router.html",
            status=status,
            config=status.get("config", {}),
            ipv6_modes=IPV6_MODE_LABELS,
            firewall_modes=FIREWALL_MODE_LABELS,
            wan_modes=WAN_MODE_LABELS,
        )

    @app.route("/router/apply", methods=["POST"])
    @login_required
    def router_apply():
        params = {
            "wan_interface": request.form.get("wan_interface", "").strip(),
            "lan_interface": request.form.get("lan_interface", "").strip(),
            "lan_network": request.form.get("lan_network", "").strip(),
            "dhcp_enabled": form_flag("dhcp_enabled"),
            "dhcp_start": request.form.get("dhcp_start", "").strip(),
            "dhcp_end": request.form.get("dhcp_end", "").strip(),
            "dhcp_lease": request.form.get("dhcp_lease", "").strip(),
            "dns_upstream": request.form.get("dns_upstream", "").strip(),
            "ipv6_mode": request.form.get("ipv6_mode", "").strip(),
            "ipv6_prefix": request.form.get("ipv6_prefix", "").strip(),
            "ipv6_nat": form_flag("ipv6_nat"),
            "wan_ipv6_auto": form_flag("wan_ipv6_auto"),
            "mtu": request.form.get("mtu", "").strip(),
            "wan_mode": request.form.get("wan_mode", "").strip(),
            "pppoe_username": request.form.get("pppoe_username", "").strip(),
            "pppoe_password": request.form.get("pppoe_password", ""),
            "pppoe_nic": request.form.get("pppoe_nic", "").strip(),
            "firewall_mode": request.form.get("firewall_mode", "").strip(),
            "firewall_lan_tcp": request.form.get("firewall_lan_tcp", "").strip(),
            "firewall_wan_tcp": request.form.get("firewall_wan_tcp", "").strip(),
            "firewall_wan_udp": request.form.get("firewall_wan_udp", "").strip(),
        }
        warning = lockout_warning(params)
        if warning:
            if is_async_request():
                return {"ok": False, "pending": False, "message": warning}, 400
            flash(warning, "error")
            return redirect(url_for("router_page"))
        try:
            operation = submit_operation("router_apply", params, scope="router")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("router_page"))
        return queued(operation, "路由配置正在应用")

    @app.route("/clients/static", methods=["POST"])
    @login_required
    def client_static_set():
        params = {
            "mac": request.form.get("mac", "").strip(),
            "ip": request.form.get("ip", "").strip(),
            "hostname": request.form.get("hostname", "").strip(),
        }
        try:
            operation = submit_operation("static_lease_set", params, scope="system")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("clients_page"))
        return queued(operation, "静态地址正在保存", "clients_page")

    @app.route("/clients/device", methods=["POST"])
    @login_required
    def client_device_save():
        params = {
            "mac": request.form.get("mac", "").strip(),
            "label": request.form.get("label", "").strip(),
            "ip": request.form.get("ip", "").strip(),
            "hostname": request.form.get("hostname", "").strip(),
        }
        try:
            operation = submit_operation("device_save", params, scope="system")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("clients_page"))
        return queued(operation, "设备信息正在保存", "clients_page")

    @app.route("/clients/static/delete", methods=["POST"])
    @login_required
    def client_static_delete():
        params = {"mac": request.form.get("mac", "").strip()}
        try:
            operation = submit_operation("static_lease_delete", params, scope="system")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("clients_page"))
        return queued(operation, "正在取消固定地址", "clients_page")

    @app.route("/clients")
    @login_required
    def clients_page():
        try:
            status = client_status()
        except AgentError as exc:
            status = {"clients": [], "total": 0, "online": 0, "lan_interface": "", "errors": [str(exc)]}
        return render_template(
            "clients.html",
            status=status,
            # The per-row "固定 IP" link pre-fills the form below the table.
            prefill={
                "mac": request.args.get("mac", "").strip(),
                "ip": request.args.get("ip", "").strip(),
                "label": request.args.get("label", "").strip(),
            },
        )


__all__ = ["register_routing_routes"]
