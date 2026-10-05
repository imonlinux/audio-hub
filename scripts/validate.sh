#!/bin/bash
# Audio Hub Validation
# Run ON the Pi as the hub user (pi), after install + reboot:
#   bash scripts/validate.sh

set -u

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

pass() { echo -e "${GREEN}✓${NC} $1"; ((PASS_COUNT++)); }
fail() { echo -e "${RED}✗${NC} $1"; ((FAIL_COUNT++)); }
warn() { echo -e "${YELLOW}⚠${NC} $1"; }

UNIT_ENV="${AUDIOHUB_CONFIG:-/etc/audiohub/unit.env}"

# --------------------------------------------------------------------------
user_service_active() {
    systemctl --user is-active "$1" &>/dev/null
}

system_service_active() {
    systemctl is-active "$1" &>/dev/null
}

check_pipewire() {
    echo ""
    echo "=== PipeWire Stack ==="
    for svc in pipewire wireplumber pipewire-pulse; do
        if user_service_active "$svc.service"; then
            pass "$svc.service running"
        else
            fail "$svc.service not running"
        fi
    done
}

check_virtual_buses() {
    echo ""
    echo "=== Virtual Buses ==="
    # wpctl shows descriptions only; pactl shows real node names
    for bus in "bus.tv" "bus.bt" "bus.music"; do
        if pactl list short sinks 2>/dev/null | grep -q "$bus"; then
            pass "Virtual bus $bus exists"
        else
            fail "Virtual bus $bus not found"
        fi
    done
    if grep -q "monitor.channel-volumes" "$HOME/.config/pipewire/pipewire.conf.d/20-virtual-buses.conf" 2>/dev/null; then
        pass "monitor.channel-volumes set on buses (volume audible)"
    else
        fail "monitor.channel-volumes missing — bus volume sliders would be silent"
    fi
}

check_ducts() {
    echo ""
    echo "=== Ducts (bus -> hardware sink) ==="
    # pw-link -l prints each port/link on separate lines; every duct
    # playback port appears twice (own block + hw-sink block) -> 3 ducts x
    # 2 channels x 2 = 12 lines.
    local lines
    lines="$(pw-link -l 2>/dev/null | grep -c 'duct\..*\.playback:output' || true)"
    if [ "$lines" -eq 12 ]; then
        pass "All 6 duct channels linked (3 ducts x stereo)"
    else
        fail "Expected 12 duct lines in pw-link output, found $lines (graph may still be assembling; retry in 30 s)"
    fi
}

check_no_feedback() {
    echo ""
    echo "=== Feedback Check ==="
    # The hardware sink's monitor must never be a link SOURCE (that is the
    # v1 boot feedback loop failure mode).
    local rogue
    rogue="$(pw-link -l 2>/dev/null | grep -c 'alsa_output.*:monitor_' || true)"
    if [ "$rogue" -eq 0 ]; then
        pass "No links sourcing the hardware-sink monitor"
    else
        fail "$rogue link(s) source the hardware-sink monitor — FEEDBACK LOOP RISK"
    fi
}

check_ur23() {
    echo ""
    echo "=== UR23 (TV Optical) ==="
    local live_node matches
    # The config covers both candidate node names (signal vs no-signal);
    # the live node must be one of them.
    mapfile -t targets < <(grep -oP 'target.object = "\Kalsa_input[^"]+' \
        "$HOME/.config/pipewire/pipewire.conf.d/40-loopback-tv.conf" 2>/dev/null)
    # wpctl shows descriptions only; pactl exposes real node names
    live_node="$(pactl list short sources 2>/dev/null | grep -oP 'alsa_input\.usb-HiFimeDIY\S+' | sort -u | head -1)"
    if [ -n "$live_node" ]; then
        pass "UR23 source present: $live_node"
    else
        warn "UR23 source not present (expected while the TV is off / no S/PDIF signal)"
    fi
    matches="$(printf '%s\n' "${targets[@]}" | grep -c "^${live_node}\$" || true)"
    if [ -n "$live_node" ] && [ "$matches" -ge 1 ]; then
        pass "Live UR23 node is covered by a loopback target"
    elif [ -n "$live_node" ]; then
        fail "Live UR23 node '$live_node' matches NO loopback target (${targets[*]}) — update 40-loopback-tv.conf"
    fi
}

check_bluetooth() {
    echo ""
    echo "=== Bluetooth ==="
    if system_service_active bluetooth.service; then
        pass "bluetooth.service running"
    else
        fail "bluetooth.service not running"
    fi
    if system_service_active bt-agent.service; then
        pass "bt-agent.service running (auto-accept pairing)"
    else
        fail "bt-agent.service not running — pairing will fail"
    fi
    if system_service_active bluetooth-setup.service; then
        pass "bluetooth-setup.service ran (unblock + discoverable)"
    else
        fail "bluetooth-setup.service not active"
    fi
    if bluetoothctl show 2>/dev/null | grep -q "Audio Sink"; then
        pass "A2DP Audio Sink UUID registered"
    else
        fail "Audio Sink UUID missing — WirePlumber has not registered the A2DP endpoint; phones will pair but get no audio. Restart wireplumber, then bluetooth."
    fi
}

