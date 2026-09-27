#!/bin/bash
# Audio Hub Installer
#
# Idempotent installer for the Audio Hub system. Run as root on the Pi:
#   sudo ./install.sh
#
# Everything user-level runs as the 'pi' user (override with
# AUDIOHUB_USER=<name>). The installer enables linger so the user's
# PipeWire session and services start at boot without a login.

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/etc/audiohub"
UNIT_ENV="$CONFIG_DIR/unit.env"
PI_USER="${AUDIOHUB_USER:-pi}"
PI_UID="$(id -u "$PI_USER" 2>/dev/null || echo "")"
PI_HOME=""
HUBD_PKG_DIR="/usr/local/lib/python3/dist-packages/hubd"

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

as_pi() {
    # Run a command as the hub user with the right runtime env.
    sudo -u "$PI_USER" -H env "XDG_RUNTIME_DIR=/run/user/$PI_UID" "HOME=$PI_HOME" "$@"
}

require_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root: sudo ./install.sh"
        exit 1
    fi
}

check_os() {
    if [ ! -f /etc/os-release ]; then
        log_error "Cannot detect OS"
        exit 1
    fi
    # shellcheck disable=SC1091
    . /etc/os-release
    if [ "${ID:-}" != "raspbian" ] && [ "${ID:-}" != "debian" ]; then
        log_warn "This installer targets Raspberry Pi OS / Debian (found: ${PRETTY_NAME:-unknown})"
    fi
    log_info "Detected: ${PRETTY_NAME:-unknown}"
}

check_user() {
    if ! id "$PI_USER" &>/dev/null; then
        log_error "User '$PI_USER' does not exist. Create it first (standard Raspberry Pi OS login)."
        exit 1
    fi
    PI_UID="$(id -u "$PI_USER")"
    PI_HOME="$(getent passwd "$PI_USER" | cut -d: -f6)"
    log_info "Hub user: $PI_USER (home: $PI_HOME)"
}

install_packages() {
    log_info "Installing packages..."

    apt-get update -qq

    # packages.txt allows comments; strip them and trim before handing to apt.
    mapfile -t packages < <(sed -e 's/#.*//' -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e '/^$/d' "$REPO_DIR/packages.txt")

    if [ "${#packages[@]}" -eq 0 ]; then
        log_error "No packages found in packages.txt"
        exit 1
    fi

    apt-get install -y "${packages[@]}"
    log_info "Packages installed"
}

ensure_user_setup() {
    log_info "Configuring user '$PI_USER'..."

    # Groups: audio (PipeWire), input (FLIRC evdev), bluetooth (rfkill/bt)
    for grp in audio input bluetooth; do
        if getent group "$grp" &>/dev/null; then
            usermod -aG "$grp" "$PI_USER"
        fi
    done

    # Linger: the user's systemd instance (PipeWire, hubd, sendspin) must
    # start at boot with nobody logged in. THE critical reboot fix.
    loginctl enable-linger "$PI_USER"

    if [ ! -d "/run/user/$PI_UID" ]; then
        log_warn "User manager not running yet for $PI_USER; starting it via linger..."
        systemctl start "user@$PI_UID.service" 2>/dev/null || true
    fi
    log_info "Linger enabled for $PI_USER"
}

remove_legacy() {
    # Remove v1 (howto-era) services and config drop-ins that conflict with
    # the declarative graph. No-ops on fresh installs (nothing matches).
    log_info "Removing legacy v1 services and configs..."

    local units="pw-static-links pw-vsink-watchdog pw-ur23-loopback audio-hub-mqtt duck-monitor filter-chain"
    if [ -d "/run/user/$PI_UID" ]; then
        local u
        for u in $units; do
            as_pi systemctl --user disable --now "$u.service" 2>/dev/null || true
        done
        as_pi systemctl --user daemon-reload 2>/dev/null || true
    fi

    local udir="$PI_HOME/.config/systemd/user"
    for u in $units; do
        rm -f "$udir/$u.service"
    done
    # stray wants-symlinks for removed units
    find "$udir" -maxdepth 2 -type l \( -name "pw-*" -o -name "audio-hub-mqtt*" \
        -o -name "duck-monitor*" -o -name "filter-chain*" \) -delete 2>/dev/null || true

    # v1 drop-ins: old virtual sinks and routing rules conflict with bus.*
    # (v1's 50-bluetooth-a2dp-sink.conf would override the new BT routing)
    rm -f "$PI_HOME/.config/pipewire/pipewire.conf.d/90-ducking-virtual-sinks.conf" \
          "$PI_HOME/.config/pipewire/pipewire.conf.d/90-audio-hub-sinks.conf" \
          "$PI_HOME/.config/pipewire/filter-chain.conf" \
          "$PI_HOME/.config/wireplumber/wireplumber.conf.d/50-bluetooth-a2dp-sink.conf" \
          "$PI_HOME/.config/wireplumber/wireplumber.conf.d/51-ur23-route.conf"

    # abandoned plan-a / v1 scripts
    rm -f "$PI_HOME/.local/bin/pw-vsink-watchdog.sh" \
          "$PI_HOME/.local/bin/audio-hub-mqtt.py" \
          "$PI_HOME/.local/bin/duck-monitor.sh" \
          "$PI_HOME/.local/bin/duck-program-on-key.sh" \
          "$PI_HOME/.local/bin/audio-hub-plan-a-migrate.sh" \
          "$PI_HOME/.local/bin/audio-hub-plan-a-rollback.sh" \
          "$PI_HOME/.local/bin/audio-hub-plan-a-verify.sh"

    # stale config backups scattered by earlier experiments
    find "$PI_HOME/.config/pipewire" "$PI_HOME/.config/wireplumber" -maxdepth 2 -type f \
        \( -name "*.save" -o -name "*.disabled" -o -name "*.plan-a-removed.*" -o -name "*.bak" \) \
        -delete 2>/dev/null || true
}

