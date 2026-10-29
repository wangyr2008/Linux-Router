#!/usr/bin/env bash
#
# Linux Router installer - Debian 13 (trixie) x86 router edition.
#
# Differences from upstream:
#   * wired-only: no Wi-Fi, hostapd, wpa_supplicant or iw dependency
#   * nftables (Debian 13 default) instead of iptables for NAT/forwarding
#   * IPv4 + IPv6 dual stack, dnsmasq provides DHCPv4 / SLAAC / DHCPv6
set -Eeuo pipefail

REPO_URL="${LINUX_ROUTER_REPO_URL:-https://github.com/Jaksay/Linux-Router.git}"
BRANCH="${LINUX_ROUTER_BRANCH:-main}"
ARCHIVE_URL="${LINUX_ROUTER_ARCHIVE_URL:-}"
SOURCE_DIR="${LINUX_ROUTER_SOURCE_DIR:-}"
INSTALL_DIR="${LINUX_ROUTER_INSTALL_DIR:-/opt/linux-router}"
DATA_DIR="${LINUX_ROUTER_DATA_DIR:-/var/lib/linux-router}"
INSTALL_STATE_DIR="$DATA_DIR/.linux-router-installer"
SERVICE_NAME="router-panel.service"
AGENT_SERVICE_NAME="router-panel-agent.service"
SERVICE_USER="router-panel"
NFTABLES_DIR="/etc/nftables.d"
NFTABLES_FILE="$NFTABLES_DIR/90-linux-router.nft"
NFTABLES_CONF="/etc/nftables.conf"
DNSMASQ_DIR="/etc/dnsmasq.d"
DNSMASQ_FILE="$DNSMASQ_DIR/linux-router.conf"
DNSMASQ_CONF="/etc/dnsmasq.conf"
SYSCTL_FILE="/etc/sysctl.d/90-router-panel.conf"
MIRROR="${LINUX_ROUTER_MIRROR:-}"
REMOTE_SOURCE_REQUESTED=0
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]:-$0}")" && pwd)"
ACTION=""
PURGE_DATA=0
NEW_PASSWORD=""
INITIAL_PASSWORD="${LINUX_ROUTER_INITIAL_PASSWORD:-}"
WORK_DIR=""
STAGING_DIR=""
BACKUP_DIR=""
ROLLBACK_DIR=""
INSTALL_STARTED=0
INSTALL_COMMITTED=0
INSTALL_DIR_EXISTED=0
INSTALL_SWAP_STARTED=0
DATA_DIR_EXISTED=0
SERVICE_USER_CREATED=0
SERVICE_GROUP_CREATED=0
DATA_TOUCHED=0
ORIGINAL_IP_FORWARD=""
ORIGINAL_IPV6_FORWARD=""
DHCPCD_WAS_ACTIVE=0
DHCPCD_WAS_ENABLED=0
RESOLVED_WAS_ACTIVE=0
RESOLVED_WAS_ENABLED=0
DNSMASQ_WAS_ACTIVE=0
DNSMASQ_WAS_ENABLED=0
NFTABLES_WAS_ACTIVE=0
NFTABLES_WAS_ENABLED=0
WEB_WAS_ACTIVE=0
WEB_WAS_ENABLED=0
AGENT_WAS_ACTIVE=0
AGENT_WAS_ENABLED=0

log() {
  printf '[linux-router] %s\n' "$*"
}

die() {
  printf '[linux-router] ERROR: %s\n' "$*" >&2
  exit 1
}

service_is_active() {
  systemctl is-active --quiet "$1" 2>/dev/null
}

service_is_enabled() {
  systemctl is-enabled --quiet "$1" 2>/dev/null
}

backup_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$path" ]]; then
    cp -a "$path" "$ROLLBACK_DIR/$name"
  else
    : > "$ROLLBACK_DIR/$name.missing"
  fi
}

restore_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$ROLLBACK_DIR/$name" ]]; then
    install -d -m 0755 "$(dirname "$path")"
    cp -a "$ROLLBACK_DIR/$name" "$path"
  elif [[ -e "$ROLLBACK_DIR/$name.missing" ]]; then
    rm -f "$path"
  fi
}

persist_install_state() {
  local name
  local state_tmp="${INSTALL_STATE_DIR}.new.$$"
  local state_previous="${INSTALL_STATE_DIR}.previous"
  rm -rf "$state_tmp" "$state_previous"
  install -d -o root -g root -m 0700 "$state_tmp"
  printf 'version=1\ndhcpcd_was_active=%s\ndhcpcd_was_enabled=%s\nresolved_was_active=%s\nresolved_was_enabled=%s\nip_forward=%s\nipv6_forward=%s\n' \
    "$DHCPCD_WAS_ACTIVE" "$DHCPCD_WAS_ENABLED" \
    "$RESOLVED_WAS_ACTIVE" "$RESOLVED_WAS_ENABLED" \
    "$ORIGINAL_IP_FORWARD" "$ORIGINAL_IPV6_FORWARD" \
    > "$state_tmp/state.base"
  for name in sysctl.conf nftables.conf dnsmasq.conf; do
    if [[ -e "$ROLLBACK_DIR/$name" ]]; then
      cp -a "$ROLLBACK_DIR/$name" "$state_tmp/$name"
    else
      : > "$state_tmp/$name.missing"
    fi
  done
  {
    cat "$state_tmp/state.base"
    printf 'complete=1\n'
  } > "$state_tmp/state.new"
  chmod 0600 "$state_tmp/state.new"
  mv "$state_tmp/state.new" "$state_tmp/state"
  rm -f "$state_tmp/state.base"
  if [[ -e "$INSTALL_STATE_DIR" ]]; then
    mv "$INSTALL_STATE_DIR" "$state_previous"
  fi
  mv "$state_tmp" "$INSTALL_STATE_DIR"
  rm -rf "$state_previous"
}

