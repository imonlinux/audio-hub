#!/bin/bash
# Audio Hub Validation Script
# Tests the implementation against the validation matrix

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS_COUNT=0
FAIL_COUNT=0

pass() {
    echo -e "${GREEN}✓${NC} $1"
    ((PASS_COUNT++))
}

fail() {
    echo -e "${RED}✗${NC} $1"
    ((FAIL_COUNT++))
}

warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

# Check if PipeWire is running
check_pipewire() {
    echo ""
    echo "=== PipeWire Status ==="
    if systemctl --user is-active pipewire.service &>/dev/null; then
        pass "PipeWire is running"
    else
        fail "PipeWire is not running"
    fi
}

# Check for virtual buses
check_virtual_buses() {
    echo ""
    echo "=== Virtual Buses ==="
    for bus in "bus.tv" "bus.bt" "bus.music"; do
        if wpctl status | grep -q "$bus"; then
            pass "Virtual bus $bus exists"
        else
            fail "Virtual bus $bus not found"
        fi
    done
}

# Check for duct loopbacks
check_ducts() {
    echo ""
    echo "=== Audio Ducts ==="
    for duct in "duct.tv" "duct.bt" "duct.music"; do
        if pw-link -i | grep -q "$duct"; then
            pass "Duct $duct exists"
        else
            fail "Duct $duct not found"
        fi
    done
}

# Check for UR23 device
check_ur23() {
    echo ""
    echo "=== UR23 Device ==="
    if wpctl status | grep -q "UR23"; then
        pass "UR23 device detected"
    else
        fail "UR23 device not found"
    fi
}

# Check for no stale monitor links
check_no_stale_links() {
    echo ""
    echo "=== Link Cleanliness ==="
    STALE=$(pw-link -o | grep -c "monitor.*fallback" || true)
    if [ "$STALE" -eq 0 ]; then
        pass "No stale monitor↔fallback links"
    else
        fail "Found $STALE stale monitor↔fallback links (Issue 1!)"
    fi
}

# Check Bluetooth is discoverable
check_bluetooth() {
    echo ""
    echo "=== Bluetooth ==="
    if systemctl is-active bluetooth &>/dev/null; then
        pass "Bluetooth service is running"
    else
        fail "Bluetooth service is not running"
    fi

    # Check discoverable timeout is 0
    if grep -q "DiscoverableTimeout=0" /etc/bluetooth/main.conf; then
        pass "DiscoverableTimeout = 0 (always discoverable)"
    else
        fail "DiscoverableTimeout not set to 0"
    fi
}

# Check WiFi settings
check_wifi() {
    echo ""
    echo "=== WiFi Settings ==="
    if nmcli connection show "McWiFi" &>/dev/null; then
        pass "McWiFi connection exists"

        RETRIES=$(nmcli -g connection.autoconnect-retries connection show "McWiFi")
        if [ "$RETRIES" = "0 (forever)" ] || [ "$RETRIES" = "0" ]; then
            pass "autoconnect-retries = 0 (infinite)"
        else
            fail "autoconnect-retries not set to infinite: $RETRIES"
        fi

        AUTH_RETRIES=$(nmcli -g auth-retries connection show "McWiFi")
        if [ "$AUTH_RETRIES" = "0 (forever)" ] || [ "$AUTH_RETRIES" = "0" ]; then
            pass "auth-retries = 0 (infinite)"
        else
            fail "auth-retries not set to infinite: $AUTH_RETRIES"
        fi
    else
        warn "McWiFi connection not found"
    fi
}

# Check for services
check_services() {
    echo ""
    echo "=== Services ==="
    for service in "pipewire" "wireplumber" "pipewire-pulse"; do
        if systemctl --user is-active "$service.service" &>/dev/null; then
            pass "$service.service is running"
        else
            fail "$service.service is not running"
        fi
    done

    if systemctl --user is-active hubd.service &>/dev/null; then
        pass "hubd.service is running"
    else
        warn "hubd.service not running (expected if not yet installed)"
    fi
}

# Print summary
print_summary() {
    echo ""
    echo "=== Summary ==="
    echo -e "${GREEN}Passed:${NC} $PASS_COUNT"
    echo -e "${RED}Failed:${NC} $FAIL_COUNT"

    if [ $FAIL_COUNT -eq 0 ]; then
        echo -e "\n${GREEN}All checks passed!${NC}"
        exit 0
    else
        echo -e "\n${RED}Some checks failed.${NC}"
        exit 1
    fi
}

# Main
main() {
    echo "Audio Hub Validation"
    echo "===================="

    check_pipewire
    check_virtual_buses
    check_ducts
    check_ur23
    check_no_stale_links
    check_bluetooth
    check_wifi
    check_services
    print_summary
}

main
