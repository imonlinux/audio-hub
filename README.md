# Audio Hub

A self-contained audio hub for Raspberry Pi 4B: mixes three sources (TV optical, Bluetooth A2DP, Music Assistant) to a single analog output, with priority ducking, IR remote control, and Home Assistant integration.

> **Status:** deployed on hardware (living-room-media, 2026-09-26). All three sources verified end-to-end on hardware: TV optical, Bluetooth A2DP, and Music Assistant/Sendspin all play through the hub; ducking, MQTT entities, IR remote, cold boot, broker-outage retry, and PipeWire-restart recovery all pass. Two environment-specific Bluetooth findings are documented below — they apply to any new unit.

## Design Principles

1. **Declarative-first** — all audio routing via PipeWire/WirePlumber config; no pw-link scripts, no watchdogs. PipeWire itself reconnects declarative loopbacks after failures or reboots.
2. **Stable names only** — routing attaches to `node.name`s (`bus.*`, `duct.*`), never numeric IDs.
3. **Single source of truth** — `/etc/audiohub/unit.env` drives hubd, Sendspin identity, and Bluetooth naming.
4. **Minimum services** — only `hubd` + `sendspin` (user units) and `bt-agent` + `bluetooth-setup` + `wifi-powersave-off` (system units).
5. **Reproducible** — checksum-verified tagged releases, idempotent installer, weekly self-updates.

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

The **Output Device** and **TV Source** Home Assistant select entities route the graph at runtime (no config edits, no restarts): sources and the output are picked from what is actually plugged in, keyed by stable node-name prefixes, persisted in `~/.config/audiohub/selection.json`, and reconciled by hubd every 2 s. Bluetooth and Sendspin stay name-routed by their own connection lifecycle and are never selectable. See `docs/device-selection-spec.md`.

## Deploy a new unit

Everything except the physical work (flashing, cabling) is one command on the Pi.