state_file_is_valid() {
  local directory="$1"
  [[ -f "$directory/state" ]] || return 1
  grep -q '^version=1$' "$directory/state" || return 1
  if grep -q '^complete=' "$directory/state"; then
    grep -q '^complete=1$' "$directory/state"
  fi
}

state_value() {
  local key="$1"
  [[ -f "$INSTALL_STATE_DIR/state" ]] || return 1
  awk -F= -v key="$key" '$1 == key { print substr($0, index($0, "=") + 1); exit }' \
    "$INSTALL_STATE_DIR/state"
}

restore_persisted_file() {
  local path="$1"
  local name="$2"
  if [[ -e "$INSTALL_STATE_DIR/$name" ]]; then
    install -d -m 0755 "$(dirname "$path")"
    cp -a "$INSTALL_STATE_DIR/$name" "$path"
    return 0
  fi
  if [[ -e "$INSTALL_STATE_DIR/$name.missing" ]]; then
    rm -f "$path"
    return 0
  fi
  return 1
}

restore_service_state() {
  local service_name="$1"
  local was_enabled="$2"
  local was_active="$3"
  if [[ "$was_enabled" -eq 1 ]]; then
    systemctl enable "$service_name" >/dev/null 2>&1 || true
  else
    systemctl disable "$service_name" >/dev/null 2>&1 || true
  fi
  if [[ "$was_active" -eq 1 ]]; then
    systemctl restart "$service_name" >/dev/null 2>&1 || true
  else
    systemctl stop "$service_name" >/dev/null 2>&1 || true
  fi
}

remove_router_rule_files() {
  rm -f "$NFTABLES_FILE" "$DNSMASQ_FILE"
}

rollback_install() {
  log "Installation failed; restoring the previous system state"
  systemctl stop "$SERVICE_NAME" >/dev/null 2>&1 || true
  systemctl stop "$AGENT_SERVICE_NAME" >/dev/null 2>&1 || true

  if [[ "$INSTALL_SWAP_STARTED" -eq 1 ]]; then
    if [[ "$INSTALL_DIR_EXISTED" -eq 1 && -d "$BACKUP_DIR" ]]; then
      rm -rf "$INSTALL_DIR"
      mv "$BACKUP_DIR" "$INSTALL_DIR"
    elif [[ "$INSTALL_DIR_EXISTED" -eq 0 ]]; then
      rm -rf "$INSTALL_DIR"
    fi
  fi

  if [[ "$DATA_TOUCHED" -eq 1 ]]; then
    if [[ "$DATA_DIR_EXISTED" -eq 1 && -d "$ROLLBACK_DIR/data-dir" ]]; then
      rm -rf "$DATA_DIR"
      cp -a "$ROLLBACK_DIR/data-dir" "$DATA_DIR"
    else
      rm -rf "$DATA_DIR"
    fi
  fi

  restore_file "/etc/systemd/system/$SERVICE_NAME" "web.service"
  restore_file "/etc/systemd/system/$AGENT_SERVICE_NAME" "agent.service"
  if [[ "$ACTION" == "install" ]]; then
    restore_file "$SYSCTL_FILE" "sysctl.conf"
    restore_file "$NFTABLES_CONF" "nftables.conf"
    restore_file "$DNSMASQ_CONF" "dnsmasq.conf"
    remove_router_rule_files
  fi

  systemctl daemon-reload >/dev/null 2>&1 || true
  if [[ "$ACTION" == "install" ]]; then
    if [[ -n "$ORIGINAL_IP_FORWARD" ]]; then
      sysctl -w "net.ipv4.ip_forward=$ORIGINAL_IP_FORWARD" >/dev/null 2>&1 || true
    fi
    if [[ -n "$ORIGINAL_IPV6_FORWARD" ]]; then
      sysctl -w "net.ipv6.conf.all.forwarding=$ORIGINAL_IPV6_FORWARD" >/dev/null 2>&1 || true
    fi
    restore_service_state "dhcpcd.service" "$DHCPCD_WAS_ENABLED" "$DHCPCD_WAS_ACTIVE"
    restore_service_state "systemd-resolved.service" "$RESOLVED_WAS_ENABLED" "$RESOLVED_WAS_ACTIVE"
    restore_service_state "dnsmasq.service" "$DNSMASQ_WAS_ENABLED" "$DNSMASQ_WAS_ACTIVE"
    restore_service_state "nftables.service" "$NFTABLES_WAS_ENABLED" "$NFTABLES_WAS_ACTIVE"
  fi
  restore_service_state "$AGENT_SERVICE_NAME" "$AGENT_WAS_ENABLED" "$AGENT_WAS_ACTIVE"
  restore_service_state "$SERVICE_NAME" "$WEB_WAS_ENABLED" "$WEB_WAS_ACTIVE"

  if [[ "$SERVICE_USER_CREATED" -eq 1 ]]; then
    userdel "$SERVICE_USER" >/dev/null 2>&1 || true
  fi
  if [[ "$SERVICE_GROUP_CREATED" -eq 1 ]]; then
    groupdel "$SERVICE_USER" >/dev/null 2>&1 || true
  fi
}

