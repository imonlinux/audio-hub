# Audio Hub

> **⚠️ WORK IN PROGRESS — NOT IN A WORKING STATE**
>
> This project is under active development. The core audio routing configuration has been created but is **untested**. Do not use this in production yet.
>
> See the [Status](#status) section below for current progress.

**Fresh implementation** of the Audio Hub for Raspberry Pi 4B.

A self-contained audio hub that mixes three sources (TV optical, Bluetooth A2DP, Music Assistant) to a single analog output, with priority ducking, IR remote control, and Home Assistant integration.

## Design Principles

1. **Declarative-first** — All audio routing via PipeWire/WirePlumber config, no pw-link scripts
2. **Stable names only** — All routing by node.name, never numeric IDs
3. **Event-driven** — Ducking via pulsectl events, no polling
4. **Minimum services** — Only sendspin + hubd as custom services
5. **Reproducible** — Git repo + idempotent installer + per-unit config

## Architecture

```
TV (UR23) ──loopback──▶ [bus.tv]  ──duct.tv──▶
Bluetooth A2DP        ──WP rule──▶ [bus.bt]  ──duct.bt──▶  [Hardware Sink] ──▶ Speakers
Sendspin               ──WP rule──▶ [bus.music] ──duct.music──▶
```

- **Per-source volume** = volume of the corresponding bus sink
- **Ducking** = multiplier on duct.tv and duct.bt streams (separate from source volumes)
- **Master volume/mute** = hardware sink volume/mute
- **Clock locked to 48 kHz** — matches TV S/PDIF output

## Quick Start

```bash
# Clone repo
git clone <repo> /tmp/audio-hub
cd /tmp/audio-hub

# Run installer (as root for full install)
sudo ./install.sh

# Configure unit
sudo nano /etc/audiohub/unit.env

# Reboot to load all configs
sudo reboot
```

## Directory Structure

```
audio-hub/
├── install.sh              # Idempotent installer
├── packages.txt            # Pinned package versions
├── unit.env.example         # Per-unit config template
├── README.md               # This file
├── config/
│   ├── pipewire.conf.d/    # PipeWire drop-in configs
│   ├── wireplumber.conf.d/ # WirePlumber rules
│   ├── bluetooth/          # BlueZ configuration
│   └── network/            # NetworkManager settings
├── systemd/
│   ├── user/               # User service definitions
│   └── system/             # System service definitions
├── hubd/                   # Hub controller daemon
│   ├── __init__.py
│   └── main.py             # Main daemon (ducking + MQTT + IR)
└── scripts/                # Validation and testing scripts
```

## Configuration

Edit `/etc/audiohub/unit.env` to configure:

```bash
# Device identity
AUDIOHUB_HOSTNAME=master-bedroom-media
AUDIOHUB_DEVICE_ID=master_bedroom_media
AUDIOHUB_DEVICE_NAME=Master Bedroom Media

# MQTT
AUDIOHUB_MQTT_HOST=192.168.0.100
AUDIOHUB_MQTT_PORT=1883
AUDIOHUB_MQTT_USERNAME=mqtt
AUDIOHUB_MQTT_PASSWORD=your_password

# Ducking
AUDIOHUB_DUCKING_ENABLED=true
AUDIOHUB_DUCK_LEVEL=0.20

# IR remote
AUDIOHUB_IR_DEVICE=/dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd
AUDIOHUB_IR_VOLUME_STEP=0.03
```

## Services

### User Services (run as pi or audiohub)

- `pipewire.service` — Core audio server
- `wireplumber.service` — Session manager
- `pipewire-pulse.service` — PulseAudio compatibility
- `sendspin.service` — Music Assistant client
- `hubd.service` — Hub controller daemon

### System Services

- `wifi-powersave-off.service` — Disable WiFi power save

## Status

| Phase | Status | Notes |
|-------|--------|-------|
| Phase 0 | ✅ | Provisioning repo structure |
| Phase 1 | 🔄 | Core audio graph (config created, untested) |
| Phase 2 | ⏳ | Bluetooth A2DP sink |
| Phase 3 | ⏳ | Sendspin + hubd daemon |
| Phase 4 | ⏳ | Network resilience |
| Phase 5 | ⏳ | Cold-boot hardening |
| Phase 6 | ⏳ | Validation matrix |

## Hardware

- Raspberry Pi 4B
- Hifime UR23 USB S/PDIF Receiver
- FLIRC USB Infrared Receiver
- Argon40 One v1 case

## Requirements

- Raspberry Pi OS 64-bit (Debian Trixie)
- PipeWire 1.4.2 + WirePlumber 0.5.8
- Python 3.13 with: paho-mqtt, pulsectl, evdev

## License

Custom hardware/audio hub deployment.
