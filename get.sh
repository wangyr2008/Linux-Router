#!/usr/bin/env bash
# Linux Router bootstrap script.
#
# Fetches install.sh through the first GitHub endpoint that works (direct, then a
# few public mirrors) and runs it. Useful on networks where
# raw.githubusercontent.com is slow, reset, or blocked.
#
#   curl -fsSL https://raw.githubusercontent.com/wangyr2008/Linux-Router/main/get.sh \
#     -o /tmp/lr-get.sh
#   sudo bash /tmp/lr-get.sh install
#
# Everything after the options is passed straight to install.sh, for example:
#
#   sudo bash /tmp/lr-get.sh upgrade --source-dir /root/Linux-Router
#   sudo bash /tmp/lr-get.sh install --mirror https://ghfast.top/
#
# Only install.sh itself is fetched here; the source archive is downloaded by
# install.sh, which receives --mirror so it uses the same endpoint.
set -Eeuo pipefail

REPO="${LINUX_ROUTER_REPO:-wangyr2008/Linux-Router}"
BRANCH="${LINUX_ROUTER_BRANCH:-main}"
FORCED_MIRROR="${LINUX_ROUTER_MIRROR:-}"
TIMEOUT="${LINUX_ROUTER_FETCH_TIMEOUT:-20}"

# ghproxy-style mirrors: prefix + the full GitHub URL.
MIRRORS=(
  "https://ghfast.top/"
  "https://gh-proxy.com/"
  "https://ghproxy.net/"
  "https://hub.gitmirror.com/"
  "https://gh.llkk.cc/"
)

log() { printf '[linux-router] %s\n' "$*" >&2; }
die() { printf '[linux-router] ERROR: %s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Fetch and run the Linux Router installer through a GitHub mirror.

Usage: sudo bash get.sh <command> [install.sh options]

Commands (passed to install.sh):
  install        fresh installation
  upgrade        upgrade an existing installation
  uninstall      remove it (keeps credentials unless --purge-data)

Options handled here:
  --repo OWNER/NAME     repository to fetch from (default wangyr2008/Linux-Router)
  --branch NAME         branch to fetch from (default main)
  --mirror PREFIX       only try this mirror, e.g. https://ghfast.top/
  --list-mirrors        print the built-in mirror list and exit
  -h, --help            show this help

Any other option is forwarded to install.sh (--source-dir, --install-dir,
--data-dir, --purge-data, --archive-url, ...).

Environment: LINUX_ROUTER_REPO, LINUX_ROUTER_BRANCH, LINUX_ROUTER_MIRROR,
LINUX_ROUTER_FETCH_TIMEOUT (seconds, default 20).
EOF
}

FORWARD=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo)
      [[ $# -ge 2 ]] || die "--repo requires a value"
      REPO="$2"
      shift 2
      ;;
    --branch)
      [[ $# -ge 2 ]] || die "--branch requires a value"
      BRANCH="$2"
      shift 2
      ;;
    --mirror)
      [[ $# -ge 2 ]] || die "--mirror requires a value"
      FORCED_MIRROR="${2%/}"
      shift 2
      ;;
    --list-mirrors)
      printf '%s\n' "${MIRRORS[@]}"
      exit 0
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      FORWARD+=("$1")
      shift
      ;;
  esac
done

[[ "$REPO" =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ ]] || die "invalid repository: $REPO"
[[ "$BRANCH" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]*$ ]] || die "invalid branch: $BRANCH"

RAW="https://raw.githubusercontent.com/$REPO/$BRANCH/install.sh"

CANDIDATES=("$RAW")
if [[ -n "$FORCED_MIRROR" ]]; then
  CANDIDATES=("${FORCED_MIRROR}${RAW}")
else
  for mirror in "${MIRRORS[@]}"; do
    CANDIDATES+=("${mirror}${RAW}")
  done
fi

WORK_DIR="$(mktemp -d /tmp/linux-router-get.XXXXXX)"
trap 'rm -rf "$WORK_DIR"' EXIT
TARGET="$WORK_DIR/install.sh"

fetch() {
  local url="$1"
  log "Trying $url"
  curl --fail --location --silent --show-error \
    --connect-timeout 8 --max-time "$TIMEOUT" --retry 1 \
    --output "$TARGET" "$url" 2>/dev/null || return 1
  # Guard against a mirror returning an HTML error page with status 200.
  grep -q 'router-panel-agent.service' "$TARGET" || return 1
  grep -q '^set -Eeuo pipefail' "$TARGET" || return 1
  return 0
}

USED_MIRROR=""
for url in "${CANDIDATES[@]}"; do
  if fetch "$url"; then
    if [[ "$url" != "$RAW" ]]; then
      USED_MIRROR="${url%"$RAW"}"
      log "Fetched through $USED_MIRROR"
    else
      log "Fetched directly from GitHub"
    fi
    break
  fi
  log "Failed, trying the next endpoint"
done

[[ -s "$TARGET" ]] || die "could not fetch install.sh from any endpoint"

# Let install.sh download the source archive from the same endpoint that worked.
if [[ -n "$USED_MIRROR" && -z "$FORCED_MIRROR" ]]; then
  FORWARD+=(--mirror "$USED_MIRROR")
fi

chmod +x "$TARGET"
exec bash "$TARGET" "${FORWARD[@]}"
