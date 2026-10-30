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
from router_panel import backup as backup_client
from router_panel import cake as cake_client
from router_panel import core
from router_panel import dependencies
from router_panel import firewall
from router_panel import network
from router_panel import network_parsers
from router_panel import networkd
from router_panel import routing
from router_panel import service_monitor
from router_panel import system
from router_panel import tailscale as tailscale_client
from router_panel import web_general
from router_panel import web_tools
from router_panel import web_routing
from router_panel.core import CommandResult


EXPECTED_ROUTES = {
    "/",
    "/clients",
    "/clients/device",
    "/clients/static",
    "/clients/static/delete",
    "/dependencies",
    "/dependencies/fix",
    "/healthz",
    "/login",
    "/logout",
    "/operations/<operation_id>",
    "/router",
    "/router/apply",
    "/settings",
    "/settings/backup",
    "/settings/backup/restore",
    "/settings/cake",
    "/settings/password",
    "/settings/reboot",
    "/static/<path:filename>",
    "/system",
    "/tools",
    "/tools/services/action",
    "/tools/services/save",
    "/tools/tailscale/config",
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
        self.assertIn("不能同时作为 WAN", error)

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

    def test_ipv4_is_served_and_ipv6_is_left_to_networkd(self):
        text = routing.build_dnsmasq_config(self.config())
        self.assertIn("interface=eth1", text)
        self.assertIn("bind-interfaces", text)
        self.assertIn("dhcp-range=192.168.50.100,192.168.50.200,255.255.255.0,12h", text)
        self.assertIn("dhcp-option=option:router,192.168.50.1", text)
        # IPv6 addressing and RAs belong to systemd-networkd now.
        self.assertNotIn("enable-ra", text)
        self.assertNotIn("ra-stateless", text)
        self.assertNotIn("option6", text)

    def test_dns_binds_lan_addresses_only(self):
        text = routing.build_dnsmasq_config(self.config())
        self.assertIn("listen-address=192.168.50.1", text)
        self.assertIn("listen-address=fe80::1", text)
        self.assertIn("listen-address=fd00:50::1", text)
        # No explicit upstream: dnsmasq reads the /etc/resolv.conf the panel maintains.
        self.assertNotIn("server=", text)
        self.assertNotIn("no-resolv", text)

    def test_dhcp_disabled_removes_v4_range(self):
        text = routing.build_dnsmasq_config(self.config(dhcp_enabled="0"))
        self.assertNotIn("dhcp-range=192.168", text)

    def test_ipv6_off_does_not_bind_the_rdnss_address(self):
        """networkd only adds fe80::1 when IPv6 is on; binding it kills dnsmasq."""
        text = routing.build_dnsmasq_config(self.config(ipv6_mode="off"))
        self.assertNotIn("listen-address=fe80::1", text)
        self.assertIn("listen-address=192.168.50.1", text)
        # And networkd really does not configure it in that mode.
        self.assertNotIn("Address=fe80::1/64", networkd.render_lan_network(self.config(ipv6_mode="off")))
        self.assertIn("Address=fe80::1/64", networkd.render_lan_network(self.config()))

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

    def test_ensure_include_drops_the_global_flush(self):
        """A global flush would delete Docker/Tailscale/Podman tables on reload."""
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "nftables.conf"
            conf_dir = Path(directory) / "nftables.d"
            dropin = Path(directory) / "systemd" / "10-linux-router.conf"
            conf.write_text(
                "#!/usr/sbin/nft -f\nflush ruleset\n\ntable inet filter {\n}\n",
                encoding="utf-8",
            )
            with patch.object(firewall, "NFTABLES_CONFIG_PATH", conf), patch.object(
                firewall, "NFTABLES_CONF_DIR", conf_dir
            ), patch.object(firewall, "NFTABLES_DROPIN_PATH", dropin), patch.object(
                firewall, "NFTABLES_DROPIN_DIR", dropin.parent
            ), patch.object(
                firewall, "run_command", return_value=ok()
            ):
                result = firewall.ensure_nftables_include()
                self.assertTrue(result.ok)
                content = conf.read_text(encoding="utf-8")
                self.assertIn('include "/etc/nftables.d/*.nft"', content)
                self.assertNotIn("flush ruleset", content)
                self.assertFalse(firewall._nftables_conf_flushes())
                # The original content (possibly hand-written rules) is kept aside.
                backup = conf.with_name(conf.name + ".linux-router.bak")
                self.assertTrue(backup.exists())
                self.assertIn("flush ruleset", backup.read_text(encoding="utf-8"))
                # Debian stops with `nft flush ruleset`; the drop-in removes that.
                self.assertIn("ExecStop=", dropin.read_text(encoding="utf-8"))

    def test_ensure_include_leaves_a_clean_file_alone(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "nftables.conf"
            conf_dir = Path(directory) / "nftables.d"
            dropin = Path(directory) / "systemd" / "10-linux-router.conf"
            conf.write_text(firewall.NFTABLES_CONF_TEMPLATE, encoding="utf-8")
            before = conf.read_text(encoding="utf-8")
            with patch.object(firewall, "NFTABLES_CONFIG_PATH", conf), patch.object(
                firewall, "NFTABLES_CONF_DIR", conf_dir
            ), patch.object(firewall, "NFTABLES_DROPIN_PATH", dropin), patch.object(
                firewall, "NFTABLES_DROPIN_DIR", dropin.parent
            ), patch.object(
                firewall, "run_command", return_value=ok()
            ):
                result = firewall.ensure_nftables_include()
            self.assertTrue(result.ok)
            self.assertEqual(conf.read_text(encoding="utf-8"), before)
            self.assertFalse(conf.with_name(conf.name + ".linux-router.bak").exists())


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


class DeviceLabelTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "ens3",
            "lan_interface": "ens4",
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "off",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_label_is_stored_and_normalised(self):
        config, error = routing.set_device_label(self.config(), "3c:7c:3f:1a:2b:3c", "  客厅电视  ")
        self.assertIsNone(error)
        self.assertEqual(config["device_labels"], {"3C:7C:3F:1A:2B:3C": "客厅电视"})

    def test_empty_label_clears_the_entry(self):
        config, _ = routing.set_device_label(self.config(), "3c:7c:3f:1a:2b:3c", "客厅电视")
        config, error = routing.set_device_label(config, "3C:7C:3F:1A:2B:3C", "   ")
        self.assertIsNone(error)
        self.assertEqual(config["device_labels"], {})

    def test_label_validation(self):
        _, error = routing.set_device_label(self.config(), "xyz", "电视")
        self.assertIn("MAC", error)
        _, error = routing.set_device_label(self.config(), "3c:7c:3f:1a:2b:3c", "x" * 33)
        self.assertIn("32", error)

    def test_labels_survive_a_router_form_round_trip(self):
        config, _ = routing.set_device_label(self.config(), "3c:7c:3f:1a:2b:3c", "客厅电视")
        again, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_interface": "ens4", "lan_network": "192.168.50.0/24"},
            existing=config,
        )
        self.assertIsNone(error)
        self.assertEqual(again["device_labels"], config["device_labels"])

    def test_label_becomes_the_displayed_name(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "label-status"
        workdir.mkdir(exist_ok=True)
        lease_file = workdir / "dnsmasq.leases"
        lease_file.write_text("1750000000 3c:7c:3f:1a:2b:3c 192.168.50.130 nas *\n", encoding="utf-8")
        config_file = workdir / "router.json"
        config_file.write_text(
            json.dumps(
                {
                    "wan_interface": "ens3",
                    "lan_interface": "ens4",
                    "lan_network": "192.168.50.0/24",
                    "ipv6_mode": "off",
                    "device_labels": {"3C:7C:3F:1A:2B:3C": "客厅电视"},
                }
            ),
            encoding="utf-8",
        )
        with patch.object(routing, "DNSMASQ_LEASE_PATH", lease_file), patch.object(
            routing, "ROUTER_CONFIG_PATH", config_file
        ), patch.object(routing, "get_interface_neighbors", return_value=[]):
            status = routing.gather_client_status()

        client = status["clients"][0]
        self.assertEqual(client["label"], "客厅电视")
        self.assertEqual(client["name"], "客厅电视")
        # The reported hostname is still visible underneath.
        self.assertEqual(client["hostname"], "nas")

    def test_devices_list_keeps_label_only_entries(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "label-only"
        workdir.mkdir(exist_ok=True)
        config_file = workdir / "router.json"
        config_file.write_text(
            json.dumps(
                {
                    "wan_interface": "ens3",
                    "lan_interface": "ens4",
                    "lan_network": "192.168.50.0/24",
                    "ipv6_mode": "off",
                    "device_labels": {"22:33:44:55:66:77": "打印机"},
                }
            ),
            encoding="utf-8",
        )
        with patch.object(routing, "DNSMASQ_LEASE_PATH", workdir / "missing.leases"), patch.object(
            routing, "ROUTER_CONFIG_PATH", config_file
        ), patch.object(routing, "get_interface_neighbors", return_value=[]):
            status = routing.gather_client_status()

        self.assertEqual(status["clients"], [])
        self.assertEqual(len(status["devices"]), 1)
        self.assertEqual(status["devices"][0]["label"], "打印机")
        self.assertEqual(status["devices"][0]["ip"], "")
        self.assertEqual(status["devices"][0]["current_ip"], "")


class RegressionTests(unittest.TestCase):
    """Bugs found while reviewing: dead references after the networkd switch."""

    def test_pd_mode_config_load_does_not_crash(self):
        """load_router_config used to call the deleted pd module in PD mode."""
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-load"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(
            json.dumps(
                {
                    "wan_interface": "ppp0",
                    "lan_interface": "ens4",
                    "lan_ports": ["ens4"],
                    "ipv6_mode": "pd",
                }
            ),
            encoding="utf-8",
        )
        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            networkd, "get_delegated_prefix", return_value="2408:8207:6a1c:2f00::/64"
        ):
            config = routing.load_router_config()

        self.assertEqual(config["ipv6_prefix"], "2408:8207:6a1c:2f00::/64")
        # The LAN address follows the delegated prefix.
        self.assertEqual(str(routing.lan_ipv6_address(config)), "2408:8207:6a1c:2f00::1")

    def test_pd_prefix_is_never_persisted(self):
        """The delegated /64 is runtime state; saving it would leak into ULA mode."""
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-save"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path):
            config, _ = routing.normalize_router_config(
                {
                    "wan_interface": "ppp0",
                    "lan_ports": ["ens4"],
                    "lan_network": "192.168.50.0/24",
                    "ipv6_mode": "pd",
                }
            )
            config["ipv6_prefix"] = "2408:8207:6a1c:2f00::/64"
            routing.save_router_config(config)

        saved = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["ipv6_prefix"], "")
        self.assertEqual(saved["ipv6_mode"], "pd")

    def test_pd_mode_without_delegation_is_empty_not_fatal(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "pd-empty"
        workdir.mkdir(exist_ok=True)
        config_path = workdir / "router.json"
        config_path.write_text(
            json.dumps({"wan_interface": "ppp0", "lan_interface": "ens4", "lan_ports": ["ens4"], "ipv6_mode": "pd"}),
            encoding="utf-8",
        )
        with patch.object(routing, "ROUTER_CONFIG_PATH", config_path), patch.object(
            networkd, "get_delegated_prefix", return_value=""
        ):
            config = routing.load_router_config()

        self.assertEqual(config["ipv6_prefix"], "")
        self.assertIsNone(routing.lan_ipv6_address(config))

    def test_fix_lan_interface_uses_networkd(self):
        """That action used to call the removed NetworkManager helper."""
        config, _ = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_ports": ["ens4"], "lan_network": "192.168.50.0/24"}
        )
        with patch.object(dependencies, "load_router_config", return_value=config), patch.object(
            dependencies.networkd, "apply_networkd", return_value=ok("applied")
        ) as applied, patch.object(dependencies, "save_router_config"):
            result = dependencies.run_dependency_action("fix_lan_interface")

        self.assertTrue(result.ok, result.output)
        applied.assert_called_once()

    def test_bridge_is_not_a_wan_candidate(self):
        self.assertTrue(network.is_bridge("br-lan"))
        self.assertFalse(network.is_bridge("ens4"))
        self.assertFalse(network.is_bridge(""))

        interfaces = [
            {"ifname": "lo", "link_type": "loopback"},
            {"ifname": "ens3", "link_type": "ether", "operstate": "UP", "addr_info": []},
            {"ifname": "br-lan", "link_type": "bridge", "operstate": "UP", "addr_info": []},
            {"ifname": "ppp0", "link_type": "ppp", "operstate": "UNKNOWN", "addr_info": []},
        ]
        with patch.object(network, "_ip_json", return_value=interfaces), patch.object(
            network, "get_default_route_devs", return_value={}
        ):
            names = [item["name"] for item in network.list_all_interfaces()]

        self.assertEqual(names, ["ens3", "ppp0"])


