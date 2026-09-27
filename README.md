# Audio Hub

A self-contained audio hub for Raspberry Pi 4B: mixes three sources (TV optical, Bluetooth A2DP, Music Assistant) to a single analog output, with priority ducking, IR remote control, and Home Assistant integration.

> **Status:** deployed on hardware (living-room-media, 2026-09-26). All three sources verified end-to-end on hardware: TV optical, Bluetooth A2DP, and Music Assistant/Sendspin all play through the hub; ducking, MQTT entities, IR remote, cold boot, broker-outage retry, and PipeWire-restart recovery all pass. Two environment-specific Bluetooth findings are documented below — they apply to any new unit.

## Design Principles

1. **Declarative-first** — all audio routing via PipeWire/WirePlumber config; no pw-link scripts, no watchdogs. PipeWire itself reconnects declarative loopbacks after failures or reboots.
2. **Stable names only** — routing attaches to `node.name`s (`bus.*`, `duct.*`), never numeric IDs.
3. **Single source of truth** — `/etc/audiohub/unit.env` drives hubd, Sendspin identity, and Bluetooth naming.
4. **Minimum services** — only `hubd` + `sendspin` (user units) and `bt-agent` + `bluetooth-setup` + `wifi-powersave-off` (system units).
5. **Reproducible** — git repo + idempotent installer.

## Architecture

```
TV (UR23) ──loopback(conf)──▶ [bus.tv]  ──duct.tv──▶
Bluetooth A2DP ──WP rule───▶ [bus.bt]  ──duct.bt──▶  [Hardware Sink] ──▶ Speakers
Sendspin ──PIPEWIRE_NODE──▶ [bus.music] ──duct.music──▶
```

- **Per-source volume** = volume of the corresponding bus sink (`monitor.channel-volumes = true` makes the ducts carry it)
- **Ducking** = multiplier on the `duct.tv` / `duct.bt` playback streams (separate from source volumes)
- **Master volume/mute** = hardware sink
- **Clock locked to 48 kHz** — matches TV S/PDIF output

hubd (single daemon) does ducking (poll, 0.5 s), MQTT/Home Assistant entities, and FLIRC IR volume/mute.

## Install

```bash
# On the Pi, as root:
sudo ./install.sh

# Edit the unit config (MQTT credentials + device identity):
sudo nano /etc/audiohub/unit.env

# Reboot to load all configs
sudo reboot
```

The installer runs everything user-level as the `pi` user (override with `AUDIOHUB_USER=<name> audiohub`) and enables linger so the stack starts at boot with no login. It also installs Sendspin (`uv tool install sendspin` into the user's home).

## Configuration

`/etc/audiohub/unit.env` — see `unit.env.example` for all fields. Device identity, MQTT, ducking, and IR are all set there.

## Services

| Unit | Scope | Purpose |
|------|-------|---------|
| `pipewire` / `wireplumber` / `pipewire-pulse` | user | stock audio stack |
| `hubd.service` | user | ducking + MQTT + IR daemon |
| `sendspin.service` | user | Music Assistant client (routed to `bus.music`) |
| `bt-agent.service` | system | auto-accept Bluetooth pairing |
| `bluetooth-setup.service` | system | rfkill unblock + discoverable/pairable at boot |
| `wifi-powersave-off.service` | system | WiFi power save off |

## Directory Structure

```
audio-hub/
├── install.sh              # Idempotent installer (run as root)
├── packages.txt            # apt packages (comments allowed)
├── unit.env.example        # Per-unit config template -> /etc/audiohub/unit.env
├── config/
│   ├── pipewire.conf.d/    # clock, virtual buses, TV loopback, ducts
│   ├── wireplumber.conf.d/ # UR23 rules, BT A2DP routing, Sendspin routing
│   └── bluetooth/          # /etc/bluetooth/main.conf
├── systemd/
│   ├── user/               # hubd.service, sendspin.service
│   └── system/             # bt-agent, bluetooth-setup, wifi-powersave-off
├── hubd/                   # Hub controller daemon (ducking + MQTT + IR)
└── scripts/
    ├── validate.sh         # Post-boot validation (run as the hub user)
    ├── bluetooth-setup.sh  # Boot adapter bring-up (installed to /usr/local/sbin)
    └── sendspin-detect-device.sh  # settings-daemon.json sync + device index detect
```

## Verification

After a reboot, as the hub user:

```bash
bash scripts/validate.sh
systemctl --user status hubd.service sendspin.service
journalctl --user -u hubd.service -f
```

### Verified on hardware (living-room-media)

- Cold boot with TV off: services up, buses + ducts linked, no rogue links, hubd connected to MQTT, discovery published, FLIRC opened.
- Ducking: tone into `bus.music` → `duct.tv`/`duct.bt` drop to 0.20, `duct.music` untouched, restore after hold-off.
- MQTT: all commands (per-source/master volume, mute, ducking switch) applied and state published.
- Resilience: PipeWire restart self-heals (stale Pulse connections retry transparently, ducking engine reconnects); broker unreachable → 5 s retries, no crash loop; sendspin device index re-detected per boot.

### Real-world findings (fixed during bring-up)

- **IR remote did nothing audible** — hubd logged keypresses but `IRHandler.set_loop()` was never called, so dispatch was dropped. Fixed; keypresses now adjust master volume.
- **TV: no audio** — the UR23 node name flips between `...analog-stereo` (signal present) and `...stereo-fallback` (probing). `40-loopback-tv.conf` now loads one loopback per candidate name; exactly one can ever link.
- **Bluetooth: phone pairs but gets no audio** — three stacked causes, each fixed:
  1. bluetoothd segfaults occasionally; bt-agent must re-register when it does (`PartOf=bluetooth.service` + `Restart=always`), or A2DP authorization is denied.
  2. **Do NOT add a `wireplumber.settings` block to the Bluetooth fragment.** In this environment (WP 0.5.8 + bluez 5.82) it stops the A2DP endpoint from registering — the adapter advertises no Audio Sink UUID, so phones connect, find no audio service, and disconnect (the v1 howto's `autoswitch-to-headset-profile` setting is the offender; it is deliberately omitted).
  3. First BT playback after connecting can be digital silence: A2DP absolute volume syncs from the phone's media volume. Turning up the phone fixes it.
- Kernel log noise (`hci0: ACL packet for unknown connection handle`, `Unexpected continuation frame`) appears on healthy units during connection setup and is benign for this firmware generation.

### Remaining real-world checks

- **Ducking under real load**: play two sources at once (e.g. phone + Sendspin) and confirm the non-music source drops to ~20% and restores.
- **Cold boot with TV on**: confirm TV audio flows immediately after power-on.

## Hardware

- Raspberry Pi 4B (Argon40 One case)
- Hifime UR23 USB S/PDIF receiver — **USB 3.0 port** (the FLIRC shares the USB 2.0 bus otherwise; isochronous contention distorts audio)
- FLIRC USB IR receiver
- TV optical out set to **PCM / Stereo** (not Auto/Dolby)

## Requirements

- Raspberry Pi OS 64-bit (Debian Trixie)
- PipeWire ≥ 1.4.2, WirePlumber ≥ 0.5.8, BlueZ ≥ 5.82
- Python 3.11+ with system packages: `python3-paho-mqtt`, `python3-pulsectl`, `python3-evdev`

See `rpi-audio-hub-howto.md` for the original (v1) setup guide — kept as a reference for device-specific facts (UR23 behavior, TV settings, USB port placement).
