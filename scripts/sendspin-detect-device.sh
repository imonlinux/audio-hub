#!/bin/bash
# Sendspin provisioning: syncs ~/.config/sendspin/settings-daemon.json with
# the unit identity from /etc/audiohub/unit.env and detects the PipeWire
# ALSA PCM index for this boot.
#
# USB enumeration order is not stable between reboots, so the numeric
# audio_device index must be re-detected every boot (v1-proven mechanism).
# Runs as ExecStartPre of sendspin.service (user: pi). Exits nonzero when
# detection fails so systemd retries via Restart=always.

set -u

UNIT_ENV="${AUDIOHUB_CONFIG:-/etc/audiohub/unit.env}"
SETTINGS="$HOME/.config/sendspin/settings-daemon.json"
SENDSPIN_BIN="$HOME/.local/bin/sendspin"
SENDSPIN_PY="$HOME/.local/share/uv/tools/sendspin/bin/python"

if [ ! -x "$SENDSPIN_BIN" ] || [ ! -x "$SENDSPIN_PY" ]; then
    echo "sendspin-detect-device: sendspin not installed at $SENDSPIN_BIN" >&2
    exit 1
fi

mkdir -p "$(dirname "$SETTINGS")"

idx=$("$SENDSPIN_PY" "$SENDSPIN_BIN" --list-audio-devices 2>/dev/null \
    | grep -i "pipewire" \
    | grep -oP '(?<=\[)\d+(?=\])' \
    | head -1)

if [ -z "$idx" ]; then
    echo "sendspin-detect-device: could not detect PipeWire audio device index" >&2
    exit 1
fi

python3 - "$UNIT_ENV" "$SETTINGS" "$idx" <<'PYEOF'
import json, os, sys

unit_env, settings, idx = sys.argv[1], sys.argv[2], sys.argv[3]

props = {}
if os.path.exists(unit_env):
    with open(unit_env) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                props[k.strip()] = v.strip().strip('"')

managed_audio_device = {"audio_device": str(idx)}

fresh = {
    "log_level": None,
    "listen_port": None,
    "player_volume": 25,
    "player_muted": False,
    "static_delay_ms": 0,
    "last_server_url": None,
    "use_mpris": False,
    # Only used when creating a fresh settings file; an existing file keeps
    # its own identity (e.g. v1 units use dash-style client_ids that Music
    # Assistant already knows).
    "name": props.get("AUDIOHUB_DEVICE_NAME", "Audio Hub"),
    "client_id": props.get("AUDIOHUB_DEVICE_ID", "audio_hub"),
    **managed_audio_device,
}

data = fresh
if os.path.exists(settings):
    try:
        with open(settings) as f:
            existing = json.load(f)
        # Preserve the unit's identity; only refresh the device index.
        existing.update(managed_audio_device)
        data = existing
        fresh = False
    except Exception as e:
        print(f"sendspin-detect-device: ignoring unreadable settings ({e})")

with open(settings, "w") as f:
    json.dump(data, f, indent=2)
print(f"sendspin-detect-device: audio_device={idx} name={data.get('name')!r} client_id={data.get('client_id')!r}")
PYEOF