install_service_units() {
  sed \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/router-panel-agent.service" > "$WORK_DIR/$AGENT_SERVICE_NAME"
  sed \
    -e "s|@INSTALL_DIR@|$INSTALL_DIR|g" \
    -e "s|@DATA_DIR@|$DATA_DIR|g" \
    "$INSTALL_DIR/router-panel.service" > "$WORK_DIR/$SERVICE_NAME"
  systemd-analyze verify "$WORK_DIR/$AGENT_SERVICE_NAME" "$WORK_DIR/$SERVICE_NAME"
  install -o root -g root -m 0644 "$WORK_DIR/$AGENT_SERVICE_NAME" "/etc/systemd/system/$AGENT_SERVICE_NAME"
  install -o root -g root -m 0644 "$WORK_DIR/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"

  systemctl daemon-reload
  systemctl enable "$AGENT_SERVICE_NAME" >/dev/null
  systemctl enable "$SERVICE_NAME" >/dev/null

  # The agent runs as root, so files it wrote (restore, config saves) may be
  # root-owned; the panel reads secret_key at boot and would fail to start.
  if getent passwd "$SERVICE_USER" >/dev/null && [[ -d "$DATA_DIR" ]]; then
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
  fi

  systemctl restart "$AGENT_SERVICE_NAME"
  systemctl restart "$SERVICE_NAME"

  log "Checking service health"
  if ! curl --fail --silent --retry 10 --retry-delay 1 --retry-connrefused \
    http://127.0.0.1/healthz >/dev/null 2>&1; then
    systemctl --no-pager --full status "$SERVICE_NAME" || true
    systemctl --no-pager --full status "$AGENT_SERVICE_NAME" || true
    die "service health check failed"
  fi
}

write_nftables_bootstrap() {
  install -d -m 0755 "$NFTABLES_DIR"
  if [[ ! -e "$NFTABLES_CONF" ]]; then
    cat > "$NFTABLES_CONF" <<'EOF'
#!/usr/sbin/nft -f

flush ruleset

include "/etc/nftables.d/*.nft"
EOF
    chmod 0755 "$NFTABLES_CONF"
  elif ! grep -q 'nftables.d' "$NFTABLES_CONF"; then
    python3 - "$NFTABLES_CONF" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
lines = path.read_text(encoding="utf-8").splitlines()
insert_at = len(lines)
for index, line in enumerate(lines):
    if line.split("#", 1)[0].strip() == "flush ruleset":
        insert_at = index + 1
        break
lines.insert(insert_at, 'include "/etc/nftables.d/*.nft"')
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  fi

  cat > "$NFTABLES_FILE" <<'EOF'
#!/usr/sbin/nft -f
# Placeholder generated by the Linux Router installer.
# Open the panel -> 路由 and apply the router configuration to replace this file.
table inet lr_forward {
    chain forward {
        type filter hook forward priority filter; policy accept;
        ct state established,related accept
    }
}
EOF
  chmod 0644 "$NFTABLES_FILE"
}

# Bootstrap dnsmasq so it listens on loopback only until the LAN is configured.
write_dnsmasq_bootstrap() {
  install -d -m 0755 "$DNSMASQ_DIR"
  if [[ ! -e "$DNSMASQ_CONF" ]]; then
    cat > "$DNSMASQ_CONF" <<'EOF'
# Generated by the Linux Router installer.
conf-dir=/etc/dnsmasq.d/,*.conf
EOF
    chmod 0644 "$DNSMASQ_CONF"
  elif ! grep -q 'dnsmasq.d' "$DNSMASQ_CONF"; then
    printf '\n# Linux Router\nconf-dir=/etc/dnsmasq.d/,*.conf\n' >> "$DNSMASQ_CONF"
  fi

  cat > "$DNSMASQ_FILE" <<'EOF'
# Placeholder generated by the Linux Router installer.
# Apply the router configuration in the panel to write the real LAN settings.
bind-dynamic
interface=lo
EOF
  chmod 0644 "$DNSMASQ_FILE"
}

