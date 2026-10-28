import atexit
import ipaddress
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEST_DATA_DIRECTORY = tempfile.TemporaryDirectory(prefix="router-panel-tests-")
atexit.register(TEST_DATA_DIRECTORY.cleanup)
os.environ["LINUX_ROUTER_DATA_DIR"] = TEST_DATA_DIRECTORY.name

import app as application
from router_panel import agent_server
from router_panel import core
from router_panel import dependencies
from router_panel import firewall
from router_panel import network
from router_panel import network_parsers
from router_panel import pd as pd_client
from router_panel import routing
from router_panel import system
from router_panel import web_routing
from router_panel.core import CommandResult


EXPECTED_ROUTES = {
    "/",
    "/clients",
    "/dependencies",
    "/dependencies/fix",
    "/healthz",
    "/login",
    "/logout",
    "/operations/<operation_id>",
    "/router",
    "/router/apply",
    "/settings",
    "/settings/password",
    "/settings/reboot",
    "/static/<path:filename>",
    "/system",
    "/tools",
    "/tools/services/action",
    "/tools/services/save",
    "/tools/tailscale/login",
    "/tools/tailscale/logout",
    "/tools/tailscale/status",
    "/wired",
}


def ok(output: str = "") -> CommandResult:
    return CommandResult(True, output)


def fail(output: str) -> CommandResult:
    return CommandResult(False, output)


class RouteTests(unittest.TestCase):
    def test_expected_routes_are_registered(self):
        registered = {rule.rule for rule in application.app.url_map.iter_rules()}
        self.assertEqual(registered, EXPECTED_ROUTES)

    def test_no_wireless_routes_left(self):
        registered = {rule.rule for rule in application.app.url_map.iter_rules()}
        for path in registered:
            self.assertNotIn("wifi", path)
            self.assertNotIn("hotspot", path)


class RouterConfigTests(unittest.TestCase):
    def test_defaults_use_ula_and_dhcp(self):
        config, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        self.assertIsNone(error)
        self.assertEqual(config["lan_network"], core.DEFAULT_LAN_NETWORK)
        self.assertTrue(config["dhcp_enabled"])
        self.assertEqual(config["ipv6_mode"], routing.IPV6_MODE_ULA)
        self.assertTrue(config["ipv6_nat"])
        prefix = ipaddress.ip_network(config["ipv6_prefix"])
        self.assertTrue(prefix.is_private)
        self.assertEqual(prefix.prefixlen, 64)

    def test_ula_prefix_is_stable_between_calls_with_existing_config(self):
        first, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        self.assertIsNone(error)
        second, error = routing.normalize_router_config({}, existing=first)
        self.assertIsNone(error)
        self.assertEqual(first["ipv6_prefix"], second["ipv6_prefix"])

    def test_wan_and_lan_must_differ(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth0"}
        )
        self.assertIn("同一块网卡", error)

    def test_interface_must_exist(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth9"},
            available_interfaces=["eth0", "eth1"],
        )
        self.assertIn("eth9", error)

    def test_lan_network_must_be_private(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "lan_network": "8.8.8.0/24"}
        )
        self.assertIn("私有网段", error)

    def test_dhcp_pool_must_be_inside_lan(self):
        _, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "lan_network": "192.168.50.0/24",
                "dhcp_start": "192.168.60.10",
                "dhcp_end": "192.168.60.20",
            }
        )
        self.assertIn("必须在 LAN 网段内", error)

    def test_dhcp_pool_must_not_start_at_gateway(self):
        _, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "lan_network": "192.168.50.0/24",
                "dhcp_start": "192.168.50.1",
                "dhcp_end": "192.168.50.20",
            }
        )
        self.assertIn("网关地址", error)

    def test_static_ipv6_requires_global_prefix(self):
        _, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "ipv6_mode": "static",
                "ipv6_prefix": "fd00:1::/64",
            }
        )
        self.assertIn("公网 IPv6", error)

    def test_static_ipv6_rejects_non_64_prefix(self):
        _, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "ipv6_mode": "static",
                "ipv6_prefix": "2408:8207:1234::/48",
            }
        )
        self.assertIn("/64", error)

    def test_static_ipv6_disables_nat66(self):
        config, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "ipv6_mode": "static",
                "ipv6_prefix": "2408:8207:1234:5600::/64",
                "ipv6_nat": "1",
            }
        )
        self.assertIsNone(error)
        self.assertFalse(config["ipv6_nat"])
        self.assertEqual(str(routing.lan_ipv6_address(config)), "2408:8207:1234:5600::1")

    def test_ipv6_off_has_no_prefix(self):
        config, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_mode": "off"}
        )
        self.assertIsNone(error)
        self.assertEqual(config["ipv6_prefix"], "")
        self.assertIsNone(routing.lan_ipv6_network(config))

    def test_mtu_range(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "mtu": "100"}
        )
        self.assertIn("MTU", error)

    def test_dhcp_disabled_keeps_pool_for_reference(self):
        config, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "dhcp_enabled": "0"}
        )
        self.assertIsNone(error)
        self.assertFalse(config["dhcp_enabled"])
        self.assertTrue(config["dhcp_start"])


class DnsmasqConfigTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "eth0",
            "lan_interface": "eth1",
            "lan_network": "192.168.50.0/24",
            "dhcp_start": "192.168.50.100",
            "dhcp_end": "192.168.50.200",
            "ipv6_mode": "ula",
            "ipv6_prefix": "fd00:50::/64",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_ipv4_and_ipv6_are_rendered(self):
        text = routing.build_dnsmasq_config(self.config())
        self.assertIn("interface=eth1", text)
        self.assertIn("bind-dynamic", text)
        self.assertIn("dhcp-range=192.168.50.100,192.168.50.200,255.255.255.0,12h", text)
        self.assertIn("dhcp-option=option:router,192.168.50.1", text)
        self.assertIn("enable-ra", text)
        self.assertIn("dhcp-range=fd00:50::,ra-stateless", text)
        self.assertIn("dhcp-option=option6:dns-server,[fd00:50::1]", text)

    def test_ipv6_off_removes_ra(self):
        text = routing.build_dnsmasq_config(self.config(ipv6_mode="off"))
        self.assertNotIn("enable-ra", text)
        self.assertNotIn("option6", text)

    def test_dhcp_disabled_removes_v4_range(self):
        text = routing.build_dnsmasq_config(self.config(dhcp_enabled="0"))
        self.assertNotIn("dhcp-range=192.168", text)
        self.assertIn("enable-ra", text)

    def test_upstream_dns(self):
        text = routing.build_dnsmasq_config(self.config(dns_upstream="223.5.5.5 1.1.1.1"))
        self.assertIn("server=223.5.5.5", text)
        self.assertIn("server=1.1.1.1", text)


class NftablesTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "eth0",
            "lan_interface": "eth1",
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "ula",
            "ipv6_prefix": "fd00:50::/64",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_ruleset_has_nat_and_forwarding(self):
        text = firewall.render_nftables_ruleset(self.config())
        self.assertIn("table inet lr_forward", text)
        self.assertIn("table ip lr_nat", text)
        self.assertIn('ip saddr 192.168.50.0/24 oifname "eth0" counter masquerade', text)
        self.assertIn('iifname "eth1" oifname "eth0" accept', text)
        self.assertIn("policy accept", text)
        self.assertIn("ct state invalid drop", text)

    def test_ruleset_deletes_tables_before_redefining(self):
        text = firewall.render_nftables_ruleset(self.config())
        for table in ("inet lr_forward", "ip lr_nat", "ip6 lr_nat6"):
            self.assertIn(f"table {table}\ndelete table {table}", text)

    def test_nat66_follows_ipv6_nat(self):
        with_nat = firewall.render_nftables_ruleset(self.config(ipv6_nat="1"))
        self.assertIn("table ip6 lr_nat6 {", with_nat)
        self.assertIn('ip6 saddr fd00:50::/64 oifname "eth0" counter masquerade', with_nat)

        without_nat = firewall.render_nftables_ruleset(self.config(ipv6_nat="0"))
        self.assertNotIn("table ip6 lr_nat6 {", without_nat)
        self.assertIn("delete table ip6 lr_nat6", without_nat)

    def test_no_nat_without_interfaces(self):
        config = self.config()
        config["wan_interface"] = ""
        text = firewall.render_nftables_ruleset(config)
        self.assertNotIn("masquerade", text)

    def test_include_detection(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "nftables.conf"
            with patch.object(firewall, "NFTABLES_CONFIG_PATH", conf):
                self.assertFalse(firewall.nftables_config_includes_our_ruleset())
                conf.write_text(
                    '#!/usr/sbin/nft -f\nflush ruleset\ninclude "/etc/nftables.d/*.nft"\n',
                    encoding="utf-8",
                )
                self.assertTrue(firewall.nftables_config_includes_our_ruleset())

    def test_ensure_include_inserts_after_flush(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "nftables.conf"
            conf_dir = Path(directory) / "nftables.d"
            conf.write_text(
                "#!/usr/sbin/nft -f\nflush ruleset\n\ntable inet filter {\n}\n",
                encoding="utf-8",
            )
            with patch.object(firewall, "NFTABLES_CONFIG_PATH", conf), patch.object(
                firewall, "NFTABLES_CONF_DIR", conf_dir
            ):
                result = firewall.ensure_nftables_include()
                self.assertTrue(result.ok)
                content = conf.read_text(encoding="utf-8")
                self.assertIn('include "/etc/nftables.d/*.nft"', content)
                self.assertLess(content.index("flush ruleset"), content.index("include"))
                self.assertLess(content.index("include"), content.index("table inet filter"))


class LeaseParsingTests(unittest.TestCase):
    def test_ipv4_and_ipv6_leases(self):
        raw = (
            "1750000000 aa:bb:cc:dd:ee:ff 192.168.50.100 laptop 01:aa:bb:cc:dd:ee:ff\n"
            "1750000000 00:01:00:01:aa:bb 192.168.50.101 phone *\n"
            "1750000000 00:01:00:01:11:22 fd00:50::100 tablet *\n"
        )
        leases = network_parsers.parse_dnsmasq_leases(raw)
        self.assertEqual(len(leases), 3)
        self.assertEqual(leases[0]["family"], "ipv4")
        self.assertEqual(leases[0]["mac_address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(leases[0]["hostname"], "laptop")
        self.assertEqual(leases[1]["hostname"], "phone")
        self.assertEqual(leases[2]["family"], "ipv6")
        self.assertEqual(leases[2]["duid"], "00:01:00:01:11:22")
        self.assertEqual(leases[2]["mac_address"], "")

    def test_expiry_label(self):
        self.assertEqual(network_parsers.format_lease_expiry("0")["expires_label"], "永久")
        self.assertEqual(network_parsers.format_lease_expiry("1")["expires_label"], "已过期")


class ClientStatusTests(unittest.TestCase):
    def test_leases_and_neighbors_are_merged(self):
        workdir = Path(TEST_DATA_DIRECTORY.name)
        lease_file = workdir / "dnsmasq.leases"
        lease_file.write_text(
            "1750000000 aa:bb:cc:dd:ee:ff 192.168.50.100 laptop *\n",
            encoding="utf-8",
        )
        config_file = workdir / "router-clients.json"
        config_file.write_text(
            json.dumps(
                {
                    "wan_interface": "eth0",
                    "lan_interface": "eth1",
                    "lan_network": "192.168.50.0/24",
                    "ipv6_mode": "off",
                }
            ),
            encoding="utf-8",
        )
        neighbors = [
            {"family": "ipv4", "ip_address": "192.168.50.100", "mac_address": "AA:BB:CC:DD:EE:FF", "state": "reachable", "online": "1"},
            {"family": "ipv6", "ip_address": "fd00:50::100", "mac_address": "AA:BB:CC:DD:EE:FF", "state": "stale", "online": "0"},
            {"family": "ipv4", "ip_address": "192.168.50.77", "mac_address": "22:33:44:55:66:77", "state": "reachable", "online": "1"},
        ]
        with patch.object(routing, "DNSMASQ_LEASE_PATH", lease_file), patch.object(
            routing, "ROUTER_CONFIG_PATH", config_file
        ), patch.object(routing, "get_interface_neighbors", return_value=neighbors):
            status = routing.gather_client_status()

        self.assertEqual(status["total"], 2)
        self.assertEqual(status["online"], 2)
        laptop = next(item for item in status["clients"] if item["mac_address"] == "AA:BB:CC:DD:EE:FF")
        self.assertEqual(laptop["ipv4"], "192.168.50.100")
        self.assertEqual(laptop["ipv6"], ["fd00:50::100"])
        self.assertEqual(laptop["hostname"], "laptop")
        self.assertEqual(laptop["online"], "1")
        unknown = next(item for item in status["clients"] if item["mac_address"] == "22:33:44:55:66:77")
        self.assertEqual(unknown["source"], "neighbor")
        self.assertEqual(unknown["ipv4"], "192.168.50.77")


class ForwardingTests(unittest.TestCase):
    def test_forwarding_writes_both_families(self):
        target = Path(TEST_DATA_DIRECTORY.name) / "90-router-panel.conf"
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            return ok()

        with patch.object(routing, "ROUTER_PANEL_SYSCTL_PATH", target), patch.object(
            routing, "run_command", side_effect=fake_run
        ):
            result = routing.ensure_forwarding()

        self.assertTrue(result.ok)
        content = target.read_text(encoding="utf-8")
        self.assertIn("net.ipv4.ip_forward=1", content)
        self.assertIn("net.ipv6.conf.all.forwarding=1", content)
        self.assertIn(["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"], calls)


class LanConnectionTests(unittest.TestCase):
    def test_creates_profile_when_missing(self):
        config, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        self.assertIsNone(error)
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            if command[:4] == ["nmcli", "-t", "-f", "NAME"]:
                return ok("Wired connection 1")
            return ok()

        with patch.object(routing, "run_command", side_effect=fake_run):
            result = routing.ensure_lan_connection(config)

        self.assertTrue(result.ok)
        add_command = calls[1]
        self.assertEqual(add_command[:5], ["nmcli", "connection", "add", "type", "ethernet"])
        self.assertIn("ipv4.addresses", add_command)
        self.assertIn("192.168.50.1/24", add_command)
        self.assertIn("ipv4.never-default", add_command)
        self.assertIn("fd00:50::1/64", add_command)
        self.assertEqual(calls[-1][:3], ["nmcli", "connection", "up"])

    def test_modifies_existing_profile(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_mode": "off"}
        )
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            if command[:4] == ["nmcli", "-t", "-f", "NAME"]:
                return ok(core.LAN_CONNECTION_NAME)
            return ok()

        with patch.object(routing, "run_command", side_effect=fake_run):
            result = routing.ensure_lan_connection(config)

        self.assertTrue(result.ok)
        modify_command = calls[1]
        self.assertEqual(
            modify_command[:4], ["nmcli", "connection", "modify", core.LAN_CONNECTION_NAME]
        )
        self.assertIn("ipv6.method", modify_command)
        self.assertEqual(modify_command[modify_command.index("ipv6.method") + 1], "disabled")


class ApplyRouterTests(unittest.TestCase):
    def test_apply_reports_missing_binaries(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        with patch.object(routing, "command_exists", return_value=False):
            result = routing.apply_router_config(config)
        self.assertFalse(result.ok)
        self.assertIn("dnsmasq", result.output)

    def test_apply_writes_files_and_starts_services(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        workdir = Path(TEST_DATA_DIRECTORY.name) / "apply"
        workdir.mkdir(exist_ok=True)
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")
        dnsmasq_rules = workdir / "linux-router.conf"
        sysctl_path = workdir / "sysctl.conf"
        router_json = workdir / "router.json"
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            if command[:2] == ["nmcli", "-t"]:
                return ok("")
            return ok()

        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf
        ), patch.object(routing, "DNSMASQ_CONF_DIR", workdir), patch.object(
            routing, "DNSMASQ_RULESET_PATH", dnsmasq_rules
        ), patch.object(routing, "ROUTER_PANEL_SYSCTL_PATH", sysctl_path), patch.object(
            routing, "ROUTER_CONFIG_PATH", router_json
        ), patch.object(routing, "run_command", side_effect=fake_run), patch.object(
            routing, "apply_ruleset", return_value=ok("rules loaded")
        ), patch.object(routing, "gather_wired_network_info", return_value={"devices": []}):
            result = routing.apply_router_config(config)

        self.assertTrue(result.ok, result.output)
        self.assertIn("interface=eth1", dnsmasq_rules.read_text(encoding="utf-8"))
        self.assertTrue(sysctl_path.exists())
        self.assertTrue(router_json.exists())
        self.assertIn(["systemctl", "restart", "dnsmasq.service"], commands)


class AgentTests(unittest.TestCase):
    def test_router_apply_validates_interfaces(self):
        with patch.object(
            agent_server, "available_router_interfaces", return_value=["eth0", "eth1"]
        ):
            with self.assertRaises(agent_server.ValidationError):
                agent_server._execute_router_apply(
                    {"wan_interface": "eth0", "lan_interface": "eth9"}
                )

    def test_router_apply_calls_apply(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        with patch.object(
            agent_server, "available_router_interfaces", return_value=["eth0", "eth1"]
        ), patch.object(agent_server, "load_router_config", return_value=config), patch.object(
            agent_server, "apply_router_config", return_value=ok("done")
        ) as apply_mock:
            result = agent_server._execute_router_apply(
                {"wan_interface": "eth0", "lan_interface": "eth1"}
            )
        self.assertTrue(result["ok"])
        apply_mock.assert_called_once()

    def test_unknown_action_is_rejected(self):
        runtime = agent_server.AgentRuntime()
        with self.assertRaises(agent_server.ValidationError):
            runtime.submit("wifi_connect", {}, "router", {})

    def test_scope_must_be_known(self):
        runtime = agent_server.AgentRuntime()
        with self.assertRaises(agent_server.ValidationError):
            runtime.submit("router_apply", {"wan_interface": "eth0"}, "hotspot", {})

    def test_query_registry(self):
        with patch.object(agent_server, "gather_router_status", return_value={"config": {}}):
            self.assertEqual(agent_server.execute_query("router_status", {}), {"config": {}})
        with self.assertRaises(agent_server.ValidationError):
            agent_server.execute_query("wireless_status", {})


class DependencyTests(unittest.TestCase):
    def test_required_packages_are_minimal(self):
        self.assertIn("nftables", core.REQUIRED_PACKAGES)
        self.assertIn("dnsmasq", core.REQUIRED_PACKAGES)
        self.assertNotIn("iptables", core.REQUIRED_PACKAGES)
        self.assertNotIn("iw", core.REQUIRED_PACKAGES)
        self.assertNotIn("wpasupplicant", core.REQUIRED_PACKAGES)

    def test_nat_status_warns_without_interfaces(self):
        with patch.object(dependencies, "load_router_config", return_value={}):
            status = dependencies.get_router_nat_status()
        self.assertEqual(status["level"], "warning")

    def test_nat_status_ok_when_rule_present(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        fake = {
            "available": True,
            "service_active": True,
            "service_enabled": True,
            "nat4": True,
            "nat6": True,
            "nat4_packets": 12,
            "nat6_packets": 3,
            "errors": [],
        }
        with patch.object(dependencies, "load_router_config", return_value=config), patch.object(
            dependencies, "get_firewall_status", return_value=fake
        ):
            status = dependencies.get_router_nat_status()
        self.assertEqual(status["level"], "ok")
        self.assertIn("12", status["details"])

    def test_fix_forwarding_action(self):
        with patch.object(dependencies, "ensure_forwarding", return_value=ok("forwarding")) as mock:
            result = dependencies.run_dependency_action("fix_forwarding")
        self.assertTrue(result.ok)
        mock.assert_called_once()


class WebTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"

    def test_pages_require_login(self):
        for path in ("/system", "/router", "/clients", "/wired", "/dependencies", "/settings"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302, path)

    def test_router_page_renders(self):
        self.login()
        status = {
            "config": routing.load_router_config(),
            "interfaces": [
                {"name": "eth0", "link_speed": "1 Gbps"},
                {"name": "eth1", "link_speed": "1 Gbps"},
            ],
            "wan": {
                "interface": "eth0",
                "state_label": "已连接",
                "online": True,
                "ipv4": ["203.0.113.2/24"],
                "ipv6": [],
                "dns": ["223.5.5.5"],
            },
            "lan": {
                "interface": "eth1",
                "state_label": "已连接",
                "online": True,
                "ipv4": ["192.168.50.1/24"],
                "ipv6": ["fd00:50::1/64"],
            },
            "ipv6": {
                "mode": "ula",
                "mode_label": "ULA 私有前缀 + NAT66",
                "prefix": "fd00:50::/64",
                "lan_address": "fd00:50::1/64",
                "nat": True,
                "wan_addresses": [],
                "wan_online": False,
            },
            "dhcp": {
                "enabled": True,
                "start": "192.168.50.100",
                "end": "192.168.50.200",
                "lease": "12h",
                "lease_count": 2,
            },
            "firewall": {"nat4": True, "nat6": True, "nat4_packets": 10, "nat6_packets": 4},
            "forwarding": {"ipv4": "1", "ipv6": "1"},
            "services": {"dnsmasq": {"active": True}, "nftables": {"active": True}},
            "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            response = self.client.get("/router")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("WAN 上行", body)
        self.assertIn("192.168.50.100", body)
        self.assertIn("fd00:50::/64", body)
        self.assertIn("IPv6 NAT66", body)

    def test_router_page_shows_pd_card(self):
        self.login()
        config, _ = routing.normalize_router_config(
            {"wan_interface": "ppp0", "lan_interface": "enp2s0", "ipv6_mode": "pd"}
        )
        config["ipv6_prefix"] = "2408:8207:6a1c:2f00::/64"
        status = {
            "config": config,
            "interfaces": [{"name": "enp2s0", "link_speed": "1 Gbps"}],
            "wan_candidates": [
                {"name": "ppp0", "state": "connected", "state_label": "已连接", "ipv4": ["100.64.12.36/32"], "ipv6": [], "default_route": True}
            ],
            "wan": {"interface": "ppp0", "state_label": "已连接", "online": True, "ipv4": ["100.64.12.36/32"], "ipv6": [], "dns": []},
            "lan": {"interface": "enp2s0", "state_label": "已连接", "online": True, "ipv4": ["192.168.50.1/24"], "ipv6": ["2408:8207:6a1c:2f00::1/64"]},
            "ipv6": {"mode": "pd", "mode_label": "运营商 PD 自动（dhcpcd）", "prefix": "2408:8207:6a1c:2f00::/64",
                     "lan_address": "2408:8207:6a1c:2f00::1/64", "nat": False, "wan_addresses": [], "wan_online": False},
            "dhcp": {"enabled": True, "start": "192.168.50.128", "end": "192.168.50.254", "lease": "12h", "lease_count": 3},
            "firewall": {"nat4": True, "nat6": False, "nat4_packets": 1, "nat6_packets": 0},
            "forwarding": {"ipv4": "1", "ipv6": "1"},
            "services": {"dnsmasq": {"active": True}, "nftables": {"active": True}},
            "pd": {"available": True, "service_active": True, "service_enabled": True,
                   "delegated": "2408:8207:6a1c:2f00::/60", "prefix": "2408:8207:6a1c:2f00::/64",
                   "updated_at": "2026-10-28T04:00:00+00:00", "reason": "DELEGATED6", "error": ""},
            "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            response = self.client.get("/router")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("IPv6 前缀委派", body)
        self.assertIn("2408:8207:6a1c:2f00::/60", body)
        self.assertIn("ppp0", body)

    def test_router_page_survives_agent_failure(self):
        self.login()
        with patch.object(web_routing, "query_agent", side_effect=web_routing.AgentError("boom")):
            response = self.client.get("/router")
        self.assertEqual(response.status_code, 200)
        self.assertIn("boom", response.get_data(as_text=True))

    def test_clients_page_renders(self):
        self.login()
        status = {
            "clients": [
                {
                    "name": "laptop",
                    "hostname": "laptop",
                    "mac_address": "AA:BB:CC:DD:EE:FF",
                    "duid": "",
                    "ipv4": "192.168.50.100",
                    "ipv6_label": "fd00:50::100",
                    "ipv6": ["fd00:50::100"],
                    "lease": "11 小时",
                    "online": "1",
                    "source": "lease",
                }
            ],
            "total": 1,
            "online": 1,
            "lan_interface": "eth1",
            "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            response = self.client.get("/clients")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("192.168.50.100", body)
        self.assertIn("fd00:50::100", body)

    def test_settings_page_no_longer_holds_lan_form(self):
        self.login()
        response = self.client.get("/settings")
        body = response.get_data(as_text=True)
        self.assertNotIn('name="lan_network"', body)
        self.assertIn("路由", body)

    def test_system_page_renders(self):
        self.login()
        info = {
            "hostname": "router",
            "os_name": "Debian GNU/Linux 13 (trixie)",
            "kernel": "6.12.0-amd64",
            "architecture": "x86_64",
            "uptime": "1 天 2 小时 3 分钟",
            "load_average": "0.10, 0.20, 0.30",
            "cpu_model": "Intel N100 (4 核)",
            "cpu_temperature": "42.0 °C",
            "memory_total": "8.0 GB",
            "memory_available": "6.0 GB",
            "disk_total": "128.0 GB",
            "disk_free": "100.0 GB",
            "ip_addresses": ["192.168.50.1", "fd00:50::1"],
            "active_connections": [
                {
                    "name": "Wired connection 1",
                    "type": "802-3-ethernet",
                    "type_label": "有线",
                    "device": "eth0",
                    "ipv4_addresses": ["203.0.113.2/24"],
                    "ipv6_addresses": ["2408:8207::2/64"],
                }
            ],
            "network_status": {"wan": True, "lan": True, "wired": True},
            "network_interfaces": [],
        }
        with patch("router_panel.web_general.query_agent", return_value=info):
            response = self.client.get("/system")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("2408:8207::2/64", body)
        self.assertIn("WAN", body)

    def test_all_pages_render_without_agent(self):
        """Every page must degrade gracefully when the root agent is unreachable."""
        self.login()
        for path in ("/system", "/router", "/clients", "/wired", "/dependencies", "/settings", "/tools"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200, path)

    def test_wired_page_renders_roles_and_ipv6(self):
        self.login()
        wired = {
            "devices": [
                {
                    "device": "eth1",
                    "role": "lan",
                    "role_label": "LAN 局域网",
                    "state": "connected",
                    "state_label": "已连接",
                    "connection": core.LAN_CONNECTION_NAME,
                    "details": {
                        "mac": "AA:BB:CC:DD:EE:FF",
                        "carrier": "1",
                        "link_speed": "1 Gbps",
                        "ipv4": ["192.168.50.1/24"],
                        "ipv6": ["fd00:50::1/64"],
                        "gateway": "",
                        "gateway6": "",
                        "dns": [],
                    },
                    "profile": {"ipv4_method_label": "静态地址", "ipv6_method_label": "静态地址"},
                }
            ],
            "errors": [],
        }
        with patch("router_panel.web_general.query_agent", return_value=wired):
            response = self.client.get("/wired")
        body = response.get_data(as_text=True)
        self.assertIn("LAN 局域网", body)
        self.assertIn("fd00:50::1/64", body)


class SummaryTests(unittest.TestCase):
    def test_network_status_uses_roles(self):
        connections = [
            {"name": "Wired connection 1", "type": "802-3-ethernet", "device": "eth0"},
            {"name": core.LAN_CONNECTION_NAME, "type": "802-3-ethernet", "device": "eth1"},
        ]
        status = system.summarize_network_status(
            connections, {"wan_interface": "eth0", "lan_interface": "eth1"}
        )
        self.assertEqual(status, {"wan": True, "lan": True, "wired": True})

        status = system.summarize_network_status(
            connections, {"wan_interface": "eth2", "lan_interface": "eth1"}
        )
        self.assertFalse(status["wan"])

    def test_translate_methods(self):
        self.assertEqual(network.translate_ipv6_method("auto"), "自动 (SLAAC/DHCPv6)")
        self.assertEqual(network.translate_ipv4_method("manual"), "静态地址")



class PrefixDelegationTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "ppp0",
            "lan_interface": "enp2s0",
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "pd",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_pd_mode_disables_nat66_and_ignores_manual_prefix(self):
        config = self.config(ipv6_prefix="fd00:1::/64", ipv6_nat="1")
        self.assertEqual(config["ipv6_prefix"], "")
        self.assertFalse(config["ipv6_nat"])

    def test_dhcpcd_conf_is_pd_only(self):
        text = pd_client.render_dhcpcd_conf(self.config())
        self.assertIn("noipv4", text)
        self.assertIn("noipv6rs", text)
        self.assertIn("allowinterfaces ppp0 enp2s0", text)
        self.assertIn("ia_pd 2/::/56 enp2s0/0/64", text)
        self.assertIn("nohook resolv.conf, hostname", text)

    def test_hook_script_never_exits(self):
        text = pd_client.render_hook_script(self.config())
        self.assertIn('if [ "$interface" = "ppp0" ]', text)
        self.assertIn("DELEGATED6", text)
        self.assertIn("pd_hook.py", text)
        self.assertIn(f'LINUX_ROUTER_DATA_DIR="{core.DATA_DIR}"', text)
        self.assertNotIn("\nexit", text)
        self.assertNotIn("exec ", text)

    def test_service_unit_runs_in_foreground(self):
        with patch.object(pd_client, "dhcpcd_binary", return_value="/usr/sbin/dhcpcd"):
            text = pd_client.render_service_unit(self.config())
        self.assertIn("Type=simple", text)
        self.assertIn("--nobackground", text)
        self.assertIn("/etc/linux-router/dhcpcd.conf", text)

    def test_first_lan_subnet_slices_delegations(self):
        self.assertEqual(pd_client.first_lan_subnet("2408:8207:6a1c:2f00::/60"), "2408:8207:6a1c:2f00::/64")
        self.assertEqual(pd_client.first_lan_subnet("2408:8207:6a1c:2f00::/56"), "2408:8207:6a1c:2f00::/64")
        self.assertEqual(pd_client.first_lan_subnet("2408:8207:6a1c:2f00::/64"), "2408:8207:6a1c:2f00::/64")
        self.assertEqual(pd_client.first_lan_subnet("2408:8207:6a1c:2f00::/72"), "")
        self.assertEqual(pd_client.first_lan_subnet(""), "")
        self.assertEqual(pd_client.first_lan_subnet("not-a-prefix"), "")

    def test_first_lan_subnet_uses_second_64_of_a_slash_56(self):
        self.assertEqual(
            pd_client.first_lan_subnet("2408:8207:6a1c:2f00::/56"),
            "2408:8207:6a1c:2f00::/64",
        )

    def test_derive_prefers_delegation_over_stale_interface_address(self):
        stale = {"ipv6": ["2408:8207:6a1c:2f00::1/64"]}
        with patch.object(pd_client, "get_interface_addresses", return_value=stale):
            prefix = pd_client.derive_lan_prefix("enp2s0", "2408:8207:6a1c:2f10::/60")
        self.assertEqual(prefix, "2408:8207:6a1c:2f10::/64")

    def test_derive_falls_back_to_interface(self):
        addresses = {"ipv6": ["fe80::1/64", "2408:8207:6a1c:2f20::1/64"]}
        with patch.object(pd_client, "get_interface_addresses", return_value=addresses):
            prefix = pd_client.derive_lan_prefix("enp2s0", "")
        self.assertEqual(prefix, "2408:8207:6a1c:2f20::/64")

    def test_sync_rewrites_dnsmasq_only_on_change(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(
            json.dumps({"wan_interface": "ppp0", "lan_interface": "enp2s0", "ipv6_mode": "pd"}),
            encoding="utf-8",
        )
        state_path = workdir / "pd.json"
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")
        dnsmasq_rules = workdir / "linux-router.conf"
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            return ok()

        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            pd_client, "PD_STATE_PATH", state_path
        ), patch.object(routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf), patch.object(
            routing, "DNSMASQ_CONF_DIR", workdir
        ), patch.object(
            pd_client, "get_interface_addresses", return_value={"ipv6": []}
        ), patch.object(
            pd_client, "run_command", side_effect=fake_run
        ), patch.object(
            pd_client, "atomic_write_text", side_effect=lambda path, content, mode=None: path.write_text(content, encoding="utf-8")
        ), patch(
            "router_panel.core.DNSMASQ_RULESET_PATH", dnsmasq_rules
        ), patch.object(
            routing, "run_command", side_effect=fake_run
        ):
            first = pd_client.sync_from_system("2408:8207:6a1c:2f00::/60", "DELEGATED6")
            self.assertEqual(first, 0)
            rendered = dnsmasq_rules.read_text(encoding="utf-8")
            self.assertIn("dhcp-range=2408:8207:6a1c:2f00::,ra-stateless", rendered)
            self.assertIn("2408:8207:6a1c:2f00::1", rendered)
            restarts = [cmd for cmd in commands if cmd[:3] == ["systemctl", "restart", "dnsmasq.service"]]
            self.assertEqual(len(restarts), 1)

            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state["prefix"], "2408:8207:6a1c:2f00::/64")

            commands.clear()
            pd_client.sync_from_system("2408:8207:6a1c:2f00::/60", "RENEW6")
            restarts = [cmd for cmd in commands if cmd[:3] == ["systemctl", "restart", "dnsmasq.service"]]
            self.assertEqual(restarts, [])

    def test_sync_clears_prefix_when_delegation_is_lost(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-lost"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(
            json.dumps({"wan_interface": "ppp0", "lan_interface": "enp2s0", "ipv6_mode": "pd"}),
            encoding="utf-8",
        )
        state_path = workdir / "pd.json"
        state_path.write_text(json.dumps({"prefix": "2408:8207:6a1c:2f00::/64", "dnsmasq_prefix": "2408:8207:6a1c:2f00::/64"}), encoding="utf-8")
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")
        dnsmasq_rules = workdir / "linux-router.conf"

        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            pd_client, "PD_STATE_PATH", state_path
        ), patch.object(routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf), patch.object(
            routing, "DNSMASQ_CONF_DIR", workdir
        ), patch.object(
            pd_client, "get_interface_addresses", return_value={"ipv6": ["2408:8207:6a1c:2f00::1/64"]}
        ), patch.object(
            pd_client, "run_command", return_value=ok()
        ), patch.object(
            pd_client, "atomic_write_text", side_effect=lambda path, content, mode=None: path.write_text(content, encoding="utf-8")
        ), patch(
            "router_panel.core.DNSMASQ_RULESET_PATH", dnsmasq_rules
        ), patch.object(
            routing, "run_command", return_value=ok()
        ):
            pd_client.sync_from_system("", "EXPIRE6")

        rendered = dnsmasq_rules.read_text(encoding="utf-8")
        self.assertNotIn("enable-ra", rendered)
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["prefix"], "")

    def test_sync_is_noop_outside_pd_mode(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-noop"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(json.dumps({"ipv6_mode": "ula", "lan_interface": "enp2s0"}), encoding="utf-8")
        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            pd_client, "_write_dnsmasq_for_prefix"
        ) as writer:
            self.assertEqual(pd_client.sync_from_system("2408::/60", "DELEGATED6"), 0)
        writer.assert_not_called()

    def test_lan_connection_ignores_ipv6_in_pd_mode(self):
        config = self.config()
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            if command[:4] == ["nmcli", "-t", "-f", "NAME"]:
                return ok("")
            return ok()

        with patch.object(routing, "run_command", side_effect=fake_run):
            routing.ensure_lan_connection(config)

        modify = calls[1]
        self.assertEqual(modify[modify.index("ipv6.method") + 1], "ignore")
        self.assertNotIn("ipv6.addresses", modify)

    def test_pd_mode_has_no_nat66(self):
        text = firewall.render_nftables_ruleset(self.config())
        self.assertNotIn("table ip6 lr_nat6 {", text)

    def test_load_config_injects_delegated_prefix(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-load"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(
            json.dumps({"wan_interface": "ppp0", "lan_interface": "enp2s0", "ipv6_mode": "pd"}),
            encoding="utf-8",
        )
        state_path = workdir / "pd.json"
        state_path.write_text(json.dumps({"prefix": "2408:8207:6a1c:2f00::/64"}), encoding="utf-8")
        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            pd_client, "PD_STATE_PATH", state_path
        ):
            config = routing.load_router_config()
        self.assertEqual(config["ipv6_prefix"], "2408:8207:6a1c:2f00::/64")
        self.assertEqual(str(routing.lan_ipv6_address(config)), "2408:8207:6a1c:2f00::1")

    def test_dependency_rows_include_pd_checks(self):
        config = self.config()
        pd_status = {
            "available": True,
            "binary": "/usr/sbin/dhcpcd",
            "service": "linux-router-pd.service",
            "service_active": True,
            "service_enabled": True,
            "prefix": "2408:8207:6a1c:2f00::/64",
            "delegated": "2408:8207:6a1c:2f00::/60",
            "wan": "ppp0",
            "lan": "enp2s0",
            "reason": "DELEGATED6",
            "updated_at": "2026-10-28T04:00:00+00:00",
            "error": "",
        }
        with patch.object(dependencies, "load_router_config", return_value=config), patch.object(
            dependencies, "get_pd_status", return_value=pd_status
        ), patch.object(dependencies, "gather_wired_network_info", return_value={"devices": []}), patch.object(
            dependencies, "get_router_nat_status", return_value={"level": "ok", "summary": "", "details": "", "action": ""}
        ), patch.object(dependencies, "get_firewall_status", return_value={
            "available": True, "service_active": True, "service_enabled": True,
            "nat4": True, "nat6": False, "nat4_packets": 0, "nat6_packets": 0, "errors": [],
        }), patch.object(dependencies, "_lan_has_address", return_value=True), patch.object(
            dependencies, "get_ip_forward_status", return_value={"ipv4": "1", "ipv6": "1", "persistent": ""}
        ), patch.object(dependencies, "get_netplan_renderer_summary", return_value={"ok": True, "level": "ok", "summary": "", "details": []}):
            rows = dependencies.get_dependency_rows()

        names = [row["name"] for row in rows]
        self.assertIn("dhcpcd（PD 客户端）", names)
        self.assertIn("已委派前缀", names)
        prefix_row = next(row for row in rows if row["name"] == "已委派前缀")
        self.assertIn("2408:8207:6a1c:2f00::/60", " ".join(prefix_row["extra_details"]))




class AccessControlTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "ppp0",
            "lan_interface": "enp2s0",
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "pd",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_default_mode_is_open_with_no_input_table(self):
        config = self.config()
        self.assertEqual(config["firewall_mode"], "open")
        text = firewall.render_nftables_ruleset(config)
        self.assertIn("policy accept;", text)
        self.assertNotIn("table inet lr_input {", text)
        self.assertIn("delete table inet lr_input", text)

    def test_protect_mode_drops_input_and_forward(self):
        text = firewall.render_nftables_ruleset(self.config(firewall_mode="protect"))
        self.assertIn("table inet lr_input {", text)
        self.assertIn("type filter hook input priority filter; policy drop;", text)
        self.assertIn("type filter hook forward priority filter; policy drop;", text)
        self.assertIn('iifname "enp2s0" accept', text)
        self.assertIn('iifname "ppp0" udp dport { 68, 546 } accept', text)
        self.assertIn('iifname "enp2s0" tcp dport { 22, 53, 80 } accept', text)
        self.assertIn('iifname "enp2s0" udp dport { 53, 67, 547 } accept', text)
        self.assertIn('iifname "tailscale0" accept', text)
        self.assertIn("meta l4proto ipv6-icmp accept", text)
        self.assertIn("meta l4proto icmp accept", text)

    def test_protect_mode_merges_extra_ports(self):
        text = firewall.render_nftables_ruleset(
            self.config(firewall_mode="protect", firewall_lan_tcp="9090, 8080", firewall_wan_tcp="22")
        )
        self.assertIn('iifname "enp2s0" tcp dport { 22, 53, 80, 9090, 8080 } accept', text)
        self.assertIn('iifname "ppp0" tcp dport { 22 } accept', text)

    def test_protect_mode_allows_wan_udp_extras(self):
        text = firewall.render_nftables_ruleset(
            self.config(firewall_mode="protect", firewall_wan_udp="41641")
        )
        self.assertIn('iifname "ppp0" udp dport { 68, 546, 41641 } accept', text)

    def test_port_list_parsing(self):
        ports, error = firewall.parse_port_list("22, 80 ,1000-2000")
        self.assertIsNone(error)
        self.assertEqual(ports, ["22", "80", "1000-2000"])
        self.assertEqual(firewall.parse_port_list("")[0], [])
        self.assertIn("格式无效", firewall.parse_port_list("abc")[1])
        self.assertIn("超出范围", firewall.parse_port_list("70000")[1])
        self.assertIn("起止颠倒", firewall.parse_port_list("200-100")[1])

    def test_invalid_ports_are_rejected_by_config(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ppp0", "lan_interface": "enp2s0", "firewall_lan_tcp": "abc"}
        )
        self.assertIn("LAN 放行端口", error)

    def test_invalid_mode_is_rejected(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ppp0", "lan_interface": "enp2s0", "firewall_mode": "block-all"}
        )
        self.assertIn("访问控制模式无效", error)

    def test_upstream_dns_disables_resolv_conf(self):
        text = routing.build_dnsmasq_config(self.config(dns_upstream="127.0.0.1#5353"))
        self.assertIn("no-resolv", text)
        self.assertIn("server=127.0.0.1#5353", text)

    def test_dependency_row_warns_when_public_ipv6_is_unfiltered(self):
        config = self.config()  # pd + open
        with patch.object(dependencies, "load_router_config", return_value=config), patch.object(
            dependencies, "gather_wired_network_info", return_value={"devices": []}
        ), patch.object(
            dependencies, "get_router_nat_status",
            return_value={"level": "ok", "summary": "", "details": "", "action": ""},
        ), patch.object(dependencies, "get_firewall_status", return_value={
            "available": True, "service_active": True, "service_enabled": True,
            "nat4": True, "nat6": False, "input_table": False, "nat4_packets": 0, "nat6_packets": 0, "errors": [],
        }), patch.object(dependencies, "_lan_has_address", return_value=True), patch.object(
            dependencies, "get_ip_forward_status", return_value={"ipv4": "1", "ipv6": "1", "persistent": ""}
        ), patch.object(
            dependencies, "get_netplan_renderer_summary",
            return_value={"ok": True, "level": "ok", "summary": "", "details": []},
        ), patch.object(dependencies, "get_pd_status", return_value={
            "available": True, "service_active": True, "service_enabled": True, "prefix": "",
            "delegated": "", "updated_at": "", "reason": "", "error": "",
        }):
            rows = dependencies.get_dependency_rows()

        row = next(item for item in rows if item["name"] == "入站访问控制")
        self.assertEqual(row["level"], "warning")
        self.assertIn("公网", row["detail"])

        protected_config = self.config(firewall_mode="protect")
        with patch.object(dependencies, "load_router_config", return_value=protected_config), patch.object(
            dependencies, "gather_wired_network_info", return_value={"devices": []}
        ), patch.object(
            dependencies, "get_router_nat_status",
            return_value={"level": "ok", "summary": "", "details": "", "action": ""},
        ), patch.object(dependencies, "get_firewall_status", return_value={
            "available": True, "service_active": True, "service_enabled": True,
            "nat4": True, "nat6": False, "input_table": True, "nat4_packets": 0, "nat6_packets": 0, "errors": [],
        }), patch.object(dependencies, "_lan_has_address", return_value=True), patch.object(
            dependencies, "get_ip_forward_status", return_value={"ipv4": "1", "ipv6": "1", "persistent": ""}
        ), patch.object(
            dependencies, "get_netplan_renderer_summary",
            return_value={"ok": True, "level": "ok", "summary": "", "details": []},
        ), patch.object(dependencies, "get_pd_status", return_value={
            "available": True, "service_active": True, "service_enabled": True, "prefix": "",
            "delegated": "", "updated_at": "", "reason": "", "error": "",
        }):
            rows = dependencies.get_dependency_rows()

        row = next(item for item in rows if item["name"] == "入站访问控制")
        self.assertEqual(row["level"], "ok")
        self.assertEqual(row["status"], "保护中")



if __name__ == "__main__":
    unittest.main()
