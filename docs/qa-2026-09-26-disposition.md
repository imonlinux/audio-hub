# QA Review Disposition — 2026-09-26

Disposition of the 13-issue QA review of the audio-hub stack, with the
real-world test results from two deployed units (living-room-media,
master-bedroom-media) and the evidence trail behind every verdict.

**Scope reviewed:** commit `1237617` and earlier (the stack as first pushed).
**Result:** 10 findings accepted and implemented (several with corrections),
1 rejected as specified (invented property + claim contradicted on hardware),
2 partially rejected (wrong sub-claims, right instinct).
**Post-fix validation:** `scripts/validate.sh` 21/21 on both units; all
sources, ducking, IR, and Home Assistant control verified by the user on
hardware.

---

## 1. Summary

| # | Severity | Finding (abridged) | Verdict | Action |
|---|----------|--------------------|---------|--------|
| 1 | High | Duck never releases on pause | **Accepted** | Sendspin start/stop hooks drive a flag file; hubd ducking is flag-primary. Pause verified live on MA interface. |
| 2 | High | MA volume slider controls master volume | **Accepted** | `use_hardware_volume: false` in provisioning. Verified: flag exists in sendspin 7.5. |
| 3 | High | TV loopbacks destroyed when target missing; add `node.linger` | **Rejected as specified** | `node.linger` does not exist in PipeWire; "WP destroys the stream" contradicted on hardware. TV off/on watch retained as a test (in progress). |
| 4 | Medium | DUCK_LEVEL=0.20 is −42 dB on the cubic scale | **Accepted** | hubd converts perceptual → cubic (`level ** (1/3)`). This also explained master-bedroom's mysterious empirical 0.58. |
| 5 | Medium | No guard against a second audio stack | **Partially accepted** | Hardening implemented (linger disabled, `audio-hub-ducts` cleanup, validate check). The "today's root cause for Bluetooth" attribution is contradicted by evidence — see §3.5. |
| 6 | Medium | Device chosen by index again; use name `"pipewire"` | **Accepted** | Fixed name verified live ("Using audio device: pipewire"); index detection removed. |
| 7 | Medium | UR23 wins graph clock with TV off | **Accepted, with a correction** | Rule added — but the QA's property name `node.priority.driver` is silently ignored; the working key is `priority.driver`. Verified 2009 → 50. |
| 8 | Low | Duplicate `monitor.alsa.rules` arrays in one file | **Accepted** | Merged into one array. |
| 9 | Low | HA misses external volume changes | **Accepted** | 2-second change-driven state sync in hubd. |
| 10 | Low | TV Playing sensor reads node existence | **Accepted and extended** | Pulse state was also insufficient (loopback keeps the source RUNNING); sensor now reads ALSA signal lock from `/proc`. |
| 11 | Low | validate.sh UR23 check broken; Audio Sink only warns | **Partially accepted** | "Never finds the node" was factually wrong (check passed on living-room); refactored to `pactl` anyway, and missing Audio Sink UUID is now a hard failure. |
| 12 | Low | `auth-retries` property name; missing WiFi tooling | **Accepted** | Full property name used. WiFi logger/watchdog deferred (not core stack). |
| 13 | Low | bluetooth-setup waits for "Powered" before powering on | **Accepted** | Now waits for controller existence (`bluetoothctl list`). |

---

## 2. Real-world test matrix (both units, post-fix)

| Test | living-room-media | master-bedroom-media |
|------|-------------------|----------------------|
| TV audio through hub | ✅ (measured: signal present at speaker bus) | ✅ user-verified, MQTT volume control |
| Bluetooth A2DP audio | ✅ (measured at source and speaker bus) | ✅ user-verified, MQTT volume control |
| Sendspin / Music Assistant | ✅ | ✅ user-verified, MA volume independent of MQTT Music volume |
| Ducking engage + release | ✅ (tone test + hook cycles) | ✅ user-verified, 5/5 cycles; pause verified |
| IR remote → master volume | ✅ (journal + MQTT state) | ✅ user-verified, reflected in MQTT |
| Reboot with TV off | ✅ (loopback waits, links on node appearance) | ✅ (21/21, clean graph, no rogue links) |
| Broker down at startup | ✅ (5 s retry, no crash loop) | same code |
| PipeWire restart recovery | ✅ (transparent reconnect) | same code |
| `scripts/validate.sh` | 21/21 | 21/21 |

---

## 3. Issue-by-issue detail with evidence