class LanBridgeTests(unittest.TestCase):
    def config(self, ports, **overrides):
        payload = {
            "wan_interface": "ens3",
            "lan_ports": ports,
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "off",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_single_port_stays_direct(self):
        config = self.config(["ens4"])
        self.assertEqual(config["lan_interface"], "ens4")
        self.assertFalse(networkd.is_bridging(config))
        self.assertIn("Name=ens4", networkd.render_lan_network(config))

    def test_two_ports_are_bridged(self):
        config = self.config(["ens4", "ens5"])
        self.assertEqual(config["lan_interface"], networkd.BRIDGE_NAME)
        self.assertTrue(networkd.is_bridging(config))
        # The L3 interface is the bridge, not either NIC.
        lan_network = networkd.render_lan_network(config)
        self.assertIn(f"Name={networkd.BRIDGE_NAME}", lan_network)
        self.assertIn("Address=192.168.50.1/24", lan_network)

        netdev = networkd.render_lan_bridge_netdev(config)
        self.assertIn(f"Name={networkd.BRIDGE_NAME}", netdev)
        self.assertIn("Kind=bridge", netdev)

        member = networkd.render_lan_port_network("ens5")
        self.assertIn("Name=ens5", member)
        self.assertIn(f"Bridge={networkd.BRIDGE_NAME}", member)

    def test_duplicate_ports_are_collapsed(self):
        config = self.config(["ens4", "ens4"])
        self.assertEqual(config["lan_ports"], ["ens4"])
        self.assertFalse(networkd.is_bridging(config))

    def test_no_ports_is_rejected(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_ports": []}
        )
        self.assertIn("至少选择一块 LAN 网口", error)

    def test_wan_cannot_be_a_lan_port(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_ports": ["ens3", "ens4"]}
        )
        self.assertIn("不能同时作为 WAN", error)

    def test_unknown_port_is_rejected(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_ports": ["ens4", "ens9"]},
            available_interfaces=["ens3", "ens4"],
        )
        self.assertIn("ens9", error)

    def test_comma_separated_string_is_accepted(self):
        config = self.config("ens4, ens5")
        self.assertEqual(config["lan_ports"], ["ens4", "ens5"])
        self.assertTrue(networkd.is_bridging(config))

    def test_dnsmasq_and_firewall_follow_the_bridge(self):
        config = self.config(["ens4", "ens5"], dhcp_enabled="1")
        dnsmasq = routing.build_dnsmasq_config(config)
        self.assertIn(f"interface={networkd.BRIDGE_NAME}", dnsmasq)
        self.assertIn("listen-address=192.168.50.1", dnsmasq)

        rules = firewall.render_nftables_ruleset(config)
        self.assertIn(f'iifname "{networkd.BRIDGE_NAME}"', rules)

    def test_apply_writes_bridge_and_member_files(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "bridge"
        workdir.mkdir(exist_ok=True)
        for stale in workdir.glob("40-linux-router-lan-*.network"):
            stale.unlink()
        config = self.config(["ens4", "ens5"])

        with patch.object(networkd, "NETWORKD_DIR", workdir), patch.object(
            networkd, "BRIDGE_NETDEV_PATH", workdir / "05-linux-router-br.netdev"
        ), patch.object(
            networkd, "LAN_NETWORK_PATH", workdir / "20-linux-router-lan.network"
        ), patch.object(
            networkd, "WAN_NETWORK_PATH", workdir / "10-linux-router-wan.network"
        ), patch.object(
            networkd, "PPP_NETWORK_PATH", workdir / "30-linux-router-ppp.network"
        ), patch.object(
            networkd, "DNS_SCRIPT_PATH", workdir / "dns.sh"
        ), patch.object(
            networkd, "DNS_SERVICE_PATH", workdir / "dns.service"
        ), patch.object(
            networkd, "DNS_TIMER_PATH", workdir / "dns.timer"
        ), patch.object(
            networkd, "command_exists", return_value=True
        ), patch.object(
            networkd, "is_service_active", return_value=False
        ), patch.object(
            networkd, "is_service_enabled", return_value=False
        ), patch.object(
            networkd, "run_command", return_value=ok()
        ), patch.object(
            networkd, "wait_for_lan_address", return_value=True
        ):
            result = networkd.apply_networkd(config)

        self.assertTrue(result.ok, result.output)
        self.assertTrue((workdir / "05-linux-router-br.netdev").exists())
        self.assertTrue((workdir / "40-linux-router-lan-ens4.network").exists())
        self.assertTrue((workdir / "40-linux-router-lan-ens5.network").exists())

    def test_switching_back_to_one_port_removes_the_bridge(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "bridge-undo"
        workdir.mkdir(exist_ok=True)
        netdev = workdir / "05-linux-router-br.netdev"
        netdev.write_text("[NetDev]\n", encoding="utf-8")
        member = workdir / "40-linux-router-lan-ens5.network"
        member.write_text("[Match]\n", encoding="utf-8")

        config = self.config(["ens4"])
        with patch.object(networkd, "NETWORKD_DIR", workdir), patch.object(
            networkd, "BRIDGE_NETDEV_PATH", netdev
        ), patch.object(
            networkd, "LAN_NETWORK_PATH", workdir / "20-linux-router-lan.network"
        ), patch.object(
            networkd, "WAN_NETWORK_PATH", workdir / "10-linux-router-wan.network"
        ), patch.object(
            networkd, "PPP_NETWORK_PATH", workdir / "30-linux-router-ppp.network"
        ), patch.object(
            networkd, "DNS_SCRIPT_PATH", workdir / "dns.sh"
        ), patch.object(
            networkd, "DNS_SERVICE_PATH", workdir / "dns.service"
        ), patch.object(
            networkd, "DNS_TIMER_PATH", workdir / "dns.timer"
        ), patch.object(
            networkd, "command_exists", return_value=True
        ), patch.object(
            networkd, "is_service_active", return_value=False
        ), patch.object(
            networkd, "is_service_enabled", return_value=False
        ), patch.object(
            networkd, "run_command", return_value=ok()
        ), patch.object(
            networkd, "wait_for_lan_address", return_value=True
        ):
            result = networkd.apply_networkd(config)

        self.assertTrue(result.ok, result.output)
        self.assertFalse(netdev.exists(), "桥接 netdev 应被删除")
        self.assertFalse(member.exists(), "成员口 network 应被删除")

    def test_router_page_renders_lan_checkboxes(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        config = self.config(["ens4", "ens5"])
        status = {
            "config": config,
            "interfaces": [
                {"name": "ens4", "link_speed": "1 Gbps"},
                {"name": "ens5", "link_speed": "1 Gbps"},
                {"name": "ens6", "link_speed": "100 Mbps"},
            ],
            "wan_candidates": [{"name": "ens3"}],
            "backend": {"service_active": True},
            "wan": {}, "lan": {}, "ipv6": {}, "dhcp": {}, "firewall": {},
            "forwarding": {}, "services": {}, "pd": {}, "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            body = client.get("/router").get_data(as_text=True)

        self.assertIn('name="lan_ports"', body)
        self.assertIn('value="ens4"', body)
        self.assertIn('value="ens5"', body)
        # The two configured ports are pre-checked, the spare one is not.
        checked = [line for line in body.splitlines() if "data-lan-port" in line or "checked" in line]
        self.assertTrue(any("checked" in line for line in checked))
        self.assertIn("br-lan", body)

    def test_backup_accepts_generated_member_files(self):
        self.assertTrue(
            backup_client.PORT_NETWORK_RE.match(
                "/etc/systemd/network/40-linux-router-lan-ens5.network"
            )
        )
        self.assertFalse(
            backup_client.PORT_NETWORK_RE.match("/etc/systemd/network/40-linux-router-lan-../passwd")
        )


class StaticLeaseTests(unittest.TestCase):
    def config(self, **overrides):
        payload = {
            "wan_interface": "ens3",
            "lan_interface": "ens4",
            "lan_network": "192.168.50.0/24",
            "ipv6_mode": "off",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_binding_is_written_as_dhcp_host(self):
        config, error = routing.set_static_lease(
            self.config(), "3C:7C:3F:1A:2B:3C", "192.168.50.10", "nas"
        )
        self.assertIsNone(error)
        text = routing.build_dnsmasq_config(config)
        self.assertIn("dhcp-host=3C:7C:3F:1A:2B:3C,192.168.50.10,nas", text)

    def test_binding_without_hostname_has_two_fields(self):
        config, _ = routing.set_static_lease(self.config(), "3C:7C:3F:1A:2B:3C", "192.168.50.10")
        self.assertIn("dhcp-host=3C:7C:3F:1A:2B:3C,192.168.50.10\n", routing.build_dnsmasq_config(config))

    def test_invalid_bindings_are_rejected(self):
        config = self.config()
        cases = [
            ("not-a-mac", "192.168.50.10", "MAC"),
            ("11:22:33:44:55:66", "192.168.50.10", "MAC"),  # multicast bit set
            ("3c:7c:3f:1a:2b:3c", "192.168.60.5", "LAN 网段"),
            ("3c:7c:3f:1a:2b:3c", "192.168.50.1", "网关"),
            ("3c:7c:3f:1a:2b:3c", "192.168.50.0", "网段地址"),
            ("3c:7c:3f:1a:2b:3c", "192.168.50.255", "广播"),
            ("3c:7c:3f:1a:2b:3c", "192.168.50.10", None),
        ]
        for mac, ip, expected in cases:
            _, error = routing.validate_static_lease(config, mac, ip)
            if expected is None:
                self.assertIsNone(error, f"{mac} {ip}")
            else:
                self.assertIn(expected, error, f"{mac} {ip}")

    def test_bad_hostname_is_rejected(self):
        _, error = routing.validate_static_lease(
            self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10", "bad host!"
        )
        self.assertIn("主机名", error)

    def test_same_mac_replaces_the_previous_address(self):
        config, _ = routing.set_static_lease(self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10")
        config, error = routing.set_static_lease(config, "3C:7C:3F:1A:2B:3C", "192.168.50.20")
        self.assertIsNone(error)
        self.assertEqual(len(config["static_leases"]), 1)
        self.assertEqual(config["static_leases"][0]["ip"], "192.168.50.20")

    def test_two_macs_cannot_share_one_address(self):
        config, _ = routing.set_static_lease(self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10")
        _, error = routing.set_static_lease(config, "22:33:44:55:66:77", "192.168.50.10")
        self.assertIn("已绑定给", error)

    def test_remove_binding(self):
        config, _ = routing.set_static_lease(self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10")
        config, error = routing.remove_static_lease(config, "3C:7C:3F:1A:2B:3C")
        self.assertIsNone(error)
        self.assertEqual(config["static_leases"], [])
        self.assertNotIn("dhcp-host", routing.build_dnsmasq_config(config))

        _, error = routing.remove_static_lease(config, "3c:7c:3f:1a:2b:3c")
        self.assertIn("没有找到", error)

    def test_bindings_survive_a_router_form_round_trip(self):
        """The router form must not wipe the list it never submits."""
        config, _ = routing.set_static_lease(self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10")
        again, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_interface": "ens4", "lan_network": "192.168.50.0/24"},
            existing=config,
        )
        self.assertIsNone(error)
        self.assertEqual(again["static_leases"], config["static_leases"])

    def test_client_status_marks_reserved_devices(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "static-lease-status"
        workdir.mkdir(exist_ok=True)
        lease_file = workdir / "dnsmasq.leases"
        lease_file.write_text("1750000000 3c:7c:3f:1a:2b:3c 192.168.50.10 nas *\n", encoding="utf-8")
        config_file = workdir / "router.json"
        config_file.write_text(
            json.dumps(
                {
                    "wan_interface": "ens3",
                    "lan_interface": "ens4",
                    "lan_network": "192.168.50.0/24",
                    "ipv6_mode": "off",
                    "static_leases": [
                        {"mac": "3C:7C:3F:1A:2B:3C", "ip": "192.168.50.10", "hostname": "nas"},
                        {"mac": "22:33:44:55:66:77", "ip": "192.168.50.11", "hostname": ""},
                    ],
                }
            ),
            encoding="utf-8",
        )
        with patch.object(routing, "DNSMASQ_LEASE_PATH", lease_file), patch.object(
            routing, "ROUTER_CONFIG_PATH", config_file
        ), patch.object(routing, "get_interface_neighbors", return_value=[]):
            status = routing.gather_client_status()

        nas = next(item for item in status["clients"] if item["mac_address"] == "3C:7C:3F:1A:2B:3C")
        self.assertEqual(nas["reserved"], "1")
        self.assertEqual(nas["static_ip"], "192.168.50.10")
        # Offline bindings are still listed so they can be removed.
        self.assertEqual(len(status["devices"]), 2)
        offline = next(item for item in status["devices"] if item["mac"] == "22:33:44:55:66:77")
        self.assertEqual(offline["current_ip"], "")
        self.assertEqual(offline["online"], "0")

    def test_apply_writes_dnsmasq_and_restarts_it(self):
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            return ok()

        config, _ = routing.set_static_lease(self.config(), "3c:7c:3f:1a:2b:3c", "192.168.50.10")
        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "ensure_dnsmasq_include", return_value=ok()
        ), patch.object(routing, "atomic_write_text"), patch.object(
            routing, "save_router_config"
        ) as saver, patch.object(routing, "run_command", side_effect=fake_run):
            result = routing.apply_static_leases(config)

        self.assertTrue(result.ok, result.output)
        saver.assert_called_once()
        flat = [" ".join(call) for call in calls]
        self.assertTrue(any("dnsmasq --test" in item for item in flat))
        self.assertTrue(any("restart dnsmasq.service" in item for item in flat))

    def test_clients_page_renders_binding_controls(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        status = {
            "clients": [
                {
                    "name": "客厅 NAS", "label": "客厅 NAS", "hostname": "nas",
                    "mac_address": "3C:7C:3F:1A:2B:3C", "duid": "",
                    "ipv4": "192.168.50.10", "ipv6": [], "ipv6_label": "", "lease": "11h", "online": "1",
                    "static_ip": "192.168.50.10", "reserved": "1",
                },
                {
                    "name": "phone", "label": "", "hostname": "", "mac_address": "22:33:44:55:66:77", "duid": "",
                    "ipv4": "192.168.50.130", "ipv6": [], "ipv6_label": "", "lease": "5h", "online": "1",
                    "static_ip": "", "reserved": "0",
                },
            ],
            "total": 2, "online": 2, "lan_interface": "ens4",
            "devices": [
                {"mac": "3C:7C:3F:1A:2B:3C", "label": "客厅 NAS", "ip": "192.168.50.10",
                 "hostname": "nas", "current_ip": "192.168.50.10", "online": "1"},
                {"mac": "22:33:44:55:66:77", "label": "手机", "ip": "",
                 "hostname": "", "current_ip": "192.168.50.130", "online": "1"},
            ],
            "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            body = client.get("/clients").get_data(as_text=True)

        self.assertIn("备注与固定地址", body)
        self.assertIn('action="/clients/device"', body)
        self.assertIn('action="/clients/static/delete"', body)
        # Bound device shows its address, the free one gets a pre-filling link.
        self.assertIn("固定 192.168.50.10", body)
        self.assertIn("mac=22:33:44:55:66:77", body)
        self.assertIn("ip=192.168.50.130", body)
        # Labels from device_labels show up in both the table and the device list.
        self.assertIn("客厅 NAS", body)
        self.assertIn("手机", body)


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
        ), patch.object(routing, "ensure_network_backend", return_value=ok("networkd ready")), patch.object(routing, "apply_cake", return_value=ok("cake skipped")), patch.object(
            routing, "is_service_active", return_value=True
        ), patch.object(routing.time, "sleep", return_value=None):
            result = routing.apply_router_config(config)

        self.assertTrue(result.ok, result.output)
        self.assertIn("interface=eth1", dnsmasq_rules.read_text(encoding="utf-8"))
        self.assertTrue(sysctl_path.exists())
        self.assertTrue(router_json.exists())
        self.assertIn(["systemctl", "restart", "dnsmasq.service"], commands)

    def test_apply_reports_progress_steps(self):
        """The panel shows these strings while the apply is running."""
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        workdir = Path(TEST_DATA_DIRECTORY.name) / "progress"
        workdir.mkdir(exist_ok=True)
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")
        steps: list[str] = []

        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf
        ), patch.object(routing, "DNSMASQ_CONF_DIR", workdir), patch.object(
            routing, "DNSMASQ_RULESET_PATH", workdir / "linux-router.conf"
        ), patch.object(routing, "ROUTER_PANEL_SYSCTL_PATH", workdir / "sysctl.conf"), patch.object(
            routing, "ROUTER_CONFIG_PATH", workdir / "router.json"
        ), patch.object(routing, "run_command", return_value=ok()), patch.object(
            routing, "apply_ruleset", return_value=ok("rules loaded")
        ), patch.object(routing, "ensure_network_backend", return_value=ok("")), patch.object(
            routing, "apply_cake", return_value=ok("")
        ), patch.object(routing, "is_service_active", return_value=True), patch.object(
            routing.time, "sleep", return_value=None
        ):
            result = routing.apply_router_config(config, progress=steps.append)

        self.assertTrue(result.ok, result.output)
        self.assertEqual(
            steps,
            [
                "写入 dnsmasq 配置",
                "写入 systemd-networkd 配置",
                "启动 dnsmasq",
                "同步上游 DNS",
                "写入 nftables 规则",
                "启用 IPv4/IPv6 转发",
                "应用 CAKE 限速",
            ],
        )

    def test_apply_stops_at_the_first_failing_step(self):
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        workdir = Path(TEST_DATA_DIRECTORY.name) / "progress-fail"
        workdir.mkdir(exist_ok=True)
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")
        steps: list[str] = []

        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf
        ), patch.object(routing, "DNSMASQ_CONF_DIR", workdir), patch.object(
            routing, "DNSMASQ_RULESET_PATH", workdir / "linux-router.conf"
        ), patch.object(routing, "ROUTER_CONFIG_PATH", workdir / "router.json"), patch.object(
            routing, "run_command", return_value=ok()
        ), patch.object(routing, "apply_ruleset", return_value=fail("nft: syntax error")), patch.object(
            routing, "ensure_network_backend", return_value=ok("")
        ), patch.object(routing, "is_service_active", return_value=True), patch.object(
            routing.time, "sleep", return_value=None
        ):
            result = routing.apply_router_config(config, progress=steps.append)

        self.assertFalse(result.ok)
        self.assertIn("nft: syntax error", result.output)
        self.assertEqual(steps[-1], "写入 nftables 规则")
        # dnsmasq comes right after networkd, so it is already up before nft fails.
        self.assertIn("启动 dnsmasq", steps)


    def test_apply_reports_dnsmasq_that_dies_immediately(self):
        """dnsmasq exits at once when its listen-address is not on the NIC yet."""
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        workdir = Path(TEST_DATA_DIRECTORY.name) / "dnsmasq-dead"
        workdir.mkdir(exist_ok=True)
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")

        def fake_run(command, timeout=8, env=None):
            if command[:1] == ["journalctl"]:
                return ok("dnsmasq: failed to create listening socket: Cannot assign requested address")
            return ok()

        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf
        ), patch.object(routing, "DNSMASQ_CONF_DIR", workdir), patch.object(
            routing, "DNSMASQ_RULESET_PATH", workdir / "linux-router.conf"
        ), patch.object(routing, "ROUTER_PANEL_SYSCTL_PATH", workdir / "sysctl.conf"), patch.object(
            routing, "ROUTER_CONFIG_PATH", workdir / "router.json"
        ), patch.object(routing, "run_command", side_effect=fake_run), patch.object(
            routing, "apply_ruleset", return_value=ok("")
        ), patch.object(routing, "ensure_network_backend", return_value=ok("")), patch.object(
            routing, "is_service_active", return_value=False
        ), patch.object(routing.time, "sleep", return_value=None):
            result = routing.apply_router_config(config)

        self.assertFalse(result.ok)
        self.assertIn("dnsmasq", result.output)
        self.assertIn("Cannot assign requested address", result.output)

    def test_apply_surfaces_the_journal_reason_when_restart_fails(self):
        """systemd only says 'control process exited'; the journal says why."""
        config, _ = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "ipv6_prefix": "fd00:50::/64"}
        )
        workdir = Path(TEST_DATA_DIRECTORY.name) / "dnsmasq-restart-fail"
        workdir.mkdir(exist_ok=True)
        dnsmasq_conf = workdir / "dnsmasq.conf"
        dnsmasq_conf.write_text("conf-dir=/etc/dnsmasq.d/,*.conf\n", encoding="utf-8")

        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["systemctl", "restart"]:
                return fail("Job for dnsmasq.service failed because the control process exited with error code.")
            if command[:1] == ["journalctl"]:
                return ok(
                    "dnsmasq: failed to create listening socket for fe80::1: Invalid argument\n"
                    "dnsmasq: FAILED to start up"
                )
            return ok()

        with patch.object(routing, "command_exists", return_value=True), patch.object(
            routing, "DNSMASQ_CONFIG_PATH", dnsmasq_conf
        ), patch.object(routing, "DNSMASQ_CONF_DIR", workdir), patch.object(
            routing, "DNSMASQ_RULESET_PATH", workdir / "linux-router.conf"
        ), patch.object(routing, "ROUTER_PANEL_SYSCTL_PATH", workdir / "sysctl.conf"), patch.object(
            routing, "ROUTER_CONFIG_PATH", workdir / "router.json"
        ), patch.object(routing, "run_command", side_effect=fake_run), patch.object(
            routing, "apply_ruleset", return_value=ok("")
        ), patch.object(routing, "ensure_network_backend", return_value=ok("")), patch.object(
            routing, "is_service_active", return_value=False
        ), patch.object(routing.time, "sleep", return_value=None):
            result = routing.apply_router_config(config)

        self.assertFalse(result.ok)
        self.assertIn("fe80::1", result.output)
        self.assertIn("Invalid argument", result.output)


class LanAddressWaitTests(unittest.TestCase):
    def test_waits_until_the_address_is_on_the_interface(self):
        config = {"lan_interface": "eth1", "lan_network": "192.168.9.0/24"}
        outputs = [
            "",
            "3: eth1    inet 192.168.9.1/24 brd 192.168.9.255 scope global eth1",
        ]
        with patch.object(
            networkd, "run_command", side_effect=lambda *a, **k: ok(outputs.pop(0))
        ), patch.object(networkd.time, "sleep", return_value=None):
            self.assertTrue(networkd.wait_for_lan_address(config))

    def test_gives_up_after_the_timeout(self):
        config = {"lan_interface": "eth1", "lan_network": "192.168.9.0/24"}
        with patch.object(networkd, "run_command", return_value=ok("")), patch.object(
            networkd.time, "sleep", return_value=None
        ), patch.object(networkd.time, "monotonic", side_effect=[0.0, 100.0]):
            self.assertFalse(networkd.wait_for_lan_address(config))


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

    def test_router_page_marks_the_uplink_port_unusable_for_lan(self):
        """The uplink NIC must render unchecked+disabled, in both WAN modes."""
        import re

        self.login()
        interfaces = [
            {"name": "eth0", "link_speed": "1 Gbps"},
            {"name": "eth1", "link_speed": "1 Gbps"},
            {"name": "eth2", "link_speed": "1 Gbps"},
        ]
        base = {
            "wan": {},
            "lan": {},
            "ipv6": {},
            "dhcp": {},
            "firewall": {},
            "forwarding": {},
            "services": {},
            "wan_candidates": [],
            "errors": [],
        }

        def port_blocks(body: str) -> dict[str, str]:
            return {
                match.group(1): match.group(0)
                for match in re.finditer(
                    r'<input\s+type="checkbox"\s+name="lan_ports"\s+value="([^"]+)"[^>]*>',
                    body,
                    re.S,
                )
            }

        cases = [
            ({"wan_mode": "pppoe", "pppoe_username": "u@isp", "pppoe_nic": "eth1"}, "eth1"),
            ({"wan_mode": "dhcp", "wan_interface": "eth1"}, "eth1"),
        ]
        for payload, uplink in cases:
            config, error = routing.normalize_router_config(
                {**payload, "lan_ports": ["eth0", "eth2"], "lan_network": "192.168.50.0/24"},
                existing=routing.load_router_config(),
            )
            self.assertIsNone(error)
            status = {**base, "config": config, "interfaces": interfaces}
            with patch.object(web_routing, "query_agent", return_value=status):
                body = self.client.get("/router").get_data(as_text=True)
            blocks = port_blocks(body)
            self.assertEqual(set(blocks), {"eth0", "eth1", "eth2"})
            self.assertIn("disabled", blocks[uplink], payload)
            self.assertNotIn("checked", blocks[uplink], payload)
            self.assertIn("checked", blocks["eth0"], payload)
            self.assertNotIn("disabled", blocks["eth0"], payload)

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

    def test_lockout_guard_blocks_wan_side_apply(self):
        """Turning protect on from the WAN without allowing the panel port locks you out."""
        params = {"firewall_mode": "protect", "firewall_wan_tcp": ""}
        with application.app.test_request_context(
            environ_base={"REMOTE_ADDR": "192.168.9.11", "SERVER_PORT": "80"}
        ), patch.object(web_routing, "load_router_config", return_value={"lan_network": "192.168.50.0/24"}):
            warning = web_routing.lockout_warning(params)

        self.assertIn("WAN 侧", warning)
        self.assertIn("80", warning)

    def test_lockout_guard_allows_when_panel_port_is_open(self):
        for allowed in ("22,80", "70-90"):
            params = {"firewall_mode": "protect", "firewall_wan_tcp": allowed}
            with application.app.test_request_context(
                environ_base={"REMOTE_ADDR": "192.168.9.11", "SERVER_PORT": "80"}
            ), patch.object(
                web_routing, "load_router_config", return_value={"lan_network": "192.168.50.0/24"}
            ):
                self.assertEqual(web_routing.lockout_warning(params), "", allowed)

    def test_lockout_guard_is_quiet_on_the_lan_side(self):
        params = {"firewall_mode": "protect", "firewall_wan_tcp": ""}
        with application.app.test_request_context(
            environ_base={"REMOTE_ADDR": "192.168.50.23", "SERVER_PORT": "80"}
        ), patch.object(web_routing, "load_router_config", return_value={"lan_network": "192.168.50.0/24"}):
            self.assertEqual(web_routing.lockout_warning(params), "")

    def test_lockout_guard_ignores_open_mode(self):
        params = {"firewall_mode": "open", "firewall_wan_tcp": ""}
        with application.app.test_request_context(
            environ_base={"REMOTE_ADDR": "192.168.9.11", "SERVER_PORT": "80"}
        ), patch.object(web_routing, "load_router_config", return_value={"lan_network": "192.168.50.0/24"}):
            self.assertEqual(web_routing.lockout_warning(params), "")

    def test_dependency_row_warns_when_public_ipv6_is_unfiltered(self):
        config = self.config()  # pd + open
        with patch.object(dependencies, "load_router_config", return_value=config), patch.object(
            dependencies, "get_router_nat_status",
            return_value={"level": "ok", "summary": "", "details": "", "action": ""},
        ), patch.object(dependencies, "get_firewall_status", return_value={
            "available": True, "service_active": True, "service_enabled": True,
            "nat4": True, "nat6": False, "input_table": False, "nat4_packets": 0, "nat6_packets": 0, "errors": [],
        }), patch.object(dependencies, "_lan_has_address", return_value=True), patch.object(
            dependencies, "get_ip_forward_status", return_value={"ipv4": "1", "ipv6": "1", "persistent": ""}
        ):
            rows = dependencies.get_dependency_rows()

        row = next(item for item in rows if item["name"] == "入站访问控制")
        self.assertEqual(row["level"], "warning")
        self.assertIn("公网", row["detail"])

        protected_config = self.config(firewall_mode="protect")
        with patch.object(dependencies, "load_router_config", return_value=protected_config), patch.object(
            dependencies, "get_router_nat_status",
            return_value={"level": "ok", "summary": "", "details": "", "action": ""},
        ), patch.object(dependencies, "get_firewall_status", return_value={
            "available": True, "service_active": True, "service_enabled": True,
            "nat4": True, "nat6": False, "input_table": True, "nat4_packets": 0, "nat6_packets": 0, "errors": [],
        }), patch.object(dependencies, "_lan_has_address", return_value=True), patch.object(
            dependencies, "get_ip_forward_status", return_value={"ipv4": "1", "ipv6": "1", "persistent": ""}
        ):
            rows = dependencies.get_dependency_rows()

        row = next(item for item in rows if item["name"] == "入站访问控制")
        self.assertEqual(row["level"], "ok")
        self.assertEqual(row["status"], "保护中")




class PackageInstallTests(unittest.TestCase):
    def test_update_failure_does_not_block_install(self):
        """A stale cdrom:// source makes apt-get update fail but the install can still work."""
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["apt-get", "update"]:
                return fail("Err:2 cdrom://... does not have a Release file")
            return ok("Setting up dhcpcd")

        with patch.object(dependencies, "package_installed", side_effect=lambda name: False), patch.object(
            dependencies, "run_command", side_effect=fake_run
        ):
            result = dependencies.install_missing_packages()

        self.assertTrue(result.ok)
        self.assertIn("有失败项", result.output)

    def test_install_failure_is_reported(self):
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["apt-get", "update"]:
                return fail("Err:2 cdrom://... does not have a Release file")
            return fail("E: Unable to locate package dhcpcd")

        with patch.object(dependencies, "package_installed", side_effect=lambda name: False), patch.object(
            dependencies, "run_command", side_effect=fake_run
        ):
            result = dependencies.install_missing_packages()

        self.assertFalse(result.ok)
        self.assertIn("Unable to locate", result.output)


class CakeShapingTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()

    def login(self):
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"

    def config(self, **overrides):
        payload = {
            "wan_interface": "ens3",
            "lan_interface": "ens4",
            "lan_network": "192.168.50.0/24",
            "cake_enabled": "1",
            "cake_upload_mbit": "30",
            "cake_download_mbit": "200",
        }
        payload.update(overrides)
        config, error = routing.normalize_router_config(payload)
        assert error is None, error
        return config

    def test_bandwidth_validation(self):
        self.assertEqual(cake_client.normalize_bandwidth("30", required=True, label="上行"), (30, None))
        self.assertEqual(cake_client.normalize_bandwidth("", required=False, label="下行"), (0, None))
        self.assertIn("不能为空", cake_client.normalize_bandwidth("", required=True, label="上行")[1])
        self.assertIn("必须是整数", cake_client.normalize_bandwidth("3O", required=True, label="上行")[1])
        self.assertIn("不能超过", cake_client.normalize_bandwidth("999999", required=True, label="上行")[1])
        self.assertIn("必须大于 0", cake_client.normalize_bandwidth("0", required=True, label="上行")[1])

    def test_config_rejects_enabled_without_upload(self):
        _, error = routing.normalize_router_config(
            {"wan_interface": "ens3", "lan_interface": "ens4", "cake_enabled": "1", "cake_upload_mbit": ""}
        )
        self.assertIn("CAKE 上行带宽", error)

    def test_config_defaults_to_disabled(self):
        config, error = routing.normalize_router_config({"wan_interface": "ens3", "lan_interface": "ens4"})
        self.assertIsNone(error)
        self.assertFalse(config["cake_enabled"])
        self.assertEqual(config["cake_upload_mbit"], 0)

    def test_script_sets_both_bandwidths(self):
        text = cake_client.render_cake_script(self.config())
        self.assertIn('WAN="ens3"', text)
        self.assertIn('UPLOAD="30"', text)
        self.assertIn('DOWNLOAD="200"', text)
        self.assertIn('cake bandwidth "${UPLOAD}mbit"', text)
        self.assertIn('cake bandwidth "${DOWNLOAD}mbit"', text)
        self.assertIn("mirred egress redirect dev", text)
        self.assertIn(f'IFB="{cake_client.CAKE_IFB_DEVICE}"', text)

    def test_script_is_valid_posix_shell(self):
        import subprocess

        for config in (self.config(), self.config(cake_enabled="", cake_upload_mbit="")):
            script = Path(TEST_DATA_DIRECTORY.name) / "cake.sh"
            script.write_text(cake_client.render_cake_script(config), encoding="utf-8")
            checked = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
            self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_disabled_script_has_zero_upload(self):
        text = cake_client.render_cake_script(self.config(cake_enabled="", cake_upload_mbit="30"))
        self.assertIn('UPLOAD="0"', text)
        self.assertIn("clear_all", text)

    def test_service_unit_is_oneshot_with_clear(self):
        text = cake_client.render_service_unit()
        self.assertIn("Type=oneshot", text)
        self.assertIn("RemainAfterExit=yes", text)
        self.assertIn("ExecStop=", text)

    def test_timer_reapplies_after_reconnect(self):
        text = cake_client.render_timer_unit()
        self.assertIn("OnUnitActiveSec=5min", text)
        self.assertIn("WantedBy=timers.target", text)

    def test_apply_enables_service_and_timer(self):
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            return ok()

        with patch.object(cake_client, "command_exists", return_value=True), patch.object(
            cake_client, "atomic_write_text"
        ), patch.object(cake_client, "run_command", side_effect=fake_run):
            result = cake_client.apply_cake(self.config())

        self.assertTrue(result.ok, result.output)
        self.assertIn("30 Mbit/s", result.output)
        self.assertIn("200 Mbit/s", result.output)
        flat = [" ".join(call) for call in calls]
        self.assertTrue(any("restart linux-router-cake.service" in item for item in flat))
        self.assertTrue(any("enable --now linux-router-cake.timer" in item for item in flat))

    def test_apply_disabled_clears_and_stops(self):
        calls = []

        def fake_run(command, timeout=8, env=None):
            calls.append(command)
            return ok()

        with patch.object(cake_client, "command_exists", return_value=True), patch.object(
            cake_client, "atomic_write_text"
        ), patch.object(cake_client, "run_command", side_effect=fake_run):
            result = cake_client.apply_cake(self.config(cake_enabled="", cake_upload_mbit=""))

        self.assertTrue(result.ok, result.output)
        self.assertIn("已关闭", result.output)
        flat = [" ".join(call) for call in calls]
        self.assertTrue(any("disable --now linux-router-cake.timer" in item for item in flat))
        self.assertTrue(any(item.endswith("clear") for item in flat))

    def test_status_parses_cake_bandwidth(self):
        def fake_run(command, timeout=8, env=None):
            if command[:3] == ["tc", "qdisc", "show"] and command[-1] == "ens3":
                return ok("qdisc cake 8001: root refcnt 2 bandwidth 30Mbit diffserv3 triple-isolate")
            if command[:3] == ["tc", "qdisc", "show"] and command[-1] == cake_client.CAKE_IFB_DEVICE:
                return ok("qdisc cake 8002: root refcnt 2 bandwidth 200Mbit diffserv3")
            return ok("")

        with patch.object(cake_client, "command_exists", return_value=True), patch.object(
            cake_client, "is_service_active", return_value=True
        ), patch.object(cake_client, "run_command", side_effect=fake_run):
            status = cake_client.get_cake_status(self.config())

        self.assertTrue(status["active"])
        self.assertEqual(status["wan_bandwidth"], 30)
        self.assertEqual(status["download_bandwidth"], 200)
        self.assertEqual(status["errors"], [])

    def test_status_reports_missing_tc(self):
        with patch.object(cake_client, "command_exists", return_value=False):
            status = cake_client.get_cake_status(self.config())
        self.assertFalse(status["available"])
        self.assertTrue(status["errors"])

    def test_apply_writes_real_files(self):
        """Uses the real atomic_write_text, so the path constants must be Paths."""
        workdir = Path(TEST_DATA_DIRECTORY.name) / "cake-apply"
        workdir.mkdir(exist_ok=True)
        script = workdir / "cake.sh"
        service = workdir / "cake.service"
        timer = workdir / "cake.timer"

        with patch.object(cake_client, "CAKE_SCRIPT_PATH", script), patch.object(
            cake_client, "CAKE_SERVICE_PATH", service
        ), patch.object(
            cake_client, "CAKE_TIMER_PATH", timer
        ), patch.object(
            cake_client, "command_exists", return_value=True
        ), patch.object(
            cake_client, "run_command", return_value=ok()
        ):
            result = cake_client.apply_cake(self.config())

        self.assertTrue(result.ok, result.output)
        self.assertIn('UPLOAD="30"', script.read_text(encoding="utf-8"))
        self.assertTrue(script.stat().st_mode & 0o111)
        self.assertIn("Type=oneshot", service.read_text(encoding="utf-8"))
        self.assertIn("OnUnitActiveSec=5min", timer.read_text(encoding="utf-8"))

    def test_settings_page_shows_cake_card(self):
        self.login()
        status = {
            "enabled": True, "active": True, "available": True, "wan": "ens3",
            "upload_mbit": 30, "download_mbit": 200,
            "wan_bandwidth": 30, "download_bandwidth": 200,
            "service_active": True, "wan_qdisc": "", "ifb_qdisc": "", "errors": [],
        }
        with patch.object(web_general, "query_agent", return_value=status):
            response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        body = response.get_data(as_text=True)
        self.assertIn("CAKE 限速", body)
        self.assertIn('name="cake_upload_mbit"', body)
        self.assertIn("30 Mbit/s", body)
class NetworkdBackendTests(unittest.TestCase):
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

    def test_lan_network_ula_owns_ra(self):
        text = networkd.render_lan_network(self.config())
        self.assertIn("Name=eth1", text)
        self.assertIn("Address=192.168.50.1/24", text)
        self.assertIn("Address=fd00:50::1/64", text)
        self.assertIn("Address=fe80::1/64", text)
        self.assertIn("IPv6SendRA=yes", text)
        self.assertIn("EmitDNS=yes", text)
        self.assertIn("IPForward=yes", text)
        self.assertNotIn("DHCPPrefixDelegation", text)

    def test_lan_network_pd_delegates(self):
        text = networkd.render_lan_network(self.config(ipv6_mode="pd", ipv6_prefix=""))
        self.assertIn("DHCPPrefixDelegation=yes", text)
        self.assertIn("IPv6SendRA=yes", text)
        self.assertIn("IPv6AcceptRA=no", text)
        # The prefix is only known at runtime, so no static global address.
        self.assertNotIn("Address=fd", text)
        self.assertIn("Address=fe80::1/64", text)

    def test_lan_network_ipv6_off(self):
        text = networkd.render_lan_network(self.config(ipv6_mode="off", ipv6_prefix=""))
        self.assertNotIn("IPv6SendRA", text)
        self.assertNotIn("fe80::1", text)
        self.assertIn("Address=192.168.50.1/24", text)

    def test_wan_network_dhcp_dual_stack(self):
        text = networkd.render_wan_network(self.config())
        self.assertIn("Name=eth0", text)
        self.assertIn("DHCP=yes", text)
        self.assertIn("IPv6AcceptRA=yes", text)

    def test_wan_network_ipv4_only_and_mtu(self):
        text = networkd.render_wan_network(self.config(wan_ipv6_auto="", mtu="1492"))
        self.assertIn("DHCP=ipv4", text)
        self.assertIn("IPv6AcceptRA=no", text)
        self.assertIn("MTUBytes=1492", text)

    def test_ppp_network_requests_dhcpv6_without_ra(self):
        text = networkd.render_ppp_network()
        self.assertIn("Name=ppp0", text)
        self.assertIn("DHCP=ipv6", text)
        self.assertIn("WithoutRA=solicit", text)

    def test_pppoe_peers_file(self):
        config = self.config(
            wan_mode="pppoe", pppoe_nic="eth0", pppoe_username="adsl123", pppoe_password="testpass"
        )
        text = networkd.render_pppoe_peers(config)
        self.assertIn("plugin rp-pppoe.so", text)
        self.assertIn("nic-eth0", text)
        self.assertIn('user "adsl123"', text)
        self.assertIn("noauth", text)
        self.assertIn("defaultroute", text)
        self.assertIn("mtu 1492", text)
        self.assertIn("ipv6 ,", text)

    def test_pppoe_secrets_are_quoted(self):
        config = self.config(
            wan_mode="pppoe", pppoe_nic="eth0", pppoe_username="adsl123", pppoe_password="testpass"
        )
        self.assertEqual(networkd.render_pppoe_secrets(config), '"adsl123" * "testpass" *\n')

    def test_pppoe_forces_ppp0_and_requires_credentials(self):
        config = self.config(wan_mode="pppoe", pppoe_nic="eth0", pppoe_username="adsl123")
        self.assertEqual(config["wan_interface"], "ppp0")
        self.assertEqual(config["wan_mode"], "pppoe")

        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "wan_mode": "pppoe", "pppoe_nic": "eth0"}
        )
        self.assertIn("PPPoE 账号", error)

        _, error = routing.normalize_router_config(
            {"wan_interface": "eth0", "lan_interface": "eth1", "wan_mode": "pppoe", "pppoe_username": "x"}
        )
        self.assertIn("物理网口", error)

        _, error = routing.normalize_router_config(
            {
                "wan_interface": "eth0",
                "lan_interface": "eth1",
                "wan_mode": "pppoe",
                "pppoe_username": "x",
                "pppoe_nic": "eth1",
            }
        )
        self.assertIn("不能同时用于 PPPoE 拨号和 LAN", error)

    def test_ppp_ipup_hook_restores_cake_and_dns(self):
        """A redial recreates ppp0, so shaping and DNS must be restored at once."""
        import subprocess

        text = networkd.render_ppp_ipup_script()
        self.assertIn("/etc/linux-router/cake.sh ensure", text)
        self.assertIn("systemctl start linux-router-dns.service", text)

        script = Path(TEST_DATA_DIRECTORY.name) / "ipup.sh"
        script.write_text(text, encoding="utf-8")
        checked = subprocess.run(["sh", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_apply_writes_networkd_files(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "networkd"
        workdir.mkdir(exist_ok=True)
        wan_path = workdir / "10-wan.network"
        lan_path = workdir / "20-lan.network"
        ppp_path = workdir / "30-ppp.network"
        peer = workdir / "peers"
        chap = workdir / "chap-secrets"
        pap = workdir / "pap-secrets"
        unit = workdir / "pppoe.service"
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            return ok()

        config = self.config(
            wan_mode="pppoe", pppoe_nic="eth0", pppoe_username="adsl123", pppoe_password="testpass"
        )
        with patch.object(networkd, "WAN_NETWORK_PATH", wan_path), patch.object(
            networkd, "LAN_NETWORK_PATH", lan_path
        ), patch.object(
            networkd, "DNS_SCRIPT_PATH", workdir / "dns.sh"
        ), patch.object(
            networkd, "DNS_SERVICE_PATH", workdir / "dns.service"
        ), patch.object(
            networkd, "DNS_TIMER_PATH", workdir / "dns.timer"
        ), patch.object(
            networkd, "PPP_NETWORK_PATH", ppp_path
        ), patch.object(
            networkd, "PPPOE_NIC_NETWORK_PATH", workdir / "15-pppoe-nic.network"
        ), patch.object(
            networkd, "PPPOE_PEER_PATH", peer
        ), patch.object(
            networkd, "PPPOE_CHAP_PATH", chap
        ), patch.object(
            networkd, "PPPOE_PAP_PATH", pap
        ), patch.object(
            networkd, "PPPOE_SERVICE_PATH", unit
        ), patch.object(
            networkd, "PPPOE_IPUP_PATH", workdir / "90-linux-router"
        ), patch.object(
            networkd, "command_exists", return_value=True
        ), patch.object(
            networkd, "is_service_active", return_value=False
        ), patch.object(
            networkd, "is_service_enabled", return_value=False
        ), patch.object(
            networkd, "run_command", side_effect=fake_run
        ), patch.object(
            networkd, "wait_for_lan_address", return_value=True
        ):
            result = networkd.apply_networkd(config)

        self.assertTrue(result.ok, result.output)
        self.assertTrue(lan_path.exists())
        self.assertTrue(ppp_path.exists())
        self.assertFalse(wan_path.exists())
        self.assertIn('"adsl123"', chap.read_text(encoding="utf-8"))
        self.assertTrue((workdir / "90-linux-router").exists(), "PPPoE 模式应写入 ip-up 钩子")
        self.assertEqual(chap.stat().st_mode & 0o777, 0o600)
        flat = [" ".join(call) for call in commands]
        self.assertTrue(any("enable systemd-networkd.service" in item for item in flat))
        self.assertTrue(any("restart linux-router-pppoe.service" in item for item in flat))
        # The physical PPPoE link must be up, otherwise pppd reports ENETDOWN.
        nic_file = workdir / "15-pppoe-nic.network"
        self.assertTrue(nic_file.exists(), "PPPoE 模式应保证物理网口被 networkd 管理")
        nic_text = nic_file.read_text(encoding="utf-8")
        self.assertIn("Name=eth0", nic_text)
        self.assertIn("ConfigureWithoutCarrier=yes", nic_text)
        self.assertIn("ip link set eth0 up", flat)

    def test_apply_dhcp_removes_ppp_files(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "networkd-dhcp"
        workdir.mkdir(exist_ok=True)
        wan_path = workdir / "10-wan.network"
        lan_path = workdir / "20-lan.network"
        ppp_path = workdir / "30-ppp.network"
        ppp_path.write_text("[Match]\nName=ppp0\n", encoding="utf-8")

        with patch.object(networkd, "WAN_NETWORK_PATH", wan_path), patch.object(
            networkd, "LAN_NETWORK_PATH", lan_path
        ), patch.object(
            networkd, "DNS_SCRIPT_PATH", workdir / "dns.sh"
        ), patch.object(
            networkd, "DNS_SERVICE_PATH", workdir / "dns.service"
        ), patch.object(
            networkd, "DNS_TIMER_PATH", workdir / "dns.timer"
        ), patch.object(
            networkd, "PPP_NETWORK_PATH", ppp_path
        ), patch.object(
            networkd, "PPPOE_NIC_NETWORK_PATH", workdir / "15-pppoe-nic.network"
        ), patch.object(
            networkd, "command_exists", return_value=True
        ), patch.object(
            networkd, "is_service_active", return_value=False
        ), patch.object(
            networkd, "is_service_enabled", return_value=False
        ), patch.object(
            networkd, "run_command", return_value=ok()
        ), patch.object(
            networkd, "wait_for_lan_address", return_value=True
        ):
            result = networkd.apply_networkd(self.config())

        self.assertTrue(result.ok, result.output)
        self.assertTrue(wan_path.exists())
        self.assertFalse(ppp_path.exists())
        self.assertFalse((workdir / "15-pppoe-nic.network").exists())
        self.assertIn("DHCP=yes", wan_path.read_text(encoding="utf-8"))

    def test_configured_methods_are_read_from_files(self):
        workdir = Path(TEST_DATA_DIRECTORY.name) / "networkd-methods"
        workdir.mkdir(exist_ok=True)
        (workdir / "10-wan.network").write_text(
            "[Match]\nName=eth0\n[Network]\nDHCP=yes\n", encoding="utf-8"
        )
        (workdir / "20-lan.network").write_text(
            "[Match]\nName=eth1\n[Network]\nAddress=192.168.50.1/24\nIPv6SendRA=yes\n",
            encoding="utf-8",
        )
        with patch.object(networkd, "NETWORKD_DIR", workdir):
            methods = networkd.get_configured_methods()
        self.assertEqual(methods["eth0"], ("auto", "auto"))
        self.assertEqual(methods["eth1"], ("manual", "auto"))

    def test_delegated_prefix_from_kernel(self):
        payload = (
            '[{"ifname":"eth1","addr_info":['
            '{"family":"inet6","local":"2408:8207:6a1c:2f00::1","prefixlen":64},'
            '{"family":"inet6","local":"fe80::1","prefixlen":64}]}]'
        )
        with patch.object(networkd, "run_command", return_value=ok(payload)):
            self.assertEqual(
                networkd.get_delegated_prefix("eth1"), "2408:8207:6a1c:2f00::/64"
            )

    def test_router_page_renders_wan_modes(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        status = {
            "config": routing.load_router_config(),
            "interfaces": [{"name": "eth1", "link_speed": "1 Gbps"}],
            "wan_candidates": [{"name": "eth0", "state": "connected", "ipv4": ["192.168.9.7/24"]}],
            "backend": {"service_active": True},
            "wan": {}, "lan": {}, "ipv6": {}, "dhcp": {}, "firewall": {},
            "forwarding": {}, "services": {}, "pd": {}, "errors": [],
        }
        with patch.object(web_routing, "query_agent", return_value=status):
            body = client.get("/router").get_data(as_text=True)
        self.assertIn('name="wan_mode"', body)
        self.assertIn('name="pppoe_username"', body)
        self.assertIn("PPPoE 账号", body)


class ServiceEnabledTests(unittest.TestCase):
    def test_enabled_state_is_reported(self):
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["systemctl", "is-active"]:
                return ok("active")
            if command[:2] == ["systemctl", "is-enabled"]:
                return ok("enabled")
            return ok("")

        with patch.object(service_monitor, "run_command", side_effect=fake_run):
            state = service_monitor.get_service_state("nginx.service")

        self.assertEqual(state["status"], "active")
        self.assertEqual(state["level"], "ok")
        self.assertEqual(state["enabled"], "enabled")
        self.assertEqual(state["enabled_label"], "开机自启")
        self.assertEqual(state["enabled_level"], "ok")

    def test_disabled_unit_warns(self):
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["systemctl", "is-active"]:
                return ok("active")
            return ok("disabled")

        with patch.object(service_monitor, "run_command", side_effect=fake_run):
            state = service_monitor.get_service_state("nginx")

        self.assertEqual(state["enabled_label"], "未自启")
        self.assertEqual(state["enabled_level"], "warning")

    def test_static_unit_is_not_a_problem(self):
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["systemctl", "is-active"]:
                return ok("active")
            return ok("static")

        with patch.object(service_monitor, "run_command", side_effect=fake_run):
            state = service_monitor.get_service_state("systemd-networkd.service")

        self.assertEqual(state["enabled_level"], "ok")
        self.assertIn("静态", state["enabled_label"])

    def test_masked_unit_is_an_error(self):
        def fake_run(command, timeout=8, env=None):
            if command[:2] == ["systemctl", "is-active"]:
                return ok("inactive")
            return ok("masked")

        with patch.object(service_monitor, "run_command", side_effect=fake_run):
            state = service_monitor.get_service_state("bad.service")

        self.assertEqual(state["enabled_level"], "error")
        self.assertEqual(state["level"], "error")

    def test_timer_and_socket_units_are_accepted(self):
        """Timers are exactly where is-enabled matters; they used to show 未知."""
        self.assertEqual(
            service_monitor.normalize_service_name("linux-router-cake.timer"),
            ("linux-router-cake.timer", None),
        )
        self.assertEqual(
            service_monitor.normalize_service_name("sshd.socket"), ("sshd.socket", None)
        )
        # A bare name still means .service, and the panel's own unit stays off limits.
        self.assertEqual(service_monitor.normalize_service_name("nginx")[0], "nginx.service")
        self.assertIn("面板自身", service_monitor.normalize_service_name("router-panel")[1])
        self.assertIn("无效", service_monitor.normalize_service_name("bad name!")[1])

    def test_tools_page_renders_enabled_column(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        status = {
            "config": {"services": ["nginx.service"]},
            "status": {
                "services": [
                    {
                        "name": "nginx.service",
                        "status": "active",
                        "level": "ok",
                        "enabled": "enabled",
                        "enabled_label": "开机自启",
                        "enabled_level": "ok",
                    }
                ],
                "errors": [],
            },
        }
        with patch.object(web_tools, "query_agent", return_value=status["status"]):
            body = client.get("/tools").get_data(as_text=True)
        self.assertIn("开机自启", body)
        self.assertIn("nginx.service", body)


class BackupRestoreTests(unittest.TestCase):
    def payload(self, **overrides):
        backup = {
            "format": backup_client.BACKUP_FORMAT,
            "version": backup_client.BACKUP_VERSION,
            "created_at": "2026-10-29T00:00:00+00:00",
            "hostname": "router",
            "build": {"branch": "main", "build": "local"},
            "packages": ["dnsmasq"],
            "files": {
                str(self.router_json): {"content": '{"wan_interface": "eth0"}', "mode": "0600"}
            },
        }
        backup.update(overrides)
        return backup

    def setUp(self):
        self.workdir = Path(TEST_DATA_DIRECTORY.name) / "backup"
        if not self.workdir.exists():
            self.workdir.mkdir(parents=True)
        self.router_json = self.workdir / "router.json"

    def test_backup_paths_are_limited_to_our_files(self):
        paths = {str(path) for path in backup_client.backup_paths()}
        self.assertIn(str(core.ROUTER_CONFIG_PATH), paths)
        self.assertIn(str(backup_client.CAKE_SCRIPT_PATH), paths)
        # The bootstrap files matter too: without them a restore leaves the
        # distro defaults (with `flush ruleset`) in place.
        self.assertIn("/etc/nftables.conf", paths)
        self.assertIn("/etc/dnsmasq.conf", paths)
        self.assertIn(str(firewall.NFTABLES_DROPIN_PATH), paths)
        for path in paths:
            self.assertTrue(path.startswith(("/etc/", str(core.DATA_DIR))), path)

    def test_restore_never_brings_back_a_flushing_nftables_conf(self):
        with tempfile.TemporaryDirectory() as directory:
            conf = Path(directory) / "nftables.conf"
            dropin = Path(directory) / "10-linux-router.conf"
            conf.write_text(
                '#!/usr/sbin/nft -f\nflush ruleset\ninclude "/etc/nftables.d/*.nft"\n',
                encoding="utf-8",
            )
            with patch.object(backup_client, "NFTABLES_CONFIG_PATH", conf), patch.object(
                firewall, "NFTABLES_CONFIG_PATH", conf
            ), patch.object(firewall, "NFTABLES_CONF_DIR", Path(directory) / "nftables.d"), patch.object(
                firewall, "NFTABLES_DROPIN_PATH", dropin
            ), patch.object(firewall, "NFTABLES_DROPIN_DIR", dropin.parent), patch.object(
                backup_client, "run_command", return_value=ok()
            ), patch.object(firewall, "run_command", return_value=ok()):
                notes = backup_client._reload_services({str(conf)})

            self.assertNotIn("flush ruleset", conf.read_text(encoding="utf-8"))
            self.assertIn("nftables 规则已重载", notes)

    def test_create_backup_collects_existing_files(self):
        self.router_json.write_text('{"wan_interface": "eth0"}\n', encoding="utf-8")
        self.router_json.chmod(0o600)
        with patch.object(backup_client, "backup_paths", return_value=[self.router_json]), patch.object(
            backup_client, "installed_package_names", return_value=["dnsmasq", "nftables"]
        ):
            payload = backup_client.create_backup()

        self.assertEqual(payload["format"], backup_client.BACKUP_FORMAT)
        self.assertEqual(payload["packages"], ["dnsmasq", "nftables"])
        entry = payload["files"][str(self.router_json)]
        self.assertIn("eth0", entry["content"])
        self.assertEqual(entry["mode"], "0600")

    def test_validate_rejects_foreign_paths(self):
        payload = self.payload(files={"/etc/passwd": {"content": "x", "mode": "0644"}})
        _, error = backup_client.validate_backup(payload)
        self.assertIn("未知路径", error)

    def test_validate_rejects_wrong_format(self):
        _, error = backup_client.validate_backup({"format": "something-else"})
        self.assertIn("不是 Linux Router", error)
        _, error = backup_client.validate_backup("nope")
        self.assertIn("格式无效", error)

    def test_validate_rejects_future_version(self):
        _, error = backup_client.validate_backup(self.payload(version=99))
        self.assertIn("版本", error)

    def test_restore_writes_files_and_reloads(self):
        self.router_json.write_text("old\n", encoding="utf-8")
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            return ok()

        with patch.object(backup_client, "backup_paths", return_value=[self.router_json]), patch.object(
            backup_client, "package_installed", return_value=True
        ), patch.object(backup_client, "run_command", side_effect=fake_run):
            result = backup_client.restore_backup(self.payload())

        self.assertTrue(result.ok, result.output)
        self.assertIn("eth0", self.router_json.read_text(encoding="utf-8"))
        self.assertEqual(self.router_json.stat().st_mode & 0o777, 0o600)
        flat = [" ".join(call) for call in commands]
        self.assertTrue(any("daemon-reload" in item for item in flat))

    def test_restore_installs_missing_packages(self):
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            return ok()

        with patch.object(backup_client, "backup_paths", return_value=[self.router_json]), patch.object(
            backup_client, "package_installed", return_value=False
        ), patch.object(backup_client, "run_command", side_effect=fake_run):
            result = backup_client.restore_backup(self.payload(packages=["dnsmasq", "ppp"]))

        self.assertTrue(result.ok, result.output)
        install = [call for call in commands if call[:2] == ["apt-get", "install"]]
        self.assertEqual(len(install), 1)
        self.assertIn("dnsmasq", install[0])
        self.assertIn("ppp", install[0])

    def test_restore_without_install_skips_apt(self):
        commands = []

        def fake_run(command, timeout=8, env=None):
            commands.append(command)
            return ok()

        with patch.object(backup_client, "backup_paths", return_value=[self.router_json]), patch.object(
            backup_client, "run_command", side_effect=fake_run
        ):
            result = backup_client.restore_backup(self.payload(), install_packages=False)

        self.assertTrue(result.ok, result.output)
        self.assertFalse([call for call in commands if call[:1] == ["apt-get"]])

    def test_download_route_returns_json_attachment(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        with patch.object(web_general, "query_agent", return_value=self.payload()):
            response = client.get("/settings/backup")
        self.assertEqual(response.status_code, 200)
        self.assertIn("attachment", response.headers["Content-Disposition"])
        self.assertIn("linux-router-backup", response.headers["Content-Disposition"])
        self.assertEqual(response.mimetype, "application/json")

    def test_restore_route_requires_a_file(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        response = client.post("/settings/backup/restore", data={"csrf_token": "test-token"})
        self.assertEqual(response.status_code, 302)

    def test_restore_route_rejects_bad_json(self):
        application.app.config["TESTING"] = True
        client = application.app.test_client()
        with client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"
        import io

        response = client.post(
            "/settings/backup/restore",
            data={
                "csrf_token": "test-token",
                "backup": (io.BytesIO(b"not json"), "backup.json"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 302)

    def test_restore_operation_rejects_paths_outside_data_dir(self):
        with self.assertRaises(agent_server.ValidationError):
            agent_server._execute_backup_restore({"path": "/etc/passwd"})

    def test_restore_hands_data_files_back_to_the_panel_user(self):
        """The agent runs as root; secret_key/auth.json must stay panel-readable."""
        payload = {
            "format": backup_client.BACKUP_FORMAT,
            "version": backup_client.BACKUP_VERSION,
            "packages": [],
            "files": {
                str(core.ROUTER_CONFIG_PATH): {"content": "{}\n", "mode": "0600"},
                "/etc/dnsmasq.d/linux-router.conf": {"content": "x\n", "mode": "0644"},
            },
        }
        touched: list[str] = []

        with patch.object(backup_client, "atomic_write_text"), patch.object(
            backup_client, "run_command", return_value=ok()
        ), patch.object(
            backup_client, "chown_service_user", side_effect=lambda path: touched.append(str(path))
        ):
            result = backup_client.restore_backup(payload)

        self.assertTrue(result.ok, result.output)
        self.assertEqual(touched, [str(core.ROUTER_CONFIG_PATH)])

    def test_restore_operation_surfaces_the_summary(self):
        """The UI shows this message, so it must list what was restored."""
        upload = Path(core.DATA_DIR) / ".restore-test.json"
        upload.parent.mkdir(parents=True, exist_ok=True)
        upload.write_text(
            json.dumps(
                {
                    "format": backup_client.BACKUP_FORMAT,
                    "version": backup_client.BACKUP_VERSION,
                    "packages": [],
                    "files": {
                        str(core.ROUTER_CONFIG_PATH): {"content": "{}\n", "mode": "0600"}
                    },
                }
            ),
            encoding="utf-8",
        )
        with patch.object(backup_client, "atomic_write_text"), patch.object(
            backup_client, "run_command", return_value=ok()
        ):
            result = agent_server._execute_backup_restore({"path": str(upload)})

        self.assertTrue(result["ok"], result)
        self.assertIn("已恢复 1 个配置文件", result["message"])
        self.assertFalse(upload.exists(), "上传的临时文件必须被删除")


class UpstreamDnsTests(unittest.TestCase):
    def setUp(self):
        # Unique per test: the link files must not leak between cases.
        self.workdir = Path(TEST_DATA_DIRECTORY.name) / f"dns-{self._testMethodName}"
        if not self.workdir.exists():
            self.workdir.mkdir(parents=True)
        self.links = self.workdir / "links"
        if not self.links.exists():
            self.links.mkdir()
        self.sysfs = self.workdir / "net"
        if not self.sysfs.exists():
            self.sysfs.mkdir()
        self.resolv = self.workdir / "resolv.conf"

    def make_link(self, ifname: str, index: int, dns: str):
        iface = self.sysfs / ifname
        if not iface.exists():
            iface.mkdir()
        (iface / "ifindex").write_text(f"{index}\n", encoding="utf-8")
        (self.links / str(index)).write_text(
            f"OPER_STATE=routable\nDNS={dns}\n", encoding="utf-8"
        )

    def test_reads_dns_from_networkd_link_file(self):
        self.make_link("eth0", 2, "211.138.164.6 221.176.88.95")
        with patch.object(networkd, "NETIF_LINKS_DIR", self.links), patch.object(
            networkd, "SYS_CLASS_NET", self.sysfs
        ):
            self.assertEqual(
                networkd.get_link_dns("eth0"), ["211.138.164.6", "221.176.88.95"]
            )
            self.assertEqual(networkd.get_link_dns(""), [])
            self.assertEqual(networkd.get_link_dns("missing0"), [])

    def test_configured_upstream_wins(self):
        config = {"wan_interface": "eth0", "dns_upstream": "223.5.5.5"}
        self.make_link("eth0", 2, "1.1.1.1")
        with patch.object(networkd, "NETIF_LINKS_DIR", self.links), patch.object(
            networkd, "SYS_CLASS_NET", self.sysfs
        ):
            self.assertEqual(networkd.resolve_upstream_dns(config), ["223.5.5.5"])
            config["dns_upstream"] = ""
            self.assertEqual(networkd.resolve_upstream_dns(config), ["1.1.1.1"])

    def test_write_resolv_conf_uses_wan_dns(self):
        self.make_link("eth0", 2, "211.138.164.6")
        with patch.object(networkd, "NETIF_LINKS_DIR", self.links), patch.object(
            networkd, "SYS_CLASS_NET", self.sysfs
        ), patch.object(networkd, "RESOLV_CONF_PATH", self.resolv):
            result = networkd.write_resolv_conf({"wan_interface": "eth0"})

        self.assertTrue(result.ok, result.output)
        content = self.resolv.read_text(encoding="utf-8")
        self.assertIn("nameserver 211.138.164.6", content)
        self.assertIn("Generated by Linux Router", content)

    def test_write_resolv_conf_keeps_existing_when_nothing_known(self):
        self.resolv.write_text("nameserver 9.9.9.9\n", encoding="utf-8")
        with patch.object(networkd, "NETIF_LINKS_DIR", self.links), patch.object(
            networkd, "SYS_CLASS_NET", self.sysfs
        ), patch.object(networkd, "RESOLV_CONF_PATH", self.resolv):
            result = networkd.write_resolv_conf({"wan_interface": "eth0"})

        self.assertTrue(result.ok)
        self.assertIn("未获取到 WAN DNS", result.output)
        self.assertIn("9.9.9.9", self.resolv.read_text(encoding="utf-8"))

    def test_dns_sync_units(self):
        import subprocess

        script = networkd.render_dns_script()
        self.assertIn("write_resolv_conf", script)
        self.assertIn('PYTHONPATH="', script)
        path = Path(TEST_DATA_DIRECTORY.name) / "dns-sync.sh"
        path.write_text(script, encoding="utf-8")
        checked = subprocess.run(["sh", "-n", str(path)], capture_output=True, text=True)
        self.assertEqual(checked.returncode, 0, checked.stderr)

        service = networkd.render_dns_service()
        self.assertIn("Type=oneshot", service)
        self.assertIn("LINUX_ROUTER_DATA_DIR=", service)
        self.assertIn(str(networkd.DNS_SCRIPT_PATH), service)

        timer = networkd.render_dns_timer()
        self.assertIn("OnBootSec=20", timer)
        self.assertIn("OnUnitActiveSec=5min", timer)

    def test_get_dns_servers_skips_comments(self):
        self.resolv.write_text(
            "# comment\nnameserver 1.1.1.1\nnameserver 8.8.8.8\nnameserver 1.1.1.1\n",
            encoding="utf-8",
        )
        with patch.object(networkd, "RESOLV_CONF_PATH", self.resolv):
            self.assertEqual(networkd.get_dns_servers(), ["1.1.1.1", "8.8.8.8"])


class TailscaleConfigTests(unittest.TestCase):
    def setUp(self):
        application.app.config["TESTING"] = True
        self.client = application.app.test_client()
        with self.client.session_transaction() as session:
            session["logged_in"] = True
            session["username"] = "admin"
            session["csrf_token"] = "test-token"

    def tearDown(self):
        tailscale_client.TAILSCALE_CONFIG_PATH.unlink(missing_ok=True)

    def test_up_command_reflects_flags(self):
        self.assertEqual(
            tailscale_client.build_tailscale_up_command({"accept_routes": True}),
            ["tailscale", "up", "--accept-routes"],
        )
        # Unchecking accept routes must drop the flag, not keep the old one.
        self.assertEqual(
            tailscale_client.build_tailscale_up_command(
                {"accept_routes": False, "advertise_routes": "192.168.50.0/24"}
            ),
            ["tailscale", "up", "--advertise-routes=192.168.50.0/24"],
        )

    def test_apply_skips_when_tailscaled_is_stopped(self):
        with patch.object(tailscale_client, "command_exists", return_value=True), patch.object(
            tailscale_client, "is_service_active", return_value=False
        ), patch.object(tailscale_client, "run_command") as run:
            result = tailscale_client.apply_tailscale_config({"accept_routes": False})
        self.assertTrue(result["ok"])
        self.assertIn("未运行", result["message"])
        run.assert_not_called()

    def test_apply_runs_up_with_saved_flags(self):
        with patch.object(tailscale_client, "command_exists", return_value=True), patch.object(
            tailscale_client, "is_service_active", return_value=True
        ), patch.object(
            tailscale_client, "run_command", return_value=ok("")
        ) as run:
            result = tailscale_client.apply_tailscale_config(
                {"accept_routes": False, "advertise_routes": ""}
            )
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args[0][0], ["tailscale", "up"])

    def test_config_route_saves_when_logged_out(self):
        with patch.object(
            web_tools, "query_agent", return_value={"logged_in": False, "installed": True}
        ), patch.object(web_tools, "submit_operation") as submit:
            response = self.client.post(
                "/tools/tailscale/config",
                data={"csrf_token": "test-token", "advertise_routes": "10.0.0.0/24"},
                headers={"X-Requested-With": "fetch"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertFalse(payload["pending"])
        self.assertIn("登录后生效", payload["message"])
        submit.assert_not_called()
        saved = tailscale_client.load_tailscale_config()
        self.assertFalse(saved["accept_routes"])
        self.assertEqual(saved["advertise_routes"], "10.0.0.0/24")

    def test_config_route_queues_apply_when_logged_in(self):
        logged_in = {"logged_in": True, "installed": True, "agent_available": True}
        operation = {"id": "op-1", "status": "queued"}
        with patch.object(web_tools, "query_agent", return_value=logged_in), patch.object(
            web_tools, "submit_operation", return_value=operation
        ) as submit:
            response = self.client.post(
                "/tools/tailscale/config",
                data={"csrf_token": "test-token", "accept_routes": "1"},
                headers={"X-Requested-With": "fetch"},
            )
        self.assertEqual(response.status_code, 202)
        payload = response.get_json()
        self.assertTrue(payload["pending"])
        self.assertEqual(submit.call_args[0][0], "tailscale_apply")

    def test_config_route_reports_agent_outage(self):
        with patch.object(
            web_tools, "query_agent", side_effect=web_tools.AgentError("socket down")
        ):
            response = self.client.post(
                "/tools/tailscale/config",
                data={"csrf_token": "test-token", "accept_routes": "1"},
                headers={"X-Requested-With": "fetch"},
            )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["ok"])
        self.assertIn("系统控制服务不可用", payload["message"])
        self.assertTrue(tailscale_client.load_tailscale_config()["accept_routes"])

    def test_tools_page_offers_save_button_when_logged_in(self):
        status = {
            "agent_available": True,
            "installed": True,
            "logged_in": True,
            "service_active": True,
            "state_label": "已连接",
            "state_level": "ok",
            "tailscale_ips": ["100.64.0.1"],
            "self_name": "router.tailnet.ts.net",
            "user": "me@example.com",
            "errors": [],
        }
        with patch.object(web_tools, "query_agent", return_value=status):
            body = self.client.get("/tools").get_data(as_text=True)
        self.assertIn("保存设置", body)
        self.assertIn("accept_routes", body)
        self.assertIn("退出登录", body)


if __name__ == "__main__":
    unittest.main()