1. **Flash the SD card** with Raspberry Pi Imager: **Raspberry Pi OS Lite (64-bit)**, and in the OS customization set hostname, user, SSH key, and WiFi. Any username works — the installer picks the hub user automatically (`pi` if present, else the first regular user).
2. **Boot the Pi, SSH in**, and run the bootstrap (inspect it first if you like — that's the point of the two steps):

   ```bash
   curl -fsSL https://raw.githubusercontent.com/imonlinux/audio-hub/main/bootstrap.sh -o bootstrap.sh
   sudo bash bootstrap.sh
   ```

   The bootstrap resolves the current **stable** release, verifies its sha256 checksum against the release asset, installs the tree to `/opt/audio-hub`, and runs the installer. Overrides: `sudo AUDIOHUB_USER=<name> bash bootstrap.sh` (hub user) or `sudo AUDIOHUB_RELEASE=v1.2.3 bash bootstrap.sh` (pin a release).

3. **Edit the unit config** — MQTT credentials are per-unit; identity is pre-seeded from the hostname:

   ```bash
   sudo nano /etc/audiohub/unit.env
   ```

4. **Reboot**, then verify as the hub user:

   ```bash
   bash /opt/audio-hub/scripts/validate.sh
   ```

The installer is idempotent and preserves `/etc/audiohub/unit.env`, so re-running the bootstrap is always safe.

**Migrating an existing git-clone unit** (the two pre-fleet deployments): `git pull` once to get this tooling, run `sudo ./install.sh`, then run `sudo bash bootstrap.sh` — the tree moves to `/opt/audio-hub` and the unit switches to release updates. The repo copy in the home directory can be removed afterwards.

## Updating

Each unit runs `audiohub-update` weekly (Sunday 04:30 + up to an hour of jitter, catch-up after downtime). Updates are release-tarball based and checksum-verified; after installing, the updater restarts PipeWire and the hub services — no reboot needed (set `AUDIOHUB_UPDATE_REBOOT=true` in `unit.env` to reboot instead).

```bash
sudo audiohub-update --check   # resolve the channel, compare, change nothing
sudo audiohub-update           # apply now
sudo audiohub-update --force   # re-apply even if current (repair)
```

Channels and per-unit settings live in `unit.env`:

| Setting | Default | Meaning |
|---|---|---|
| `AUDIOHUB_AUTO_UPDATE` | `false` | opt a unit in to scheduled self-updates; manual `sudo audiohub-update` always works |
| `AUDIOHUB_RELEASE_CHANNEL` | `stable` | `stable` = promoted releases; `canary` = includes pre-releases |
| `AUDIOHUB_RELEASE` | unset | Pin an exact tag — overrides the channel, and doubles as rollback |
| `AUDIOHUB_UPDATE_REBOOT` | `false` | Reboot after a successful update instead of restarting services |

**Rollback**: set `AUDIOHUB_RELEASE=v1.2.2` in `unit.env` and run `sudo audiohub-update`. Scheduled updates are forward-only — a channel resolving to something older than the deployed version is skipped.

## Cutting a release (maintainer)

1. Tag and push: `git tag v1.2.3 && git push origin v1.2.3`. CI attaches `sha256sums.txt` (of the exact archives devices download) and publishes the release as a **pre-release** — that is the **canary** channel.
2. Point a soak unit at it: set `AUDIOHUB_RELEASE_CHANNEL=canary` there (the maintainer's own unit(s)); it updates at the next weekly check, or run `sudo audiohub-update` directly.
3. After soak, promote: edit the release on GitHub and untick **Set as a pre-release**. It is now the **stable** release; all stable units pick it up within a week.
4. `sendspin` is version-pinned in `unit.env.example` (`AUDIOHUB_SENDSPIN_VERSION`) — bump the pin only after re-verifying the ducking hooks on hardware.

## Configuration

`/etc/audiohub/unit.env` — see `unit.env.example` for all fields. Device identity, MQTT, ducking, IR, and release-update behavior are all set there. One key deserves a note: setting `AUDIOHUB_WIFI_CONNECTION=<nm-connection-name>` opts a WiFi unit into reliability tuning (unlimited autoconnect/auth retries, connection-level power-save off), applied by the installer — set it and re-run the installer to apply. Leave unset on wired units.

## Services

| Unit | Scope | Purpose |
|------|-------|---------|
| `pipewire` / `wireplumber` / `pipewire-pulse` | user | stock audio stack |
| `hubd.service` | user | ducking + MQTT + IR daemon |
| `sendspin.service` | user | Music Assistant client (routed to `bus.music`) |
| `bt-agent.service` | system | auto-accept Bluetooth pairing |
| `bluetooth-setup.service` | system | rfkill unblock + discoverable/pairable at boot |
| `wifi-powersave-off.service` | system | WiFi power save off |
| `audiohub-update.timer` → `.service` | system | weekly checksum-verified release check |

## Directory Structure

```
audio-hub/
├── bootstrap.sh            # One-command provisioning (curl | sudo bash)
├── install.sh              # Idempotent installer (run as root)
├── packages.txt            # apt packages (comments allowed)
├── unit.env.example        # Per-unit config template -> /etc/audiohub/unit.env
├── config/
│   ├── pipewire.conf.d/    # clock, virtual buses, TV loopback, ducts
│   ├── wireplumber.conf.d/ # UR23 rules, BT A2DP routing, Sendspin routing
│   └── bluetooth/          # /etc/bluetooth/main.conf
├── systemd/
│   ├── user/               # hubd.service, sendspin.service
│   └── system/             # bt-agent, bluetooth-setup, wifi-powersave-off,
│                           #   audiohub-update.{service,timer}
├── .github/workflows/      # release.yml: tag -> pre-release + sha256sums
├── hubd/                   # Hub controller daemon (ducking + MQTT + IR)
└── scripts/
    ├── validate.sh         # Post-boot validation (run as the hub user)
    ├── audiohub-update     # Release updater (installed to /usr/local/sbin)
    ├── bluetooth-setup.sh  # Boot adapter bring-up (installed to /usr/local/sbin)
    ├── sendspin-detect-device.sh  # settings-daemon.json identity sync
    └── audiohub-music-hook # Sendspin start/stop hook (ducking flag)
```

Deployed units keep the tree at `/opt/audio-hub` (root-owned, swapped atomically by the updater); development checkouts run the installer straight from the repo.

## Verification

After a reboot, as the hub user:

```bash
bash /opt/audio-hub/scripts/validate.sh   # (repo path on dev checkouts)
systemctl --user status hubd.service sendspin.service
journalctl --user -u hubd.service -f
```

`validate.sh` also reports the deployed version (`/etc/audiohub/version`) and the state of the release-update timer.

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
See `docs/qa-2026-09-26-disposition.md` for the disposition of the external QA review: every finding, the evidence (including measurements and logs) behind accepted and rejected verdicts, and the reproducible verification commands.
