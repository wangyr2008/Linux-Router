from __future__ import annotations

from flask import flash, redirect, render_template, request, url_for

from .agent_client import AgentError, query_agent, submit_operation
from .firewall import FIREWALL_MODE_LABELS
from .routing import IPV6_MODE_LABELS, load_router_config


def register_routing_routes(app, login_required, is_async_request) -> None:
    def router_status() -> dict:
        return query_agent("router_status")

    def client_status() -> dict:
        return query_agent("client_status")

    def form_flag(name: str) -> str:
        return "1" if request.form.get(name, "").strip() in {"1", "on", "true", "yes"} else "0"

    def queued(operation, message: str):
        if is_async_request():
            return {
                "ok": True,
                "pending": True,
                "message": message,
                "operation_id": operation["id"],
                "operation_url": url_for("operation_status", operation_id=operation["id"]),
            }, 202
        flash(message, "success")
        return redirect(url_for("router_page"))

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
            "firewall_mode": request.form.get("firewall_mode", "").strip(),
            "firewall_lan_tcp": request.form.get("firewall_lan_tcp", "").strip(),
            "firewall_wan_tcp": request.form.get("firewall_wan_tcp", "").strip(),
            "firewall_wan_udp": request.form.get("firewall_wan_udp", "").strip(),
        }
        try:
            operation = submit_operation("router_apply", params, scope="router")
        except AgentError as exc:
            if is_async_request():
                return {"ok": False, "pending": False, "message": str(exc)}, 503
            flash(str(exc), "error")
            return redirect(url_for("router_page"))
        return queued(operation, "路由配置正在应用")

    @app.route("/clients")
    @login_required
    def clients_page():
        try:
            status = client_status()
        except AgentError as exc:
            status = {"clients": [], "total": 0, "online": 0, "lan_interface": "", "errors": [str(exc)]}
        return render_template("clients.html", status=status)


__all__ = ["register_routing_routes"]