uninstall_router_panel() {
  local has_install_state=0
  local original_dhcpcd_active=0
  local original_dhcpcd_enabled=0
  local original_resolved_active=0
  local original_resolved_enabled=0
  local original_ip_forward=""
  local original_ipv6_forward=""
  log "Uninstalling Linux Router"

  if ! state_file_is_valid "$INSTALL_STATE_DIR" && state_file_is_valid "${INSTALL_STATE_DIR}.previous"; then
    INSTALL_STATE_DIR="${INSTALL_STATE_DIR}.previous"
  fi
  if state_file_is_valid "$INSTALL_STATE_DIR"; then
    has_install_state=1
    original_dhcpcd_active="$(state_value dhcpcd_was_active || printf '0')"
    original_dhcpcd_enabled="$(state_value dhcpcd_was_enabled || printf '0')"
    original_resolved_active="$(state_value resolved_was_active || printf '0')"
    original_resolved_enabled="$(state_value resolved_was_enabled || printf '0')"
    original_ip_forward="$(state_value ip_forward || true)"
    original_ipv6_forward="$(state_value ipv6_forward || true)"
    [[ "$original_dhcpcd_active" =~ ^[01]$ ]] || original_dhcpcd_active=0
    [[ "$original_dhcpcd_enabled" =~ ^[01]$ ]] || original_dhcpcd_enabled=0
    [[ "$original_resolved_active" =~ ^[01]$ ]] || original_resolved_active=0
    [[ "$original_resolved_enabled" =~ ^[01]$ ]] || original_resolved_enabled=0
  fi

  # Panel services first, so nothing keeps rewriting files we are about to remove.
  for unit in \
    "$SERVICE_NAME" \
    "$AGENT_SERVICE_NAME" \
    linux-router-cake.service \
    linux-router-cake.timer \
    linux-router-dns.service \
    linux-router-dns.timer \
    linux-router-pppoe.service \
    linux-router-pd.service; do
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    rm -f "/etc/systemd/system/$unit"
  done
  systemctl daemon-reload
  systemctl reset-failed "$SERVICE_NAME" "$AGENT_SERVICE_NAME" >/dev/null 2>&1 || true

  rm -rf /etc/linux-router /run/linux-router
  rm -f /etc/systemd/network/10-linux-router-wan.network \
        /etc/systemd/network/20-linux-router-lan.network \
        /etc/systemd/network/30-linux-router-ppp.network
  rm -f /etc/ppp/peers/linux-router /etc/ppp/ip-up.d/90-linux-router
  rm -f /etc/ppp/chap-secrets /etc/ppp/pap-secrets
  rm -f /usr/lib/dhcpcd/dhcpcd-hooks/99-linux-router

  rm -f "$SYSCTL_FILE"
  if [[ "$has_install_state" -eq 1 ]]; then
    if ! restore_persisted_file "$SYSCTL_FILE" "sysctl.conf"; then
      log "WARNING: original sysctl configuration backup is missing"
    fi
    if ! restore_persisted_file "$NFTABLES_CONF" "nftables.conf"; then
      log "WARNING: original nftables.conf backup is missing"
    fi
    if ! restore_persisted_file "$DNSMASQ_CONF" "dnsmasq.conf"; then
      log "WARNING: original dnsmasq.conf backup is missing"
    fi
  fi

  # Applying the router config commented these out; give them back.
  if [[ -f /etc/network/interfaces.linux-router.bak ]]; then
    mv -f /etc/network/interfaces.linux-router.bak /etc/network/interfaces
    log "Restored /etc/network/interfaces"
  fi

  remove_router_rule_files
  if command -v nft >/dev/null 2>&1; then
    for table in "inet lr_forward" "ip lr_nat" "ip6 lr_nat6" "inet lr_input"; do
      nft delete table $table >/dev/null 2>&1 || true
    done
  fi

  if [[ "$has_install_state" -eq 1 ]]; then
    if [[ -n "$original_ip_forward" ]]; then
      sysctl -w "net.ipv4.ip_forward=$original_ip_forward" >/dev/null 2>&1 || \
        log "WARNING: could not restore net.ipv4.ip_forward"
    fi
    if [[ -n "$original_ipv6_forward" ]]; then
      sysctl -w "net.ipv6.conf.all.forwarding=$original_ipv6_forward" >/dev/null 2>&1 || \
        log "WARNING: could not restore net.ipv6.conf.all.forwarding"
    fi
    restore_service_state dhcpcd.service "$original_dhcpcd_enabled" "$original_dhcpcd_active"
    restore_service_state systemd-resolved.service "$original_resolved_enabled" "$original_resolved_active"
    systemctl disable --now dnsmasq.service >/dev/null 2>&1 || true
    systemctl restart nftables.service >/dev/null 2>&1 || true
    rm -rf "$DATA_DIR/.linux-router-installer" "$DATA_DIR/.linux-router-installer.previous"
  fi

  rm -rf "$INSTALL_DIR"

  if [[ "$PURGE_DATA" -eq 1 ]]; then
    rm -rf "$DATA_DIR"
    log "Removed application data from $DATA_DIR"
  elif [[ -e "$DATA_DIR" ]]; then
    chown -R root:root "$DATA_DIR"
    chmod 0700 "$DATA_DIR"
    find "$DATA_DIR" -type f -exec chmod 0600 {} +
    log "Preserved application data in $DATA_DIR"
  fi

  if getent passwd "$SERVICE_USER" >/dev/null; then
    userdel "$SERVICE_USER" || log "WARNING: could not remove user $SERVICE_USER"
  fi
  if getent group "$SERVICE_USER" >/dev/null; then
    groupdel "$SERVICE_USER" || log "WARNING: could not remove group $SERVICE_USER"
  fi

  log "Linux Router has been uninstalled"
  log "Installed apt packages were left unchanged"
}