### 3.1 — Duck never releases on pause (High) — ACCEPTED

**QA claim:** ducking means "any uncorked stream on `bus.music`"; Sendspin
keeps its stream open while paused; the README verification used a test tone
whose stream closes, so pause was never exercised.

**Assessment:** correct. The code comments admitted the limitation and the
tone test could not distinguish pause from stop.

**Implementation:** Sendspin 7.5 exposes `--hook-start` / `--hook-stop`
(verified in `sendspin daemon --help`). The installer ships
`scripts/audiohub-music-hook`, which the provisioning script wires into
`settings-daemon.json`:

```json
"hook_start": "/home/pi/.local/bin/audiohub-music-hook start",
"hook_stop":  "/home/pi/.local/bin/audiohub-music-hook stop"
```

The hook touches/removes `$XDG_RUNTIME_DIR/audiohub/music-playing`. hubd's
ducking is **flag-primary**: when the flag's directory exists, the flag alone
decides (the stream cannot distinguish playing from paused); stream-based
detection remains as fallback when hooks are not provisioned.

**Real-world proof (master-bedroom, user test):** four complete ducking
cycles in `journalctl --user -u hubd`, each releasing within the 2 s hold-off
of the user clicking MA's pause icon or "stop playback":

```
17:57:39 Ducking ON   17:57:58 Ducking OFF
17:58:03 Ducking ON   17:58:21 Ducking OFF
17:58:27 Ducking ON   17:58:36 Ducking OFF
18:00:08 Ducking ON   18:00:13 Ducking OFF
```

Flag file verified absent after the final stop; "Ducking Active" sensor
returned to OFF.

### 3.2 — MA volume slider controls master (High) — ACCEPTED

**Proof the flag is real** (`sendspin daemon --help`, sendspin 7.5.0):
`--hardware-volume {true,false}`. **Implementation:** provisioning script
sets `"use_hardware_volume": false` alongside the hooks.
**Real-world:** user-verified on master-bedroom — MA's volume control and
the MQTT Music volume now operate independently.

### 3.3 — `node.linger` / destroyed loopbacks (High) — REJECTED AS SPECIFIED

**QA claim:** WirePlumber destroys streams whose target is missing; both
loopbacks need `node.linger = true` or TV audio cannot return after a
TV off/on cycle.

**Evidence against:**