install_config() {
    log_info "Installing configuration files..."

    mkdir -p "$CONFIG_DIR"
    if [ ! -f "$UNIT_ENV" ]; then
        cp "$REPO_DIR/unit.env.example" "$UNIT_ENV"
        chmod 644 "$UNIT_ENV"
        log_warn "Created $UNIT_ENV from template"
        log_warn ">>> EDIT THIS FILE (MQTT credentials, device name) BEFORE REBOOT <<<"
    else
        log_info "$UNIT_ENV already exists, keeping it"
    fi

    # PipeWire configuration (virtual buses, clock, TV loopback, ducts)
    local pw_dir="$PI_HOME/.config/pipewire/pipewire.conf.d"
    mkdir -p "$pw_dir"
    cp "$REPO_DIR"/config/pipewire.conf.d/*.conf "$pw_dir/"
    chown -R "$PI_USER:$PI_USER" "$PI_HOME/.config/pipewire"

    # WirePlumber configuration (UR23, BT A2DP, Sendspin routing)
    local wp_dir="$PI_HOME/.config/wireplumber/wireplumber.conf.d"
    mkdir -p "$wp_dir"
    cp "$REPO_DIR"/config/wireplumber.conf.d/*.conf "$wp_dir/"
    chown -R "$PI_USER:$PI_USER" "$PI_HOME/.config/wireplumber"

    # Bluetooth configuration
    cp "$REPO_DIR/config/bluetooth/main.conf" /etc/bluetooth/main.conf
    systemctl restart bluetooth || log_warn "Could not restart bluetooth (continuing)"

    log_info "Configuration files installed"
}

install_hubd() {
    log_info "Installing hubd daemon..."

    mkdir -p "$HUBD_PKG_DIR"
    cp "$REPO_DIR"/hubd/__init__.py "$REPO_DIR"/hubd/main.py "$HUBD_PKG_DIR/"
    chmod 644 "$HUBD_PKG_DIR"/*.py

    # hubd/main.py is self-contained (no relative imports), so a direct
    # symlink works as the entry point.
    ln -sf "$HUBD_PKG_DIR/main.py" /usr/local/bin/hubd
    chmod 755 "$HUBD_PKG_DIR/main.py"

    # Fail fast on syntax errors rather than at boot
    python3 -m py_compile "$HUBD_PKG_DIR/main.py"

    log_info "hubd installed to /usr/local/bin/hubd"
}

install_sendspin() {
    log_info "Installing Sendspin client..."

    local bin="$PI_HOME/.local/bin/sendspin"
    if [ ! -x "$bin" ]; then
        # Install uv + the sendspin tool under the hub user's home, which is
        # what the official installer does when run via sudo. We skip the
        # official script's system-level unit entirely (we provide our own
        # user unit).
        as_pi bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh' \
            || { log_error "uv installation failed"; exit 1; }
        as_pi "$PI_HOME/.local/bin/uv" tool install sendspin \
            || { log_error "sendspin installation failed"; exit 1; }
    else
        log_info "sendspin already installed, upgrading"
        as_pi "$PI_HOME/.local/bin/uv" tool upgrade sendspin || true
    fi

    # Boot-time settings sync + device index detection
    local local_bin="$PI_HOME/.local/bin"
    mkdir -p "$local_bin"
    install -o "$PI_USER" -g "$PI_USER" -m 755 \
        "$REPO_DIR/scripts/sendspin-detect-device.sh" "$local_bin/sendspin-detect-device.sh"

    log_info "Sendspin installed ($bin)"
}

install_services() {
    log_info "Installing systemd services..."

    # System services
    install -m 644 "$REPO_DIR/systemd/system/bt-agent.service" /etc/systemd/system/
    install -m 644 "$REPO_DIR/systemd/system/bluetooth-setup.service" /etc/systemd/system/
    install -m 644 "$REPO_DIR/systemd/system/wifi-powersave-off.service" /etc/systemd/system/
    install -m 755 "$REPO_DIR/scripts/bluetooth-setup.sh" /usr/local/sbin/bluetooth-setup.sh

    # User services
    local user_dir="$PI_HOME/.config/systemd/user"
    mkdir -p "$user_dir"
    install -o "$PI_USER" -g "$PI_USER" -m 644 \
        "$REPO_DIR/systemd/user/hubd.service" "$user_dir/hubd.service"
    install -o "$PI_USER" -g "$PI_USER" -m 644 \
        "$REPO_DIR/systemd/user/sendspin.service" "$user_dir/sendspin.service"

    systemctl daemon-reload

    systemctl enable --now bt-agent.service
    systemctl enable --now bluetooth-setup.service
    systemctl enable --now wifi-powersave-off.service

    # User services: enabled inside the hub user's manager
    if [ -d "/run/user/$PI_UID" ]; then
        as_pi systemctl --user daemon-reload
        as_pi systemctl --user enable hubd.service sendspin.service
        log_info "hubd.service and sendspin.service enabled for $PI_USER"
    else
        log_warn "User manager for $PI_USER is not running; the units are installed"
        log_warn "and will be picked up at next boot (linger is enabled)."
    fi

    log_info "Services installed"
}

configure_wifi() {
    # Device-specific; opt-in via AUDIOHUB_WIFI_CONNECTION=<ssid>
    local conn="${AUDIOHUB_WIFI_CONNECTION:-}"
    if [ -z "$conn" ]; then
        log_info "AUDIOHUB_WIFI_CONNECTION not set; skipping WiFi tuning"
        return
    fi
    if nmcli connection show "$conn" &>/dev/null; then
        log_info "Tuning WiFi connection '$conn'..."
        nmcli connection modify "$conn" \
            connection.autoconnect-retries 0 \
            auth-retries 0 \
            802-11-wireless.powersave 2
    else
        log_warn "WiFi connection '$conn' not found; configure manually if needed"
    fi
}

validate_install() {
    log_info "Validating installation..."

    local failed=0

    [ -f "$UNIT_ENV" ] || { log_error "$UNIT_ENV missing"; failed=1; }
    grep -q "bus.tv" "$PI_HOME/.config/pipewire/pipewire.conf.d/"*.conf \
        || { log_error "Virtual bus configuration missing"; failed=1; }
    grep -q "duct.tv" "$PI_HOME/.config/pipewire/pipewire.conf.d/"*.conf \
        || { log_error "Duct configuration missing"; failed=1; }
    grep -q "monitor.channel-volumes" "$PI_HOME/.config/pipewire/pipewire.conf.d/20-virtual-buses.conf" \
        || { log_error "20-virtual-buses.conf is missing monitor.channel-volumes (volume would be inaudible)"; failed=1; }
    [ -x /usr/local/bin/hubd ] || { log_error "hubd not installed"; failed=1; }
    python3 -c "import paho.mqtt, pulsectl, evdev" 2>/dev/null \
        || { log_error "Python dependencies not importable (paho-mqtt/pulsectl/evdev)"; failed=1; }
    loginctl show-user "$PI_USER" --property=Linger 2>/dev/null | grep -q yes \
        || { log_error "Linger not enabled for $PI_USER"; failed=1; }

    if [ "$failed" -ne 0 ]; then
        log_error "Validation FAILED — fix the issues above before rebooting"
        exit 1
    fi
    log_info "Validation passed"
}

print_status() {
    log_info "Installation complete!"
    echo ""
    echo "Next steps:"
    echo "  1. Edit $UNIT_ENV — set MQTT host/credentials and the device identity."
    echo "  2. Reboot to load all configs:   sudo reboot"
    echo "  3. After reboot, verify as $PI_USER (not root):"
    echo "       systemctl --user status hubd.service sendspin.service"
    echo "       bash $REPO_DIR/scripts/validate.sh"
    echo ""
    echo "Bluetooth pairing uses bt-agent (auto-accept). The adapter is made"
    echo "discoverable at boot by bluetooth-setup.service."
}

main() {
    log_info "Audio Hub Installer"
    echo ""

    require_root
    check_os
    check_user
    install_packages
    ensure_user_setup
    remove_legacy
    install_config
    install_hubd
    install_sendspin
    install_services
    configure_wifi
    validate_install
    print_status
}

main "$@"
