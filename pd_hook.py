#!/usr/bin/python3
"""Entry point run by the dhcpcd hook to sync the delegated IPv6 prefix.

Called as: pd_hook.py <reason> <interface> <delegated-prefixes>

The script must never fail loudly: it runs inside dhcpcd's hook chain, and a
non-zero exit would show up as a dhcpcd error on every lease renewal.
"""
from __future__ import annotations

import sys

from router_panel.pd import sync_from_system


def main(argv: list[str]) -> int:
    reason = argv[1] if len(argv) > 1 else ""
    delegated = argv[3] if len(argv) > 3 else ""
    try:
        return sync_from_system(delegated_prefix=delegated, reason=reason)
    except Exception:  # noqa: BLE001 - hooks must not break dhcpcd
        return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