check_no_second_stack() {
    echo ""
    echo "=== Single Audio Stack ==="
    # Generic: fail if ANY user other than the current hub user is running
    # a PipeWire daemon (the sendspin-user case is the known instance).
    local offenders
    offenders="$(ps -o user= -C pipewire 2>/dev/null | tr -d ' ' | sort -u | grep -v "^${USER}$" | tr '\n' ' ')"
    if [ -n "$offenders" ]; then
        fail "Second PipeWire instance(s) running as: ${offenders}— disable linger for those users"
    else
        pass "No second audio stack (single hub user runs PipeWire)"
    fi
    if id sendspin &>/dev/null && loginctl show-user sendspin --property=Linger 2>/dev/null | grep -q yes; then
        fail "Legacy 'sendspin' user still lingers — run: loginctl disable-linger sendspin"
    fi
}

check_services() {
    echo ""
    echo "=== Hub Services ==="
    if user_service_active hubd.service; then
        pass "hubd.service running"
    else
        fail "hubd.service not running (journalctl --user -u hubd.service)"
    fi
    if user_service_active sendspin.service; then
        pass "sendspin.service running"
    else
        warn "sendspin.service not running (needs Music Assistant to connect to be useful)"
    fi
    [ -f "$UNIT_ENV" ] && pass "Unit config present ($UNIT_ENV)" || fail "Unit config missing ($UNIT_ENV)"
    [ -f "$HOME/.config/sendspin/settings-daemon.json" ] \
        && pass "Sendspin settings present" \
        || warn "Sendspin settings not yet provisioned (created on first sendspin start)"
}

check_linger() {
    echo ""
    echo "=== Boot Resilience ==="
    if loginctl show-user "$USER" --property=Linger 2>/dev/null | grep -q yes; then
        pass "Linger enabled for $USER (services start at boot)"
    else
        fail "Linger NOT enabled for $USER — nothing user-level will start after reboot"
    fi
}

check_wifi() {
    echo ""
    echo "=== WiFi (optional) ==="
    # unit.env is the source of truth; env override for ad-hoc runs
    local conn="${AUDIOHUB_WIFI_CONNECTION:-}"
    if [ -z "$conn" ] && [ -r "$UNIT_ENV" ]; then
        conn="$(grep -E '^AUDIOHUB_WIFI_CONNECTION=' "$UNIT_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"' || true)"
    fi
    if [ -z "$conn" ]; then
        echo "  (optional) AUDIOHUB_WIFI_CONNECTION not configured — skipping"
        return 0
    fi
    if nmcli connection show "$conn" &>/dev/null; then
        pass "WiFi connection '$conn' present"
    else
        warn "WiFi connection '$conn' not found (set in unit.env but no such NetworkManager connection)"
    fi
}

check_updates() {
    echo ""
    echo "=== Release Updates ==="
    local version auto
    if [ -r /etc/audiohub/version ]; then
        version="$(cat /etc/audiohub/version)"
        pass "Deployed version stamped: $version"
    else
        warn "No version stamp at /etc/audiohub/version (development checkout?)"
    fi
    auto="$(grep -E '^AUDIOHUB_AUTO_UPDATE=' "$UNIT_ENV" 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '"')"
    auto="${auto:-true}"
    if [ "$auto" != "true" ]; then
        warn "Scheduled self-updates disabled (AUDIOHUB_AUTO_UPDATE=$auto)"
        return 0
    fi
    if systemctl is-active audiohub-update.timer &>/dev/null; then
        pass "audiohub-update.timer active (weekly release check)"
    else
        fail "AUDIOHUB_AUTO_UPDATE=$auto but audiohub-update.timer is not active — run the installer"
    fi
    if [ -x /usr/local/sbin/audiohub-update ]; then
        pass "audiohub-update present (/usr/local/sbin)"
    else
        fail "audiohub-update missing — re-run the installer"
    fi
    if grep -q "your_password_here" "$UNIT_ENV" 2>/dev/null; then
        fail "unit.env still has the placeholder MQTT password — set real credentials"
    fi
}

check_selection() {
    echo ""
    echo "=== Device Selection ==="
    if journalctl --user -u hubd.service --no-pager 2>/dev/null \
        | grep -qi "selection reconciler active"; then
        pass "Device-selection reconciler active"
    else
        fail "Selection reconciler not detected in the hubd journal (hubd build predates device selection?)"
    fi
}

print_summary() {
    echo ""
    echo "=== Summary ==="
    echo -e "${GREEN}Passed:${NC} $PASS_COUNT"
    echo -e "${RED}Failed:${NC} $FAIL_COUNT"
    if [ "$FAIL_COUNT" -eq 0 ]; then
        echo -e "\n${GREEN}All checks passed.${NC}"
        exit 0
    fi
    echo -e "\n${RED}Some checks failed.${NC}"
    exit 1
}

main() {
    echo "Audio Hub Validation"
    echo "===================="
    check_pipewire
    check_virtual_buses
    check_ducts
    check_no_feedback
    check_ur23
    check_bluetooth
    check_no_second_stack
    check_services
    check_linger
    check_wifi
    check_updates
    check_selection
    print_summary
}

main