1. **The property does not exist.** A source-tree search for
   `node.linger` in PipeWire returns nothing:
   `gh search code "node.linger" --repo PipeWire/pipewire` → no hits.
   The nearest real property is `object.linger` ("If the object should
   outlive its creator" — `pipewire-props(7)`), which is semantically
   inapplicable: a loopback stream's creator is the PipeWire module itself,
   which stays alive. This is the same failure mode as `stream.capture.silence`
   (issue found earlier the same day), which also does not exist anywhere in
   the PipeWire source.

2. **The destruction claim is contradicted on hardware.** living-room-media
   was rebooted at 16:16 with the TV off — exactly the "target missing" case.
   The loopback was *not* destroyed: it sat unlinked, and when the UR23 node
   appeared it linked and carried audio (measured at the speaker bus:
   RMS 961 with the TV's own content). master-bedroom was rebooted with the
   TV off at 18:07 with the same result — `loopback.tv.capture` linked to the
   present UR23 node, `loopback.tv2` (the other candidate name) waiting.

3. **The QA's own reference does not contain the proposed fix.** The master-
   bedroom ducts file cited ("as in the version tested on your unit") contains
   no `linger` and no `dont-fallback` on any stream (verified by grep).

**Action taken instead:** the TV off/on transition is retained as an open
*test* (see §5) — watched live with `pw-link -l` — rather than a config
change made for an invented property. The dual-target design
(`40-loopback-tv.conf`) already covers both observed node names, and
`node.dont-fallback` prevents the v1-style wrong-node linking.

### 3.4 — Cubic volume scale (Medium) — ACCEPTED

**QA claim:** `DUCK_LEVEL=0.20` is applied as a Pulse volume value, and Pulse
volumes are cubic — 0.2 means amplitude 0.008 ≈ −42 dB, i.e. inaudible.

**Corroborating proof from the hardware:** master-bedroom's `unit.env`
carried a "user-tuned" `AUDIOHUB_DUCK_LEVEL=0.58` with no explanation. The
cube root of 0.20 is **0.5848** — the second deployment had empirically
derived the cubic equivalent of "20% loudness". Two independent units
converging on the same constant is strong evidence the scale is cubic.

**Implementation:** hubd converts perceptual → cubic when applying and when
verifying duct levels (`_cubic()`); configuration is now documented as
perceptual loudness, and master-bedroom's `unit.env` was reset to 0.20 so
both units mean the same thing.

### 3.5 — Second audio stack (Medium) — PARTIALLY ACCEPTED

**Accepted and implemented:**

- The `sendspin` system user (uid 997) existed **with Linger=yes on both
  units**, and its session ran a second `/usr/bin/pipewire` (verified:
  `ps -u sendspin` showed PID 955 on living-room). The installer now runs
  `loginctl disable-linger sendspin`, and validate.sh **fails** if a second
  PipeWire is running as that user.
- `audio-hub-ducts.service` added to `remove_legacy` in the installer.
- validate.sh gained the single-audio-stack check.

**Rejected with evidence:** the QA attributes "today's root cause for
Bluetooth" to this second stack. The proven root cause is independent of it:

- The A2DP endpoint registered **with** the second stack present (14:46 boot,
  living-room: transports opened, `fd(28) ready` in the bluetoothd log) and
  failed **with** it present only after `wireplumber.settings` was added to
  the Bluetooth fragment (16:08 boot: adapter advertised no Audio Sink UUID;
  `btmon` showed the phone browsing SDP and never attempting A2DP/PSM 25
  before disconnecting itself). The working unit's config comment documented
  the same finding before we hit it.
- Root cause chain, each proven: (1) the settings block disables endpoint
  registration; (2) a bluetoothd segfault killed the pairing agent, whose
  `Restart=on-failure` let a clean exit stand — fixed with
  `PartOf=bluetooth.service` + `Restart=always`; (3) failed acquires poison
  the A2DP SEP (`a2dp_resume() SEP in bad state`) until a full reconnect.

The second-stack hardening is good hygiene regardless of attribution.

### 3.6 — Stable device name (Medium) — ACCEPTED

Verified live on hardware: setting `"audio_device": "pipewire"` produced
`Using audio device 3: pipewire` in the sendspin log (and the *index*
changed from 3 to 2 across boots while the name did not — demonstrating why
the name is the stable form). Index detection removed from the provisioning
script, which now always exits successfully (the old detect-fail loop was a
boot-stability hazard); identity sync retained.

### 3.7 — UR23 as graph clock (Medium) — ACCEPTED, WITH A CORRECTION TO THE QA'S OWN FIX

The concern is sound: with suspend disabled the UR23 capture runs even
without signal, and as graph driver a stalled S/PDIF clock degrades the other
sources.

**Correction:** the QA's implied property `node.priority.driver` is
**silently ignored** by WirePlumber rules. Deploying it changed nothing —
the node stayed at `priority.driver=2009` (verified with `pw-dump`). The
working key is `priority.driver`:

```
UR23 priority.driver=50        (after fix; was 2009)
built-in output = 1000         (so the built-in drives whenever it runs)
```

Verified applied on both units. **Real-world:** user tested BT and Sendspin
with the TV off — clean playback.

### 3.8 — Duplicate `monitor.alsa.rules` arrays — ACCEPTED

Merged into a single array (node rule + card rule). Defensive: the second
block demonstrably applied on this WirePlumber version (the UR23 node's
suspend-disabled behaviour was observable), but the merge removes ambiguity.

### 3.9 — Periodic state sync — ACCEPTED

hubd now runs a 2-second change-driven sync: actual volumes/mute are compared
against the last published values and only deltas are published. HA stays
truthful about changes made outside hubd (wpctl, manual `pactl`, an older
daemon instance) without retained-message churn.

### 3.10 — TV Playing sensor — ACCEPTED AND EXTENDED (two rounds)

The QA is right that node existence is wrong, but the suggested pulse state
was *also* insufficient: with suspend disabled and the loopback consuming it,
the UR23 source stays pulse-RUNNING even with the TV off (verified on
master-bedroom: `86 alsa_input.usb-HiFimeDIY…RUNNING` while the TV was off).

A second approach — checking `Status: Running` in `/proc/asound` — was then
**disproven by the TV-unplugged test**: with the TV completely removed from
power, the UR23 PCM *still* reports `Status: Running, Momentary freq =
48000 Hz`. The receiver free-runs on its internal clock whenever the capture
is open, streaming pure digital silence (measured: RMS 0.0, peak 0, a single
distinct sample value across 135,168 frames).

Final implementation: a **rate-limited level probe**. Every 10 s hubd records
~0.35 s of raw samples from the UR23 (`pw-record --raw` on stdout, SIGKILLed
after timeout — the already-flushed bytes arrive via
`TimeoutExpired.stdout`) and measures RMS. The sensor is ON only when
non-silent audio is actually arriving. Verified on both units with the TV
unplugged: `tv/active = OFF`. Hardware note: master-bedroom's UR23 stays
locked at 48 kHz with the TV in standby (optical passthrough live), while
living-room's PCM fully stops — so "signal present on the wire" is the only
honest definition of this sensor.

### 3.11 — validate.sh UR23 / Audio Sink — PARTIALLY ACCEPTED

"Never finds the node" was factually wrong — the wpctl-based check passed on
living-room (it returned the live node name and matched it against the
loopback targets). Refactored to `pactl list short sources` anyway for
consistency with the rest of the script. The severity bump for the missing
Audio Sink UUID (warn → **fail**) was accepted; it was the single most
important BT failure indicator.

### 3.12 — WiFi property name — ACCEPTED

`connection.auth-retries` (full name) used; a shorthand rejection would
abort the installer under `set -e`. The WiFi health logger/watchdog from the
second deployment is noted as optional future tooling, not part of the core
stack.

### 3.13 — bluetooth-setup wait condition — ACCEPTED

The script now waits for controller *existence* (`bluetoothctl list`) rather
than `Powered: yes` — powering the adapter on is the script's own job, so
waiting for power before powering on was circular.

---

## 4. Also fixed during bring-up (not in the QA report)

- `bt-agent` is not an apt package on Debian Trixie — the binary comes from
  `bluez-tools` (the howto's package list fails on this).
- `config/bluetooth/main.conf` shipped invented keys (`[A2DP]` group,
  `AutoConnect`) that bluetoothd rejects on every start;
  `JustWorksRepairing = always` restored for guest re-pairing.
- Per-unit Bluetooth name: the adapter alias is set from `unit.env` at boot
  (`bluetooth-setup.sh`), so a second hub does not advertise the first hub's
  name.
- Stale retained MQTT topics from the v1 client (and, on master-bedroom, from
  the underscore-prefixed era) were wiped from the broker so Home Assistant
  shows only current entities. Note master-bedroom's device id is
  dash-style (`master-bedroom-media`) while living-room's is underscore-style
  (`living_room_media`) — the provisioning preserves each unit's existing
  identity.
- WirePlumber's stream-properties state can restore stale ducked duct volumes
  at boot (June-era 0.2 resurfaced after redeploy); hubd now re-asserts duct
  volumes every poll, making it the sole owner of the duct levels.

## 5. Verification methodology (reproduce any claim above)

| Claim | Command |
|---|---|
| Endpoint registered | `bluetoothctl show \| grep "Audio Sink"` |
| Phone never attempted A2DP | `sudo btmon -w /tmp/btmon.log`, then `sudo btmon -r /tmp/btmon.log` — count L2CAP Connection Requests by PSM |
| Where audio stops | `pw-record --target <node> /tmp/x.wav` + RMS check (pure Python; `audioop` is gone in 3.13) |
| Node names / second stack | `pactl list short sinks` / `pgrep -u sendspin pipewire` |
| Driver priority applied | `pw-dump \| jq '.. \| select(.info?.props?."node.name"? // "" \| contains("HiFimeDIY"))'` |
| TV signal presence | `grep -E "Status\|Momentary" /proc/asound/card*/stream0` |
| Graph wiring | `pw-link -l` (ports and arrows print on separate lines) |
| What MA/hubd last said | `mosquitto_sub -t "<device-id>/#" -v` (mind each unit's id style) |

## 6. Open items

1. ~~**TV unplugged test**~~ — **DONE (2026-09-26 evening).** With the TV
   fully removed from power: hub reboot clean (validate 21/21, no rogue
   links, ducts normalized, BT/Sendspin untouched); the analog-stereo
   loopback stayed linked to the persisting UR23 node; the UR23 proved to
   free-run digital silence (all-zero stream, RMS 0.0) — which retired the
   `/proc`-based sensor approach in favour of the level probe (§3.10).
   Expected next: on plug-in, the sensor returns ON and TV audio flows
   through the already-linked loopback.
2. Optional: WiFi health logger/watchdog from the second deployment.
3. Optional: HA player-state link if pause semantics ever change upstream in
   Sendspin/MA (the hooks currently cover it).
