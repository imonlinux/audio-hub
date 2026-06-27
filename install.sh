#!/bin/bash
# Audio Hub Installer
# Idempotent installer for the Audio Hub system.
# Run this after cloning the repo to a fresh or existing system.

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="/etc/audiohub"
UNIT_ENV="$CONFIG_DIR/unit.env"
SERVICE_DIR="/etc/systemd/system"

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "This script must be run as root (for system config)"
        log_info "For user-level install, run without sudo for user services only"
        exit 1
    fi
}

check_os() {
    if [ ! -f /etc/os-release ]; then
        log_error "Cannot detect OS"
        exit 1
    fi

    . /etc/os-release
    if [ "$ID" != "raspbian" ] && [ "$ID" != "debian" ]; then
        log_warn "This script is designed for Raspberry Pi OS / Debian"
        log_warn "Proceeding anyway, but things may not work"
    fi

    log_info "Detected: $PRETTY_NAME"
}

install_packages() {
    log_info "Installing required packages..."

    # Update package list
    apt-get update -qq

    # Install packages
    xargs -a "$REPO_DIR/packages.txt" -r apt-get install -y

    log_info "Packages installed"
}

setup_audiohub_user() {
    log_info "Setting up audiohub user..."

    if ! id audiohub &>/dev/null; then
        useradd -r -G audio,input,bluetooth -s /bin/bash -d /var/lib/audiohub audiohub
        log_info "Created audiohub user"
    else
        log_info "audiohub user already exists"
    fi

    # Enable linger for persistent user session
    loginctl enable-linger audiohub
    log_info "Enabled linger for audiohub user"
}

install_config() {
    log_info "Installing configuration files..."

    mkdir -p "$CONFIG_DIR"

    # Copy unit.env if not exists
    if [ ! -f "$UNIT_ENV" ]; then
        cp "$REPO_DIR/unit.env.example" "$UNIT_ENV"
        log_warn "Created $UNIT_ENV from template"
        log_warn "Edit this file with your settings!"
    else
        log_info "$UNIT_ENV already exists, skipping"
    fi

    # PipeWire configuration
    mkdir -p /home/pi/.config/pipewire/pipewire.conf.d
    cp -r "$REPO_DIR/config/pipewire.conf.d"/* /home/pi/.config/pipewire/pipewire.conf.d/
    chown -R pi:pi /home/pi/.config/pipewire

    # WirePlumber configuration
    mkdir -p /home/pi/.config/wireplumber/wireplumber.conf.d
    cp -r "$REPO_DIR/config/wireplumber.conf.d"/* /home/pi/.config/wireplumber/wireplumber.conf.d/
    chown -R pi:pi /home/pi/.config/wireplumber

    # Bluetooth configuration
    cp "$REPO_DIR/config/bluetooth/main.conf" /etc/bluetooth/main.conf
    systemctl restart bluetooth

    log_info "Configuration files installed"
}

install_hubd() {
    log_info "Installing hubd daemon..."

    # Create hubd package directory
    HUBD_DIR="/usr/local/lib/python3/dist-packages/hubd"
    mkdir -p "$HUBD_DIR"
    cp "$REPO_DIR/hubd"/*.py "$HUBD_DIR/"
    chmod +x "$HUBD_DIR/main.py"

    # Create symlink
    ln -sf "$HUBD_DIR/main.py" /usr/local/bin/hubd

    log_info "hubd installed"
}

install_services() {
    log_info "Installing systemd services..."

    # User services (run as audiohub or pi)
    USER_SERVICE_DIR="/home/pi/.config/systemd/user"
    mkdir -p "$USER_SERVICE_DIR"

    cp "$REPO_DIR/systemd/user/hubd.service" "$USER_SERVICE_DIR/"
    cp "$REPO_DIR/systemd/user/sendspin.service" "$USER_SERVICE_DIR/"

    chown -R pi:pi "$USER_SERVICE_DIR"

    # System service for WiFi powersave
    cp "$REPO_DIR/systemd/system/wifi-powersave-off.service" "$SERVICE_DIR/"
    systemctl daemon-reload
    systemctl enable wifi-powersave-off.service
    systemctl start wifi-powersave-off.service

    log_info "Services installed"
}

setup_wifi() {
    log_info "Setting up WiFi configuration..."

    # Check for existing McWiFi connection
    if nmcli connection show "McWiFi" &>/dev/null; then
        log_info "McWiFi connection exists, updating settings..."

        # Set infinite retries
        nmcli connection modify "McWiFi" \
            connection.autoconnect-retries 0 \
            auth-retries 0 \
            802-11-wireless.powersave 2

        log_info "WiFi settings updated"
    else
        log_warn "McWiFi connection not found"
        log_warn "You'll need to configure WiFi manually"
    fi
}

validate_install() {
    log_info "Validating installation..."

    # Check PipeWire
    if ! systemctl --user is-active pipewire.service &>/dev/null; then
        log_warn "PipeWire not running - may need to start user services"
    fi

    # Check configuration
    if [ ! -f "$UNIT_ENV" ]; then
        log_error "$UNIT_ENV not found!"
        log_error "Please configure the unit before starting services"
        return 1
    fi

    # Check for virtual buses in config
    if ! grep -q "bus.tv" /home/pi/.config/pipewire/pipewire.conf.d/*.conf; then
        log_error "Virtual bus configuration not found"
        return 1
    fi

    log_info "Validation passed"
}

print_status() {
    log_info "Installation complete!"
    echo ""
    echo "Next steps:"
    echo "1. Edit $UNIT_ENV with your settings"
    echo "2. Reboot to load all configurations"
    echo "3. Check status: systemctl --user status pipewire wireplumber"
    echo "4. Start hubd: systemctl --user start hubd.service"
    echo ""
    echo "To enable services at boot:"
    echo "  systemctl --user enable hubd.service"
    echo "  systemctl --user enable sendspin.service"
}

# Main installation flow
main() {
    log_info "Audio Hub Installer"
    echo ""

    # Check if running as root for system-level changes
    if [ "$EUID" -eq 0 ]; then
        check_os
        install_packages
        setup_audiohub_user
        setup_wifi
    else
        log_info "Running in user-only mode (no system changes)"
    fi

    install_config
    install_hubd
    install_services

    if [ "$EUID" -eq 0 ]; then
        validate_install
    fi

    print_status
}

main "$@"
