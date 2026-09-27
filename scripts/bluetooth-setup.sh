#!/bin/bash
# Ensure the Bluetooth adapter is unblocked, powered, and pairable at boot.
#
# rfkill soft-blocks persist across reboots, and BlueZ does not restore
# discoverable/pairable adapter state, so without this a phone either
# cannot pair or cannot see the hub after a reboot. Idempotent; safe to
# run on every boot.

set -u

rfkill unblock bluetooth 2>/dev/null || true

# Wait for bluetoothd to expose the controller (it is dbus-activated, so
# After=bluetooth.service alone does not guarantee readiness). Wait for
# EXISTENCE, not power: powering on is this script's job — the adapter may
# legitimately be down when we get here (rfkill, AutoEnable unset).
for _ in $(seq 1 15); do
    if bluetoothctl list 2>/dev/null | grep -q "Controller"; then
        break
    fi
    sleep 2
done

bluetoothctl power on >/dev/null 2>&1 || true
bluetoothctl discoverable on >/dev/null 2>&1 || true
bluetoothctl pairable on >/dev/null 2>&1 || true

# Per-unit Bluetooth name from the unit config (the repo main.conf carries a
# placeholder Name; the adapter alias is what phones actually display).
UNIT_ENV="${AUDIOHUB_CONFIG:-/etc/audiohub/unit.env}"
if [ -r "$UNIT_ENV" ]; then
    UNIT_NAME=$(grep -E "^AUDIOHUB_DEVICE_NAME=" "$UNIT_ENV" | cut -d= -f2-)
    if [ -n "$UNIT_NAME" ]; then
        bluetoothctl system-alias "$UNIT_NAME" >/dev/null 2>&1 || true
    fi
fi

if bluetoothctl show 2>/dev/null | grep -qi "Powered: yes"; then
    exit 0
fi

echo "bluetooth-setup: adapter did not come up" >&2
exit 1
