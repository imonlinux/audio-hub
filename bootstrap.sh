#!/bin/bash
# Audio Hub bootstrap — one-command provisioning for a fresh Raspberry Pi.
#
# On the Pi (as a user with sudo):
#   curl -fsSL https://raw.githubusercontent.com/imonlinux/audio-hub/main/bootstrap.sh -o bootstrap.sh
#   sudo bash bootstrap.sh
#
# What it does:
#   1. Resolves the release to install (channels below, or AUDIOHUB_RELEASE)
#   2. Downloads the release tarball and verifies it against the
#      sha256sums.txt asset published with that release by CI
#   3. Installs the tree to /opt/audio-hub and runs its install.sh
#   4. Stamps the deployed version into /etc/audiohub/version
#
# Channels:
#   stable   newest promoted release (default; a release becomes stable when
#            its "pre-release" flag is unticked on GitHub)
#   canary   newest release including pre-releases (soak channel)
#
# Env overrides:
#   AUDIOHUB_USER      hub user (default: 'pi' if it exists, else the first
#                      regular user on the system)
#   AUDIOHUB_RELEASE   pin an exact tag (e.g. v1.0.0) instead of a channel
#   AUDIOHUB_CHANNEL   stable (default) or canary
#
# Idempotent: re-running updates an existing deployment in place;
# /etc/audiohub/unit.env is preserved by install.sh.

set -euo pipefail

GH_REPO="imonlinux/audio-hub"
API_REPO="https://api.github.com/repos/${GH_REPO}"
TARBALL_BASE="https://github.com/${GH_REPO}/archive"
INSTALL_ROOT="/opt/audio-hub"
WORK=""   # download staging dir; EXIT trap cleans it (global on purpose)

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1" >&2; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1" >&2; }
log_error() { echo -e "${RED}[ERROR]${NC} $1" >&2; }

# ---------------------------------------------------------------------------
# HTTP + release resolution (self-contained: nothing here trusts the repo
# tree yet — verification happens before any downloaded code is executed)
# ---------------------------------------------------------------------------

http_get() { # http_get <url> <outfile>
    if command -v curl &>/dev/null; then
        curl -fsSL "$1" -o "$2"
    else
        wget -qO "$2" "$1"
    fi
}

# GitHub API GET; prints the response with the HTTP code appended as the
# last line (000 = could not connect). Uses curl directly so the status
# stays distinguishable; curl is guaranteed present (ensure_fetch_tools
# installs it if missing).
api_get() { # api_get <path>
    curl -sS -w '\n%{http_code}' "$API_REPO$1" 2>/dev/null || printf '\n000'
}

# Split api_get output: sets API_CODE and API_BODY
api_split() {
    API_CODE="${1##*$'\n'}"
    API_BODY="${1%$'\n'*}"
}

# First tag_name field of a GitHub releases API response
json_tag() {
    sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -1
}

resolve_release() { # resolve_release <stable|canary> -> tag on stdout
    local channel="$1" out tag
    case "$channel" in
        canary)
            out="$(api_get "/releases?per_page=1")"; api_split "$out"
            if [ "$API_CODE" != "200" ]; then
                log_error "Cannot reach the GitHub API (HTTP $API_CODE)"
                return 1
            fi
            tag="$(json_tag <<<"$API_BODY")"
            ;;
        stable)
            out="$(api_get /releases/latest)"; api_split "$out"
            case "$API_CODE" in
                200)
                    tag="$(json_tag <<<"$API_BODY")"
                    ;;
                404)
                    # /releases/latest is 404 exactly when every release is
                    # still a pre-release — fall back to the newest one.
                    log_warn "No stable release promoted yet; falling back to the newest (canary) release"
                    out="$(api_get "/releases?per_page=1")"; api_split "$out"
                    if [ "$API_CODE" != "200" ]; then
                        log_error "Cannot reach the GitHub API (HTTP $API_CODE)"
                        return 1
                    fi
                    tag="$(json_tag <<<"$API_BODY")"
                    ;;
                *)
                    log_error "Cannot reach the GitHub API (HTTP $API_CODE)"
                    return 1
                    ;;
            esac
            ;;
        *)
            log_error "Unknown channel '$channel' (expected stable or canary)"
            return 1
            ;;
    esac
    if [ -z "$tag" ]; then
        log_error "No release found. Has the first release been cut? (push a v* tag, or set AUDIOHUB_RELEASE)"
        return 1
    fi
    echo "$tag"
}

