# Raspberry Pi Audio Hub — Setup Guide

**Raspberry Pi 4B · PipeWire · WirePlumber · Home Assistant MQTT**

> **What this guide builds:** A multi-source audio hub that mixes three audio inputs — TV optical (via Hifime UR23), Bluetooth A2DP, and Music Assistant (via Sendspin) — to a single 3.5mm line-out. Audio from TV and Bluetooth is automatically ducked to 20% whenever Sendspin plays.
> 
> 
> The system is controllable from Home Assistant via MQTT with per-source volume sliders and playback state sensors. IR volume control is supported via a FLIRC USB receiver.

---

## Table of Contents

1. [Hardware & OS Requirements](#1-hardware--os-requirements)
2. [OS Package Installation](#2-os-package-installation)
3. [Project Software Installation](#3-project-software-installation)
4. [System Configuration](#4-system-configuration)
5. [Device-Specific Configuration](#5-device-specific-configuration)
6. [Platform Verification](#6-platform-verification)
7. [Troubleshooting](#7-troubleshooting)
8. [Appendix — File Reference](#8-appendix--file-reference)

---

## 1. Hardware & OS Requirements

### 1.1 Hardware

| Component                | Purpose                                                  |
| ------------------------ | -------------------------------------------------------- |
| Raspberry Pi 4B          | Main compute — runs PipeWire audio stack                 |
| Hifime UR23 USB SPDIF Rx | Optical-to-USB converter — connects TV optical out to Pi |
| Speaker system           | Connected to Pi 3.5mm line-out                           |
| Bluetooth device         | Phone or tablet connecting via A2DP                      |
| FLIRC USB IR receiver    | Optional — IR remote volume control                      |

> **USB port placement:** Connect the UR23 to a **USB 3.0 port** (blue). If a FLIRC or other USB HID device is also connected, placing both on USB 2.0 ports causes isochronous bandwidth contention and audio distortion. The UR23 must have its own USB controller.

### 1.2 Operating System

- **Debian GNU/Linux 13 (Trixie)**, aarch64, kernel 6.12+
- Raspberry Pi OS Trixie image
- User: `pi`, with lingering enabled (Section 4.2)

### 1.3 Signal Flow

```
TV (optical) ──► UR23 (USB 3.0) ──► pw-loopback ──► vsink.program ──► line-out
BT Device (A2DP) ────────────────────────────────► vsink.program ──►  (mixed)
Sendspin / Music Assistant ──────────────────────► vsink.key     ──► line-out

When Sendspin active: BT stream + UR23 source ducked to 20%
audio-hub-mqtt.py polls every 2s, adjusts volumes via wpctl
```

The line-out sink (`alsa_output.platform-fe00b840.mailbox.stereo-fallback`) is the bcm2835 headphone jack. Both vsink.program and vsink.key feed their monitor outputs into it simultaneously via pw-static-links.service.

---

## 2. OS Package Installation

Install all required packages in a single pass. This covers Bluetooth, PipeWire, and
supporting tools:

```bash
sudo apt update
sudo apt install -y \
  bluetooth bluez bluez-tools bluez-obexd rfkill \
  pipewire pipewire-audio pipewire-alsa pipewire-pulse pipewire-bin \
  wireplumber libspa-0.2-bluetooth \
  pulseaudio-utils \
  evdev python3-evdev \
  bt-agent \
  usbutils \
  curl wget git
```

> **`pulseaudio-utils`** provides `pactl`, which is required by the UR23 loopback service guard. On a fresh Trixie install this package may be missing even if `pipewire-pulse` is installed — verify with `which pactl` before proceeding.

Enable Bluetooth at boot:

```bash
sudo systemctl enable --now bluetooth
```

---

## 3. Project Software Installation

### 3.1 Sendspin CLI

Install the system-level service first (required by the installer), then immediately disable it — it will be replaced with a user-level service:

```bash
curl -fsSL https://raw.githubusercontent.com/Sendspin/sendspin-cli/refs/heads/main/scripts/systemd/install-systemd.sh | sudo bash

sudo systemctl stop sendspin.service
sudo systemctl disable sendspin.service
sudo rm /etc/systemd/system/sendspin.service
```

### 3.2 MQTT Python Environment

Create an isolated Python virtual environment for the MQTT client:

```bash
python3 -m venv ~/.local/share/audio-hub
source ~/.local/share/audio-hub/bin/activate
pip install paho-mqtt evdev
deactivate
```

### 3.3 Restore Project Files from Archive

If an archive from a working device is available, restore it now. This populates all config
files, scripts, and service units in a single step:

```bash
chmod +x audio-hub-archive.sh
./audio-hub-archive.sh --restore audio-hub-backup-YYYYMMDD-HHMM.tar.gz
```

The restore script will prompt separately for user files and system files, and print a
checklist of device-specific edits required after restore. If restoring to a new device,
complete those edits before proceeding to Section 5.

If starting from scratch (no archive), follow Sections 4 and 5 to create all files manually.

---

## 4. System Configuration

This section covers configuration that is identical across all devices.

### 4.1 Bluetooth — Main Config

```bash
sudo nano /etc/bluetooth/main.conf
```

```ini
[General]
Name=Living Room Media
Class=0x200414
DiscoverableTimeout=0
PairableTimeout=0

[Policy]
AutoEnable=true
```

```bash
sudo systemctl restart bluetooth
```

> **Note:** `Name` and `Alias` are device-specific — update them per device (Section 5.1).



### 4.2 Unblock Bluetooth & Verify Adapter

On a fresh Trixie install the Bluetooth adapter may be rfkill-blocked:

```bash
rfkill list
# If Bluetooth shows "Soft blocked: yes":
rfkill unblock bluetooth
```

Power on and make discoverable and set the Alias of the bluetooth adapter:

```bash
bluetoothctl
  system-alias "Living Room Media"
  power on
  discoverable on
  pairable on
  exit
```

Verify:

```bash
bluetoothctl show | grep -E "Powered|Discoverable|Pairable|PowerState"
```

Expected: `Powered: yes`, `PowerState: on`, `Discoverable: yes`, `Pairable: yes`.

> **Warning:** If `PowerState: off-blocked` is shown, `rfkill unblock bluetooth` must be run before attempting `power on`. `AutoEnable=true` handles power-on at boot but cannot override an rfkill block.

### 4.3 Enable User Lingering

Allows the user PipeWire session to start at boot without a login shell:

```bash
sudo loginctl enable-linger pi
```

### 4.4 Bluetooth Pairing Agent

Installs an auto-accept agent so devices can pair without manual confirmation:

```bash
sudo tee /etc/systemd/system/bt-agent.service > /dev/null << 'EOF'
[Unit]
Description=Bluetooth auto-accept pairing agent
After=bluetooth.service
Wants=bluetooth.service

[Service]
Type=simple
ExecStart=/usr/bin/bt-agent -c NoInputNoOutput
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bt-agent.service
```

> **Boot ordering note:** `bt-agent` must be running before pairing attempts. After a fresh install, restart `bluetooth.service` after WirePlumber has fully started to ensure A2DP endpoints are registered before the first pair:
> 
> ```bash
> sudo systemctl restart bluetooth
> ```

### 4.5 PipeWire — Virtual Sinks

Create two null sinks — one for Sendspin (`vsink.key`) and one for BT/TV audio
(`vsink.program`):

```bash
mkdir -p ~/.config/pipewire/pipewire.conf.d
nano ~/.config/pipewire/pipewire.conf.d/90-ducking-virtual-sinks.conf
```

```
context.objects = [
  {
    factory = adapter
    args = {
      factory.name    = support.null-audio-sink
      node.name       = vsink.program
      node.description = "Program (BT/TV)"
      media.class     = Audio/Sink
      audio.position  = [ FL FR ]
      monitor.channel-volumes = true
    }
  }
  {
    factory = adapter
    args = {
      factory.name    = support.null-audio-sink
      node.name       = vsink.key
      node.description = "Key (Sendspin)"
      media.class     = Audio/Sink
      audio.position  = [ FL FR ]
      monitor.channel-volumes = true
    }
  }
]
```

### 4.6 WirePlumber — Bluetooth A2DP Routing

Route all incoming Bluetooth A2DP streams to `vsink.program`:

```bash
mkdir -p ~/.config/wireplumber/wireplumber.conf.d
nano ~/.config/wireplumber/wireplumber.conf.d/50-bluetooth-a2dp-sink.conf
```

```
wireplumber.settings = {
  bluetooth.autoswitch-to-headset-profile = false
}

monitor.bluez.rules = [
  {
    matches = [{ "node.name" = "~bluez_input.*" }]
    actions = {
      update-props = {
        "target.object"  = "vsink.program"
        "node.autoconnect" = true
      }
    }
  }
]
```

> **Warning:** Do NOT add `wireplumber.settings = { default.configured.audio.sink = "vsink.key" }` or any default sink override. This breaks audio routing. Sendspin is directed to `vsink.key` via its `PIPEWIRE_NODE` environment variable.

### 4.7 WirePlumber — UR23 Optical Input

```bash
nano ~/.config/wireplumber/wireplumber.conf.d/51-ur23-route.conf
```

```
monitor.alsa.rules = [
  {
    matches = [{ "device.name" = "~alsa_card.usb-HiFimeDIY*" }]
    actions = {
      update-props = {
        api.acp.auto-profile  = false
        device.profile        = "input:iec958-stereo"
      }
    }
  }
  {
    matches = [{ "node.name" = "~alsa_input.usb-HiFimeDIY*" }]
    actions = {
      update-props = {
        node.description  = "TV Optical Input (UR23)"
        audio.format      = "S16LE"
        audio.rate        = 48000
        target.object     = "vsink.program"
        node.autoconnect  = true
      }
    }
  }
]
```

> The `target.object` property on Source nodes does not auto-create a loopback. `pw-ur23-loopback.service` (Section 4.11) handles the actual routing via `pw-loopback`.

### 4.8 Restart PipeWire & Verify Virtual Sinks

```bash
systemctl --user restart pipewire wireplumber pipewire-pulse
wpctl status | grep -E "vsink|Program|Key"
```

Both `vsink.program (Program (BT/TV))` and `vsink.key (Key (Sendspin))` should appear under Sinks.

> **Critical:** Never restart WirePlumber from within a service or script after the system is running. Doing so drops all virtual sink registrations and destroys the pw-link graph, requiring a full reboot to recover.

### 4.9 Sendspin Configuration

```bash
nano ~/.config/sendspin/settings-daemon.json
```

```json
{
  "name": "Living Room Media",
  "client_id": "living-room-media",
  "log_level": null,
  "listen_port": null,
  "player_volume": 25,
  "player_muted": false,
  "static_delay_ms": 0,
  "last_server_url": null,
  "audio_device": "2",
  "use_mpris": false
}
```

> **Note:** `name` and `client_id` are device-specific — update them per device (Section 5.1). The `audio_device` index is auto-detected at boot by `sendspin-detect-device.sh`.

### 4.10 Sendspin Auto-Detect Device Script

Detects the correct PipeWire device index at each boot, handling USB enumeration changes:

```bash
mkdir -p ~/.local/bin
nano ~/.local/bin/sendspin-detect-device.sh
```

```bash
#!/bin/bash
SETTINGS="$HOME/.config/sendspin/settings-daemon.json"
SENDSPIN_PY="/home/pi/.local/share/uv/tools/sendspin/bin/python"
SENDSPIN_BIN="/home/pi/.local/bin/sendspin"

idx=$($SENDSPIN_PY $SENDSPIN_BIN --list-audio-devices \
  | grep -i "pipewire" \
  | grep -oP "(?<=\[)\d+(?=\])" \
  | head -1)

if [ -n "$idx" ]; then
  python3 -c "
import json
with open('$SETTINGS') as f:
    d = json.load(f)
d['audio_device'] = str($idx)
with open('$SETTINGS', 'w') as f:
    json.dump(d, f, indent=2)
print('Set audio_device to', $idx)
"
else
  echo "Could not detect pipewire device index" >&2
  exit 1
fi
```

```bash
chmod +x ~/.local/bin/sendspin-detect-device.sh
```

### 4.11 Systemd User Services

Create all service unit files:

**`~/.config/systemd/user/pw-static-links.service`**

Creates persistent monitor→line-out connections at boot.

```bash
nano ~/.config/systemd/user/pw-static-links.service
```

```ini
[Unit]
Description=PipeWire static links and loopbacks
After=wireplumber.service pipewire.service pipewire-pulse.service
Wants=wireplumber.service pipewire.service pipewire-pulse.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c '\
  for i in $(seq 1 20); do \
    pw-link -i | grep -q "alsa_output.platform-fe00b840.mailbox.stereo-fallback:playback_FL" && break; \
    echo "Waiting for fallback sink ports... attempt $i"; \
    sleep 2; \
  done; \
  pw-link "vsink.program:monitor_FL" "alsa_output.platform-fe00b840.mailbox.stereo-fallback:playback_FL" || true; \
  pw-link "vsink.program:monitor_FR" "alsa_output.platform-fe00b840.mailbox.stereo-fallback:playback_FR" || true; \
  pw-link "vsink.key:monitor_FL" "alsa_output.platform-fe00b840.mailbox.stereo-fallback:playback_FL" || true; \
  pw-link "vsink.key:monitor_FR" "alsa_output.platform-fe00b840.mailbox.stereo-fallback:playback_FR" || true'

[Install]
WantedBy=default.target
```

> **Warning:** Do NOT add `After=pw-ur23-loopback.service` — it creates a circular dependency. The sink name `alsa_output.platform-fe00b840.mailbox.stereo-fallback` is specific to the bcm2835 audio hardware on Raspberry Pi 4B. Verify with: `wpctl status | grep fallback`

---

**`~/.local/bin/pw-vsink-watchdog.sh`**

Polls every 10 seconds and recreates any missing vsink→fallback links:

```bash
nano ~/.local/bin/pw-vsink-watchdog.sh
```

```bash
#!/bin/bash
FALLBACK="alsa_output.platform-fe00b840.mailbox.stereo-fallback"
while true; do
  for pair in \
    "vsink.program:monitor_FL ${FALLBACK}:playback_FL" \
    "vsink.program:monitor_FR ${FALLBACK}:playback_FR" \
    "vsink.key:monitor_FL ${FALLBACK}:playback_FL" \
    "vsink.key:monitor_FR ${FALLBACK}:playback_FR"; do
    SRC=$(echo $pair | cut -d" " -f1)
    DST=$(echo $pair | cut -d" " -f2)
    pw-link -l | grep -qF "$SRC" || pw-link "$SRC" "$DST" 2>/dev/null
  done
  sleep 10
done
```

```bash
chmod +x ~/.local/bin/pw-vsink-watchdog.sh
```

**`~/.config/systemd/user/pw-vsink-watchdog.service`**

```bash
nano ~/.config/systemd/user/pw-vsink-watchdog.service
```

```ini
[Unit]
Description=PipeWire vsink->fallback link watchdog
After=pw-static-links.service
Wants=pw-static-links.service

[Service]
Type=simple
ExecStart=/home/pi/.local/bin/pw-vsink-watchdog.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

> **Warning:** Do NOT add `After=wireplumber.service`. The watchdog only needs`pw-static-links` to have run first.

---

**`~/.config/systemd/user/pw-ur23-loopback.service`**

Bridges the UR23 capture node into `vsink.program`:

```bash
nano ~/.config/systemd/user/pw-ur23-loopback.service
```

```ini
[Unit]
Description=PipeWire loopback for UR23 TV optical input
After=wireplumber.service pipewire.service pipewire-pulse.service
Wants=wireplumber.service pipewire.service

[Service]
Type=simple
ExecStartPre=/bin/bash -c '\
  for i in $(seq 1 30); do \
    wpctl status | grep -q "UR23" && \
    wpctl status | grep -q "Program (BT/TV)" && break; \
    echo "Waiting for UR23 device and Program sink... attempt $i"; \
    sleep 2; \
  done'
ExecStart=/usr/bin/pw-loopback \
  -C "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo" \
  -P "vsink.program"
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

> The guard uses `wpctl status` to check for the UR23 USB device, not `pactl list sources`— the ALSA source only appears when the TV is on and sending optical signal. Without this guard, `pw-loopback` may connect to the fallback monitor instead, creating an audio feedback loop.

---

**`~/.config/systemd/user/sendspin.service`**

```bash
mkdir -p ~/.config/systemd/user
nano ~/.config/systemd/user/sendspin.service
```

```ini
[Unit]
Description=Sendspin Client
Wants=pipewire.service wireplumber.service pipewire-pulse.service pw-static-links.service
After=pipewire.service wireplumber.service pipewire-pulse.service pw-static-links.service

[Service]
Type=simple
WorkingDirectory=/home/pi/
ExecStartPre=/bin/sleep 10
ExecStartPre=/home/pi/.local/bin/sendspin-detect-device.sh
Environment=PYTHONUNBUFFERED=1
Environment=PIPEWIRE_ALSA_SINK=vsink.key
Environment=PIPEWIRE_NODE=vsink.key
ExecStart=/home/pi/.local/bin/sendspin daemon
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
```

---

**`~/.config/systemd/user/audio-hub-mqtt.service`**

```bash
nano ~/.config/systemd/user/audio-hub-mqtt.service
```

```ini
[Unit]
Description=Audio Hub MQTT Client
After=wireplumber.service pipewire.service pw-static-links.service network-online.target
Wants=wireplumber.service pipewire.service network-online.target

[Service]
Type=simple
ExecStart=/home/pi/.local/share/audio-hub/bin/python \
  /home/pi/.local/bin/audio-hub-mqtt.py \
  /home/pi/config.json
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
```

### 4.12 MQTT Configuration File

```bash
nano ~/config.json
```

```json
{
  "mqtt": {
    "host": "192.168.0.100",
    "port": 1883,
    "username": "your_mqtt_username",
    "password": "your_mqtt_password",
    "base_topic": "homeassistant"
  },
  "device": {
    "name": "Living Room Media",
    "id": "living_room_media",
    "model": "Raspberry Pi 4B"
  },
  "sources": {
    "bt": {
      "name": "Bluetooth",
      "icon": "mdi:bluetooth-audio",
      "wpctl_pattern": "bluez_input",
      "type": "stream"
    },
    "tv": {
      "name": "TV Optical",
      "icon": "mdi:television-speaker",
      "wpctl_pattern": "TV Optical Input (UR23)",
      "type": "source"
    },
    "sendspin": {
      "name": "Music Assistant",
      "icon": "mdi:music-circle",
      "wpctl_pattern": "Key (Sendspin)",
      "type": "sink",
      "readonly": true
    }
  },
  "audio": {
    "lineout_sink": "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
    "program_sink": "vsink.program",
    "sendspin_sink": "vsink.key"
  },
  "ducking": {
    "enabled": true,
    "duck_level": 0.2,
    "normal_level": 1.0,
    "poll_interval": 2
  },
  "ir": {
    "device": "/dev/input/by-id/usb-flirc.tv_flirc_90241C2150554354392E3120FF0D062F-if01-event-kbd",
    "volume_step": 0.05
  }
}
```

> **Warning:** Replace `host`, `username`, and `password` with your actual Mosquitto broker details. Keep this file secure — it contains credentials.
> 
> `device.name` and `device.id` are device-specific — update per device (Section 5.1).
> The `ir.device` path may differ — verify with `ls /dev/input/by-id/ | grep flirc`.

### 4.13 Deploy MQTT Script and Enable All Services

```bash
cp audio-hub-mqtt.py ~/.local/bin/audio-hub-mqtt.py
chmod +x ~/.local/bin/audio-hub-mqtt.py

systemctl --user daemon-reload
systemctl --user enable --now pw-static-links.service
systemctl --user enable --now pw-vsink-watchdog.service
systemctl --user enable --now pw-ur23-loopback.service
systemctl --user enable --now sendspin.service
systemctl --user enable --now audio-hub-mqtt.service
```

### 4.14 Add Sendspin to Music Assistant

In Music Assistant: **Settings → Players → Add Player → Sendspin**. The player named to match `client_id` in `settings-daemon.json` should appear automatically via mDNS discovery.

---

## 5. Device-Specific Configuration

After setting up a new device (or restoring from archive), update all identity and
device-specific values:

### 5.1 Hostname and Identity

```bash
# Set hostname
sudo hostnamectl set-hostname master-bedroom-media
sudo nano /etc/hosts   # update 127.0.1.1 entry to match new hostname

# Bluetooth display name
sudo nano /etc/bluetooth/main.conf
# Update: Name = Master Bedroom Media

# MQTT device identity
nano ~/config.json
# Update: device.name, device.id

# Sendspin player identity
nano ~/.config/sendspin/settings-daemon.json
# Update: name, client_id
```

### 5.2 TV Optical Output Settings

The UR23 requires PCM stereo from the TV optical output. Dolby Digital or DTS passthrough will not decode correctly.

**LG OLED (WebOS):**

| Setting           | Value   |
| ----------------- | ------- |
| Sound Out         | Optical |
| Digital Sound Out | PCM     |
| TV Speaker        | Off     |

**TCL Roku TV:**

| Setting                                  | Value  |
| ---------------------------------------- | ------ |
| Settings → Audio → Digital Output Format | Stereo |
| Settings → Audio → TV Speakers           | Off    |

> On TCL Roku TVs, PCM stereo is labelled **Stereo**, not **PCM**. The Auto setting may send Dolby Digital for some streaming apps regardless of this setting.

### 5.3 FLIRC IR Receiver (optional)

The FLIRC enumerates as a standard USB HID keyboard — no drivers or udev rules are needed on the Pi. The `pi` user must be in the `input` group:

```bash
sudo usermod -aG input pi
# Log out and back in, or reboot
```

Verify IR events are received:

```bash
evtest /dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd
```

Expected events: `KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, `KEY_MUTE`.

To reprogram button mappings, connect the FLIRC to the Nobara workstation (where the FLIRC GUI app and hidraw udev rules are configured) and use the FLIRC GUI.

### 5.4 UR23 on USB 3.0 Port (FLIRC co-installed)

If both a FLIRC and the UR23 are installed, place the UR23 on a **USB 3.0 (blue) port** and
the FLIRC on a USB 2.0 port. Placing both on USB 2.0 causes isochronous bandwidth contention on the shared Full Speed bus, resulting in audio distortion.

### 5.5 Persistent Journal (optional but recommended)

```bash
sudo mkdir -p /var/log/journal
sudo systemd-tmpfiles --create --prefix /var/log/journal
sudo tee /etc/systemd/journald.conf.d/user-journal.conf << 'EOF'
[Journal]
Storage=persistent
EOF
sudo systemctl restart systemd-journald
```

---

## 6. Platform Verification

Run these checks after a fresh install or reboot to confirm the system is healthy.

### 6.1 Service Status

All five services should show `active`:

```bash
systemctl --user status \
  sendspin.service \
  pw-static-links.service \
  pw-vsink-watchdog.service \
  pw-ur23-loopback.service \
  audio-hub-mqtt.service
```

`pw-static-links.service` shows `active (exited)` — this is correct for a oneshot service.

### 6.2 Audio Routing Links

```bash
pw-link -l | grep -E "vsink|fallback"
```

Expected output — exactly four links, no feedback loops:

```
vsink.program:monitor_FL  |->  alsa_output...fallback:playback_FL
vsink.program:monitor_FR  |->  alsa_output...fallback:playback_FR
vsink.key:monitor_FL      |->  alsa_output...fallback:playback_FL
vsink.key:monitor_FR      |->  alsa_output...fallback:playback_FR
```

### 6.3 Bluetooth

```bash
bluetoothctl show | grep -E "Powered|Discoverable|UUID"
```

`UUID: Audio Sink (0000110b...)` must appear — this confirms WirePlumber has registered the A2DP endpoint with bluetoothd. If it is absent, restart bluetooth after WirePlumber is running:

```bash
sudo systemctl restart bluetooth
sleep 5
bluetoothctl show | grep "Audio Sink"
```

### 6.4 UR23 Optical Input

```bash
wpctl status | grep -i ur23
cat /proc/asound/card1/stream0 | grep -E "Status|Momentary"
```

When the TV is on and sending PCM audio: `Status: Running` and
`Momentary freq = 48000 Hz (0x30.0000)`.

### 6.5 Stream Routing

```bash
wpctl status | grep -A15 "Streams"
```

- Bluetooth playing: `bluez_input` → `Program (BT/TV)`
- Sendspin playing: `PipeWire ALSA [python3.x]` → `Key (Sendspin)`

### 6.6 Ducking Test

```bash
watch -n1 "echo BT:  \$(wpctl get-volume \$(wpctl status | grep bluez_input \
  | grep -oP '\b[0-9]+\b' | head -1) 2>/dev/null); \
  echo TV:  \$(wpctl get-volume \$(wpctl status | grep 'TV Optical Input' \
  | grep -oP '\b[0-9]+\b' | head -1) 2>/dev/null)"
```

Trigger Sendspin playback from Music Assistant. Both BT and TV volumes should drop to `0.20` and return to `1.00` when Sendspin stops.

### 6.7 IR Remote (if FLIRC installed)

```bash
evtest /dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd
```

Press volume up/down and mute on the remote. Expected: `KEY_VOLUMEUP`, `KEY_VOLUMEDOWN`, `KEY_MUTE` events.

### 6.8 Home Assistant Entities

Check HA **Settings → Devices & Services → Devices** for the device entry. Expected entities:

| Entity                            | Type          | Description                          |
| --------------------------------- | ------------- | ------------------------------------ |
| `number.*_bluetooth_volume`       | number        | BT volume slider (0–100%)            |
| `number.*_tv_optical_volume`      | number        | TV volume slider (0–100%)            |
| `sensor.*_music_assistant_volume` | sensor        | Sendspin level (read-only)           |
| `sensor.*_bluetooth_state`        | sensor        | BT state: playing / idle / off       |
| `sensor.*_music_assistant_state`  | sensor        | Sendspin state: playing / idle / off |
| `switch.*_lineout_mute`           | switch        | Mute entire line-out                 |
| `switch.*_ducking`                | switch        | Enable/disable auto-ducking          |
| `binary_sensor.*_sendspin_status` | binary_sensor | Sendspin service running             |
| `binary_sensor.*_pipewire_status` | binary_sensor | PipeWire service running             |

---

## 7. Troubleshooting

### 7.1 Sendspin — "Specified audio device not found"

The PipeWire device index shifted due to USB enumeration order. The auto-detect script handles this automatically, but to fix manually:

```bash
/home/pi/.local/share/uv/tools/sendspin/bin/python \
  /home/pi/.local/bin/sendspin --list-audio-devices
```

Note the index of the `pipewire` entry and update `~/.config/sendspin/settings-daemon.json` manually (`audio_device` field).

### 7.2 No Audio from TV

Verify the TV optical output is set to PCM/Stereo (not Auto or Dolby). Check the UR23 node:

```bash
wpctl status | grep -i ur23
systemctl --user status pw-ur23-loopback.service
systemctl --user restart pw-ur23-loopback.service
```

### 7.3 UR23 Audio Distortion

If audio from the TV is distorted while Bluetooth and Sendspin are clean:

1. Check `cat /proc/asound/card1/stream0` — `Momentary freq` should show `48000 Hz`. If absent, the UR23 has no valid S/PDIF lock (TV off, wrong format, or faulty optical connection).
2. Check the UR23 is on a **USB 3.0 port**. If a FLIRC or other USB HID device is on the same USB 2.0 bus, bandwidth contention causes distortion.
3. Verify the TV Digital Output Format is set to **Stereo** (TCL) or **PCM** (LG).

### 7.4 Bluetooth — Cannot Connect (br-connection-profile-unavailable)

`Audio Sink` UUID is missing from `bluetoothctl show`, meaning WirePlumber has not registered the A2DP endpoint with bluetoothd. Restart bluetooth after WirePlumber is fully running:

```bash
sudo systemctl restart bluetooth
sleep 5
bluetoothctl show | grep "Audio Sink"
```

If `Audio Sink` still does not appear:

```bash
# Verify libspa-0.2-bluetooth is installed
dpkg -l | grep libspa-0.2-bluetooth

# Verify pactl is available (required by loopback guard)
which pactl

# Verify rfkill is not blocking
rfkill list
```

### 7.5 No Audio from Bluetooth

Confirm the device is paired and connected:

```bash
bluetoothctl info
# Look for "Connected: yes"
wpctl status | grep -A4 "bluez_input"
# Stream should show routing to "Program (BT/TV)"
```

### 7.6 Audio Feedback Loop (Extremely Loud on Boot)

`pw-loopback` started before the UR23 device was ready and connected to the fallback monitor instead. Check the loopback service log:

```bash
journalctl --user -u pw-ur23-loopback.service -n 30
# Should show "Waiting for UR23 device..." lines before pw-loopback starts
```

To recover at runtime:

```bash
# Find rogue link IDs (fallback monitor -> vsink playback)
pw-link -l -I | grep -A2 "fallback.*monitor\|monitor.*fallback"

# Destroy rogue links by ID (replace with actual IDs)
pw-cli destroy <ID1>
pw-cli destroy <ID2>

# Restart loopback cleanly
systemctl --user restart pw-ur23-loopback.service
pw-link -l | grep -E "vsink|fallback"
```

### 7.7 Monitor→Fallback Links Missing After Reboot

```bash
systemctl --user restart pw-static-links.service
pw-link -l | grep -E "vsink|fallback"
```

The watchdog (`pw-vsink-watchdog.service`) should recreate links within 10 seconds
automatically. If it doesn't, check the watchdog is running:

```bash
systemctl --user status pw-vsink-watchdog.service
```

### 7.8 MQTT Service Not Connecting

```bash
journalctl --user -u audio-hub-mqtt.service -f
```

Check for `MQTT connect failed` errors. Verify `host`, `port`, `username`, and `password` in `~/config.json`. Confirm `paho-mqtt` 2.x is installed:

```bash
~/.local/share/audio-hub/bin/pip show paho-mqtt
```

### 7.9 HA Entities Not Appearing

The MQTT script publishes discovery payloads on startup. Restart the service and check HA:

```bash
systemctl --user restart audio-hub-mqtt.service
journalctl --user -u audio-hub-mqtt.service -n 20
```

If stale entities persist in HA: **Settings → Devices & Services → MQTT → Clear retained messages**, or delete specific retained topics via MQTT Explorer.

---

## 8. Appendix — File Reference

### 8.1 User Files (`~/` and `~/.config/`)

| File                                                                   | Purpose                                              |
| ---------------------------------------------------------------------- | ---------------------------------------------------- |
| `~/.config/pipewire/pipewire.conf.d/90-ducking-virtual-sinks.conf`     | Defines vsink.program and vsink.key null sinks       |
| `~/.config/wireplumber/wireplumber.conf.d/50-bluetooth-a2dp-sink.conf` | Routes BT A2DP streams to vsink.program              |
| `~/.config/wireplumber/wireplumber.conf.d/51-ur23-route.conf`          | UR23 profile, node description, format, and target   |
| `~/.config/systemd/user/pw-static-links.service`                       | Creates monitor→line-out links at boot               |
| `~/.config/systemd/user/pw-vsink-watchdog.service`                     | Watchdog: recreates vsink→fallback links every 10s   |
| `~/.config/systemd/user/pw-ur23-loopback.service`                      | Bridges UR23 capture into vsink.program              |
| `~/.config/systemd/user/sendspin.service`                              | Runs Sendspin Music Assistant client                 |
| `~/.config/systemd/user/audio-hub-mqtt.service`                        | Runs MQTT client, ducking, and HA entity publishing  |
| `~/.config/sendspin/settings-daemon.json`                              | Sendspin client configuration                        |
| `~/.local/bin/pw-vsink-watchdog.sh`                                    | Watchdog polling script                              |
| `~/.local/bin/sendspin-detect-device.sh`                               | Auto-detects PipeWire device index at boot           |
| `~/.local/bin/audio-hub-mqtt.py`                                       | MQTT client — ducking, HA discovery, volume control  |
| `~/.local/share/audio-hub/`                                            | Python venv containing paho-mqtt and evdev           |
| `~/config.json`                                                        | MQTT credentials, source definitions, ducking config |

### 8.2 System Files (`/etc/` and `/usr/`)

| File                                   | Purpose                                               |
| -------------------------------------- | ----------------------------------------------------- |
| `/etc/bluetooth/main.conf`             | Bluetooth adapter config (AutoEnable, name, timeouts) |
| `/etc/systemd/system/bt-agent.service` | Auto-accept Bluetooth pairing agent                   |

### 8.3 Backup and Restore

Use `audio-hub-archive.sh` to back up and restore all project files:

```bash
# Backup
audio-hub-archive.sh --backup [output-dir]

# Restore
audio-hub-archive.sh --restore audio-hub-backup-YYYYMMDD-HHMM.tar.gz
```

The archive contains two inner tarballs — user files and system files — which can be
restored independently. After restoring to a new device, follow Section 5 to update
all device-specific values before rebooting.

### 8.4 PipeWire Fragility Reference

| Issue                                              | Cause                                               | Mitigation                                                                                                            |
| -------------------------------------------------- | --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------- |
| pw-link connections lost after WirePlumber restart | Links exist only in memory; restart wipes the graph | Never restart WirePlumber manually. pw-vsink-watchdog.service recreates links every 10s                               |
| UR23 loopback connects to wrong node at boot       | pw-loopback starts before UR23 ready                | ExecStartPre guard uses `wpctl status \| grep UR23` (not pactl — ALSA source absent when TV is off). 30 retries / 60s |
| UR23 audio distortion with FLIRC present           | USB bandwidth contention on shared Full Speed bus   | Place UR23 on USB 3.0 port, FLIRC on USB 2.0                                                                          |
| Sendspin device index shifts between reboots       | USB enumeration order is not stable                 | sendspin-detect-device.sh auto-detects correct index on every boot                                                    |
| Ducking null sink has no audible effect            | Null sink volume does not affect monitor output     | Ducking targets input stream/source nodes directly, not vsink.program                                                 |

---

*Raspberry Pi Audio Hub · Raspberry Pi 4B · PipeWire + WirePlumber + HA MQTT*