cleanup() {
  local status=$?
  trap - EXIT
  set +e

  if [[ "$status" -ne 0 && "$INSTALL_COMMITTED" -eq 0 && "$INSTALL_STARTED" -eq 1 ]]; then
    rollback_install
  fi

  [[ -z "$STAGING_DIR" || ! -e "$STAGING_DIR" ]] || rm -rf "$STAGING_DIR"
  [[ -z "$WORK_DIR" || ! -e "$WORK_DIR" ]] || rm -rf "$WORK_DIR"
  if [[ "$status" -eq 0 && -n "$BACKUP_DIR" && -e "$BACKUP_DIR" ]]; then
    rm -rf "$BACKUP_DIR"
  fi
  exit "$status"
}

trap cleanup EXIT

usage() {
  cat <<'EOF'
Install, upgrade, or uninstall Linux Router (Debian 13, wired, nftables, IPv4+IPv6).

Usage: sudo bash install.sh <command> [options]

Commands:
  install                    Perform a new installation
  upgrade                    Upgrade an existing installation
  uninstall                  Remove the installation and preserve user data

Options:
  --purge-data               With uninstall, also remove credentials and LAN settings
  --repo URL                 GitHub repository URL
  --branch NAME              GitHub branch (default: main)
  --archive-url URL          Download a specific source archive instead
  --source-dir PATH          Install from a local source directory (no download)
  --mirror PREFIX            Download through a GitHub mirror, e.g.
                             https://ghfast.top/ (prefix + full GitHub URL)
  --install-dir PATH         Application directory (default: /opt/linux-router)
  --data-dir PATH            Persistent data directory (default: /var/lib/linux-router)
  -h, --help                 Show this help

Environment variables with the LINUX_ROUTER_ prefix can also set repo, branch,
mirror, install directory, and data directory. The installer never reboots the
system and never touches your NIC configuration: the panel takes the interfaces
over when you apply the router settings.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    install|upgrade|uninstall)
      [[ -z "$ACTION" ]] || die "only one command may be specified"
      ACTION="$1"
      shift
      ;;
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a value"
      REPO_URL="$2"
      REMOTE_SOURCE_REQUESTED=1
      shift 2
      ;;
    --purge-data)
      PURGE_DATA=1
      shift
      ;;
    --branch)
      [[ $# -ge 2 ]] || die "--branch requires a value"
      BRANCH="$2"
      REMOTE_SOURCE_REQUESTED=1
      shift 2
      ;;
    --archive-url)
      [[ $# -ge 2 ]] || die "--archive-url requires a value"
      ARCHIVE_URL="$2"
      REMOTE_SOURCE_REQUESTED=1
      shift 2
      ;;
    --source-dir)
      [[ $# -ge 2 ]] || die "--source-dir requires a value"
      SOURCE_DIR="${2%/}"
      shift 2
      ;;
    --install-dir)
      [[ $# -ge 2 ]] || die "--install-dir requires a value"
      INSTALL_DIR="${2%/}"
      shift 2
      ;;
    --data-dir)
      [[ $# -ge 2 ]] || die "--data-dir requires a value"
      DATA_DIR="${2%/}"
      shift 2
      ;;
    --mirror)
      [[ $# -ge 2 ]] || die "--mirror requires a value"
      MIRROR="${2%/}"
      REMOTE_SOURCE_REQUESTED=1
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

[[ -n "$ACTION" ]] || die "specify one command: install, upgrade, or uninstall"
[[ "$PURGE_DATA" -eq 0 || "$ACTION" == "uninstall" ]] || die "--purge-data requires the uninstall command"
[[ -z "$MIRROR" || "$MIRROR" =~ ^https?://[^[:space:]]+$ ]] || die "invalid mirror URL: $MIRROR"
[[ "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid GitHub branch: $BRANCH"
[[ "$REPO_URL" =~ ^https://github\.com/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+(\.git)?$ ]] || die "invalid GitHub repository URL"

for path in "$INSTALL_DIR" "$DATA_DIR"; do
  [[ "$path" == /* ]] || die "paths must be absolute: $path"
  [[ "$path" =~ ^/[A-Za-z0-9._/-]+$ ]] || die "paths contain unsupported characters: $path"
  [[ ! "$path" =~ /\.\.?(/|$) ]] || die "paths must not contain . or .. segments: $path"
done

[[ "$INSTALL_DIR" != "$DATA_DIR" ]] || die "install and data directories must differ"
[[ "$DATA_DIR/" != "$INSTALL_DIR/"* ]] || die "data directory must not be inside install directory"
[[ "$INSTALL_DIR/" != "$DATA_DIR/"* ]] || die "install directory must not be inside data directory"
for path in "$INSTALL_DIR" "$DATA_DIR"; do
  case "$path" in
    /|/bin|/boot|/dev|/etc|/home|/lib|/opt|/proc|/root|/run|/sbin|/sys|/tmp|/usr|/var)
      die "refusing to use a system directory directly: $path"
      ;;
  esac
done
[[ ! -L "$INSTALL_DIR" ]] || die "install directory must not be a symbolic link"
[[ ! -L "$DATA_DIR" ]] || die "data directory must not be a symbolic link"
[[ ! -e "$INSTALL_DIR" || -d "$INSTALL_DIR" ]] || die "install path exists but is not a directory: $INSTALL_DIR"
[[ ! -e "$DATA_DIR" || -d "$DATA_DIR" ]] || die "data path exists but is not a directory: $DATA_DIR"

if [[ "$ACTION" == "install" ]]; then
  [[ ! -e "$INSTALL_DIR" ]] || die "an installation already exists at $INSTALL_DIR; use upgrade"
  [[ ! -e "/etc/systemd/system/$SERVICE_NAME" ]] || die "$SERVICE_NAME already exists; use upgrade"
  [[ ! -e "/etc/systemd/system/$AGENT_SERVICE_NAME" ]] || die "$AGENT_SERVICE_NAME already exists; use upgrade"
elif [[ "$ACTION" == "upgrade" ]]; then
  [[ -f "$INSTALL_DIR/app.py" ]] || die "no installation found at $INSTALL_DIR; use install"
  [[ -f "/etc/systemd/system/$SERVICE_NAME" ]] || die "$SERVICE_NAME is not installed"
  [[ -f "/etc/systemd/system/$AGENT_SERVICE_NAME" ]] || die "$AGENT_SERVICE_NAME is not installed"
  [[ -d "$DATA_DIR" ]] || die "application data directory not found: $DATA_DIR"
  getent passwd "$SERVICE_USER" >/dev/null || die "service user not found: $SERVICE_USER"
  getent group "$SERVICE_USER" >/dev/null || die "service group not found: $SERVICE_USER"
fi

[[ ${EUID} -eq 0 ]] || die "run this installer as root"
[[ -d /run/systemd/system ]] || die "systemd is required"
command -v apt-get >/dev/null 2>&1 || die "only apt-based Debian systems are supported"

if [[ "$ACTION" == "uninstall" ]]; then
  uninstall_router_panel
  INSTALL_COMMITTED=1
  exit 0
fi

if [[ -z "$SOURCE_DIR" && "$REMOTE_SOURCE_REQUESTED" -eq 0 ]] \
  && [[ -f "$SCRIPT_DIR/app.py" && -d "$SCRIPT_DIR/router_panel" && -d "$SCRIPT_DIR/templates" ]]; then
  SOURCE_DIR="$SCRIPT_DIR"
  log "Detected a local source tree, installing from $SOURCE_DIR"
  log "(pass --repo/--branch/--archive-url to install from GitHub instead)"
fi

if [[ -n "$SOURCE_DIR" ]]; then
  [[ -d "$SOURCE_DIR" ]] || die "source directory not found: $SOURCE_DIR"
  [[ -f "$SOURCE_DIR/app.py" ]] || die "source directory does not contain app.py: $SOURCE_DIR"
  BUILD_ID="local"
  log "Using local source directory $SOURCE_DIR"
else
  DEFAULT_ARCHIVE_URL="${REPO_URL%.git}/archive/refs/heads/$BRANCH.tar.gz"
  if [[ -z "$ARCHIVE_URL" ]]; then
    ARCHIVE_URL="$DEFAULT_ARCHIVE_URL"
  fi
  [[ "$ARCHIVE_URL" =~ ^https:// ]] || die "archive URL must use HTTPS"
fi

if [[ "$ACTION" == "install" ]]; then
  export DEBIAN_FRONTEND=noninteractive
  log "Installing system packages"
  # A stale cdrom:// entry (common after a DVD install) makes apt-get update
  # return non-zero even though the network mirrors work, so do not abort here.
  if ! apt-get update; then
    log "WARNING: apt-get update reported failures, continuing anyway"
  fi
  # Deliberately no iptables, iw, wpasupplicant or hostapd: this build is wired-only
  # and uses the nftables stack that ships with Debian 13.
  install_packages=(
    ca-certificates
    curl
    dnsmasq
    gunicorn
    iproute2
    nftables
    ppp
    python3
    python3-flask
    tar
    udev
  )
  # Optional: only needed for the "运营商 PD 自动" IPv6 mode.
  apt-get install -y --no-install-recommends "${install_packages[@]}"
else
  for command_name in curl tar python3 gunicorn systemd-analyze nft dnsmasq; do
    command -v "$command_name" >/dev/null 2>&1 || die "required command is missing: $command_name"
  done
fi

if [[ -n "$SOURCE_DIR" ]]; then
  log "Copying application files from $SOURCE_DIR"
  WORK_DIR="$(mktemp -d /tmp/linux-router-install.XXXXXX)"
  install -d -m 0755 "$WORK_DIR/source"
  cp -a "$SOURCE_DIR/." "$WORK_DIR/source/"
  rm -rf "$WORK_DIR/source/.git"
else
  if [[ -n "$MIRROR" ]]; then
    # Mirrors only proxy file downloads, so skip the GitHub API and tag the build
    # with the branch name instead of a commit id.
    BUILD_ID="${BRANCH}-mirror"
    log "Using mirror $MIRROR"
  else
    log "Resolving $BRANCH branch build"
    REPO_PATH="${REPO_URL#https://github.com/}"
    REPO_PATH="${REPO_PATH%.git}"
    REF_JSON="$(curl --fail --location --silent --show-error --retry 3 \
      "https://api.github.com/repos/$REPO_PATH/git/ref/heads/$BRANCH")" || \
      die "could not resolve branch: $BRANCH"
    REMOTE_COMMIT="$(REF_JSON="$REF_JSON" python3 -c 'import json, os; print(json.loads(os.environ["REF_JSON"])["object"]["sha"])')" || \
      die "could not parse branch metadata: $BRANCH"
    [[ -n "$REMOTE_COMMIT" ]] || die "could not resolve branch: $BRANCH"
    BUILD_ID="${REMOTE_COMMIT:0:7}"
  fi

  # Prefix the plain GitHub URL with the mirror; an explicit --archive-url wins.
  if [[ -n "$MIRROR" && "$ARCHIVE_URL" == "$DEFAULT_ARCHIVE_URL" ]]; then
    ARCHIVE_URL="${MIRROR}${ARCHIVE_URL}"
  fi
  log "Downloading application archive"
  WORK_DIR="$(mktemp -d /tmp/linux-router-install.XXXXXX)"
  install -d -m 0755 "$WORK_DIR/source"
  curl --fail --location --silent --show-error --retry 3 \
    --output "$WORK_DIR/source.tar.gz" "$ARCHIVE_URL"
  tar -xzf "$WORK_DIR/source.tar.gz" -C "$WORK_DIR/source" --strip-components=1
fi

[[ -f "$WORK_DIR/source/app.py" ]] || die "downloaded archive does not contain app.py"
[[ -f "$WORK_DIR/source/router-panel.service" ]] || die "downloaded archive does not contain router-panel.service"
[[ -f "$WORK_DIR/source/router-panel-agent.service" ]] || die "downloaded archive does not contain router-panel-agent.service"
[[ -f "$WORK_DIR/source/agent.py" ]] || die "downloaded archive does not contain agent.py"
[[ -d "$WORK_DIR/source/router_panel" ]] || die "downloaded archive does not contain router_panel"
[[ -d "$WORK_DIR/source/templates" ]] || die "downloaded archive does not contain templates"
python3 -m py_compile "$WORK_DIR/source/app.py" "$WORK_DIR/source/agent.py" "$WORK_DIR/source/router_panel/"*.py

ROLLBACK_DIR="$WORK_DIR/rollback"
install -d -m 0700 "$ROLLBACK_DIR"
INSTALL_SWAP_STARTED=1
if [[ -e "$INSTALL_DIR" ]]; then
  INSTALL_DIR_EXISTED=1
fi
if [[ "$ACTION" == "install" && -e "$DATA_DIR" ]]; then
  DATA_DIR_EXISTED=1
  cp -a "$DATA_DIR" "$ROLLBACK_DIR/data-dir"
fi
service_is_active "$SERVICE_NAME" && WEB_WAS_ACTIVE=1
service_is_enabled "$SERVICE_NAME" && WEB_WAS_ENABLED=1
service_is_active "$AGENT_SERVICE_NAME" && AGENT_WAS_ACTIVE=1
service_is_enabled "$AGENT_SERVICE_NAME" && AGENT_WAS_ENABLED=1
backup_file "/etc/systemd/system/$SERVICE_NAME" "web.service"
backup_file "/etc/systemd/system/$AGENT_SERVICE_NAME" "agent.service"
if [[ "$ACTION" == "install" ]]; then
  service_is_active NetworkManager.service && NETWORKMANAGER_WAS_ACTIVE=1
  service_is_enabled NetworkManager.service && NETWORKMANAGER_WAS_ENABLED=1
  service_is_active dhcpcd.service && DHCPCD_WAS_ACTIVE=1
  service_is_enabled dhcpcd.service && DHCPCD_WAS_ENABLED=1
  service_is_active systemd-resolved.service && RESOLVED_WAS_ACTIVE=1
  service_is_enabled systemd-resolved.service && RESOLVED_WAS_ENABLED=1
  service_is_active dnsmasq.service && DNSMASQ_WAS_ACTIVE=1
  service_is_enabled dnsmasq.service && DNSMASQ_WAS_ENABLED=1
  service_is_active nftables.service && NFTABLES_WAS_ACTIVE=1
  service_is_enabled nftables.service && NFTABLES_WAS_ENABLED=1
  ORIGINAL_IP_FORWARD="$(sysctl -n net.ipv4.ip_forward 2>/dev/null || true)"
  ORIGINAL_IPV6_FORWARD="$(sysctl -n net.ipv6.conf.all.forwarding 2>/dev/null || true)"
  backup_file "$SYSCTL_FILE" "sysctl.conf"
  backup_file "/etc/NetworkManager/conf.d/90-linux-router.conf" "networkmanager.conf"
  backup_file "/etc/netplan/90-linux-router.yaml" "netplan.yaml"
  backup_file "$NFTABLES_CONF" "nftables.conf"
  backup_file "$DNSMASQ_CONF" "dnsmasq.conf"
fi
INSTALL_STARTED=1

install -d -m 0755 "$(dirname "$INSTALL_DIR")"
STAGING_DIR="${INSTALL_DIR}.new.$$"
BACKUP_DIR="${INSTALL_DIR}.previous"
rm -rf "$STAGING_DIR" "$BACKUP_DIR"
install -d -m 0755 "$STAGING_DIR"
cp -a "$WORK_DIR/source/." "$STAGING_DIR/"
printf 'branch=%s\nbuild=%s\n' "$BRANCH" "$BUILD_ID" > "$STAGING_DIR/BUILD_INFO"
chown -R root:root "$STAGING_DIR"

# Runtime secrets always live in DATA_DIR, never in downloaded application files.
rm -f \
  "$STAGING_DIR/data/auth.json" \
  "$STAGING_DIR/data/initial_password.txt" \
  "$STAGING_DIR/data/secret_key" \
  "$STAGING_DIR/data/router.json" \
  "$STAGING_DIR/data/network.json"

if [[ -e "$INSTALL_DIR" ]]; then
  mv "$INSTALL_DIR" "$BACKUP_DIR"
fi
mv "$STAGING_DIR" "$INSTALL_DIR"
STAGING_DIR=""

[[ -f "$INSTALL_DIR/app.py" ]] || die "app.py was not found in $INSTALL_DIR"
if [[ "$ACTION" == "upgrade" ]]; then
  log "Installing upgraded services"
  install_service_units
  INSTALL_COMMITTED=1
  log "Linux Router upgrade completed"
  exit 0
fi

DATA_TOUCHED=1
if ! getent group "$SERVICE_USER" >/dev/null; then
  groupadd --system "$SERVICE_USER"
  SERVICE_GROUP_CREATED=1
fi
if ! getent passwd "$SERVICE_USER" >/dev/null; then
  useradd --system --gid "$SERVICE_USER" --home-dir "$DATA_DIR" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
  SERVICE_USER_CREATED=1
fi
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0700 "$DATA_DIR"

if [[ ! -f "$DATA_DIR/auth.json" ]]; then
  NEW_PASSWORD="${INITIAL_PASSWORD:-$(python3 -c 'import secrets; print(secrets.token_urlsafe(15))')}"
  log "Creating initial administrator credentials"
fi

LINUX_ROUTER_DATA_DIR="$DATA_DIR" \
LINUX_ROUTER_INITIAL_PASSWORD="$NEW_PASSWORD" \
python3 -c "import sys; sys.path.insert(0, '$INSTALL_DIR'); import app"

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 0700 "$DATA_DIR"
find "$DATA_DIR" -type f -exec chmod 0600 {} +
persist_install_state

log "Enabling IPv4 and IPv6 forwarding"
cat > "$SYSCTL_FILE" <<'EOF'
# Generated by Linux Router
net.ipv4.ip_forward=1
net.ipv6.conf.all.forwarding=1
EOF
chmod 0644 "$SYSCTL_FILE"
sysctl -w net.ipv4.ip_forward=1 >/dev/null
sysctl -w net.ipv6.conf.all.forwarding=1 >/dev/null

log "Writing nftables and dnsmasq bootstrap configuration"
write_nftables_bootstrap
write_dnsmasq_bootstrap
systemctl enable nftables.service >/dev/null
systemctl restart nftables.service

# /etc/resolv.conf is maintained by the panel from the WAN link's DHCP DNS, so
# systemd-resolved is not required (a minimal Debian install does not ship it).
systemctl enable dnsmasq.service >/dev/null
systemctl restart dnsmasq.service || log "WARNING: dnsmasq failed to start; check journalctl -u dnsmasq"

log "Enabling systemd-networkd"
systemctl enable systemd-networkd.service >/dev/null
systemctl restart systemd-networkd.service

# The old stack keeps running for now: it still provides connectivity until the
# panel writes its own .network files. Applying the router config takes the
# interfaces over (stops NetworkManager/dhcpcd/ifupdown for those NICs).
log "networkd is ready; the panel will take the NICs over when you apply"

log "Installing systemd services"
install_service_units
INSTALL_COMMITTED=1

log "Linux Router is running at http://<device-ip>/"
if [[ -n "$NEW_PASSWORD" ]]; then
  printf '\nInitial login:\n  Username: admin\n  Password: %s\n' "$NEW_PASSWORD"
  printf 'Credentials are also stored in %s/initial_password.txt\n' "$DATA_DIR"
fi
printf '\nNext step: open the panel -> 路由, pick the WAN and LAN NICs, then apply.\n'
printf 'That writes /etc/systemd/network/*.network, /etc/dnsmasq.d/linux-router.conf\n'
printf 'and /etc/nftables.d/90-linux-router.nft (NAT + forwarding, IPv4 and IPv6).\n'
if [[ -n "${SSH_CONNECTION:-}" ]]; then
  printf '\nNetwork changes were deferred because the installer detected an SSH session.\n'
  printf 'Apply the WAN/LAN settings from the panel during a maintenance window.\n'
fi