# Downloads the tag's tarball + checksum asset into $2 and verifies it.
# On success prints the extracted tree directory.
fetch_and_verify() { # fetch_and_verify <tag> <workdir>
    local tag="$1" work="$2"
    local tgz="audio-hub-${tag}.tar.gz"
    local url_tgz="${TARBALL_BASE}/refs/tags/${tag}.tar.gz"
    local url_sha="https://github.com/${GH_REPO}/releases/download/${tag}/sha256sums.txt"

    log_info "Downloading release $tag ..."
    http_get "$url_tgz" "$work/$tgz"
    if ! http_get "$url_sha" "$work/sha256sums.txt"; then
        log_error "Release $tag has no sha256sums.txt asset — refusing to install unverified code."
        log_error "Cut the release via the CI workflow (push the tag), or pin a tag that has checksums."
        return 1
    fi
    (cd "$work" && grep -E "[[:space:]]${tgz}\$" sha256sums.txt | sha256sum --check --status -) || {
        log_error "Checksum verification FAILED for release $tag — aborting"
        return 1
    }
    log_info "Checksum OK"

    mkdir -p "$work/tree"
    tar -xzf "$work/$tgz" -C "$work/tree" --strip-components=1
    echo "$work/tree"
}

# ---------------------------------------------------------------------------
# Hub user selection (same policy install.sh applies when it exists)
# ---------------------------------------------------------------------------

pick_hub_user() {
    if [ -n "${AUDIOHUB_USER:-}" ]; then
        echo "$AUDIOHUB_USER"
        return
    fi
    if id pi &>/dev/null; then
        echo "pi"
        return
    fi
    # First regular user (uid >= 1000) with a home and a login shell
    awk -F: '$3 >= 1000 && $1 != "nobody" && $6 != "" && $7 !~ /(false|nologin)$/ { print $1; exit }' /etc/passwd
}

# ---------------------------------------------------------------------------

require_root() {
    if [ "${EUID:-$(id -u)}" -ne 0 ]; then
        log_error "Run as root: sudo bash $0"
        exit 1
    fi
}

ensure_fetch_tools() {
    if ! command -v curl &>/dev/null && ! command -v wget &>/dev/null; then
        log_info "Installing curl (download tool) ..."
        apt-get update -qq
        apt-get install -y curl ca-certificates
    fi
}

swap_tree() { # swap_tree <new-tree-dir> — install new tree, keep one .old
    if [ -d "$INSTALL_ROOT" ]; then
        rm -rf "$INSTALL_ROOT.old"
        mv "$INSTALL_ROOT" "$INSTALL_ROOT.old"
    fi
    mv "$1" "$INSTALL_ROOT"
    chmod 755 "$INSTALL_ROOT"
}

print_next_steps() {
    local user="$1"
    echo ""
    log_info "Bootstrap complete."
    echo ""
    echo "Next steps:"
    echo "  1. Edit the unit config (MQTT credentials + device identity):"
    echo "       sudo nano /etc/audiohub/unit.env"
    echo "  2. Reboot to load all configs:   sudo reboot"
    echo "  3. After reboot, verify as $user (not root):"
    echo "       bash $INSTALL_ROOT/scripts/validate.sh"
    echo ""
    echo "The unit then updates itself from the release channel weekly"
    echo "(Sun 04:30 + random hour). See README.md for channels, manual"
    echo "updates and rollback:"
    echo "       sudo audiohub-update --check"
}

main() {
    require_root
    ensure_fetch_tools

    local hub_user channel tag tree
    hub_user="$(pick_hub_user)"
    if [ -z "$hub_user" ] || ! id "$hub_user" &>/dev/null; then
        log_error "No hub user found. Create one (Raspberry Pi Imager does this), or run:"
        log_error "  sudo AUDIOHUB_USER=<name> bash $0"
        exit 1
    fi
    log_info "Hub user: $hub_user"

    if [ -n "${AUDIOHUB_RELEASE:-}" ]; then
        tag="$AUDIOHUB_RELEASE"
        log_info "Release pinned via AUDIOHUB_RELEASE: $tag"
    else
        channel="${AUDIOHUB_CHANNEL:-stable}"
        tag="$(resolve_release "$channel")"
        log_info "Channel '$channel' -> release $tag"
    fi

    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    tree="$(fetch_and_verify "$tag" "$WORK")"

    swap_tree "$tree"
    echo "$tag" > "$INSTALL_ROOT/.release-tag"

    log_info "Running installer ..."
    (cd "$INSTALL_ROOT" && AUDIOHUB_USER="$hub_user" bash install.sh)

    # Version stamp (installer also does this from .release-tag; be certain)
    echo "$tag" > /etc/audiohub/version 2>/dev/null || true

    print_next_steps "$hub_user"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
