#!/bin/bash
# Sendspin provisioning: syncs ~/.config/sendspin/settings-daemon.json with
# the unit identity and the ducking/attach settings hubd relies on.
#
# - "audio_device": "pipewire" names the PipeWire PCM directly; unlike the
#   numeric index it does not change between boots (no detection needed).
# - use_hardware_volume=false keeps Music Assistant's volume slider on the
#   per-source music bus instead of the master volume.
# - hook_start/hook_stop drive hubd's ducking flag: the hook distinguishes
#   playing from paused/stopped, which the audio stream alone cannot.
#
# Runs as ExecStartPre of sendspin.service (user: pi). Always succeeds:
# with the stable device name there is nothing left to detect.

set -u

UNIT_ENV="${AUDIOHUB_CONFIG:-/etc/audiohub/unit.env}"
SETTINGS="$HOME/.config/sendspin/settings-daemon.json"
HOOK_BIN="$HOME/.local/bin/audiohub-music-hook"

mkdir -p "$(dirname "$SETTINGS")" "$(dirname "$HOOK_BIN")"

python3 - "$UNIT_ENV" "$SETTINGS" "$HOOK_BIN" <<'PYEOF'
import json, os, sys

unit_env, settings, hook_bin = sys.argv[1], sys.argv[2], sys.argv[3]

props = {}
if os.path.exists(unit_env):
    with open(unit_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip().strip('"')

managed = {
    "audio_device": "pipewire",
    "use_hardware_volume": False,
    "hook_start": f"{hook_bin} start",
    "hook_stop": f"{hook_bin} stop",
}

fresh = {
    "log_level": None,
    "listen_port": None,
    "player_volume": 25,
    "player_muted": False,
    "static_delay_ms": 0,
    "last_server_url": None,
    "use_mpris": False,
    # Only used when creating a fresh settings file; an existing file keeps
    # its own identity (Music Assistant already knows it).
    "name": props.get("AUDIOHUB_DEVICE_NAME", "Audio Hub"),
    "client_id": props.get("AUDIOHUB_DEVICE_ID", "audio_hub"),
    **managed,
}

data = fresh
if os.path.exists(settings):
    try:
        with open(settings) as f:
            existing = json.load(f)
        existing.update(managed)
        data = existing
    except Exception as e:
        print(f"sendspin-provision: ignoring unreadable settings ({e})")

with open(settings, "w") as f:
    json.dump(data, f, indent=2)
print(f"sendspin-provision: audio_device=pipewire name={data.get('name')!r} client_id={data.get('client_id')!r}")
PYEOF
exit 0
