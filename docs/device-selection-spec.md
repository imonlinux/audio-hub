# Runtime Device Selection Spec

Status: PROPOSED (not implemented)

Motivation: the graph's hardware coupling is baked into two config files —
`40-loopback-tv.conf` (UR23 candidate node names) and `50-ducts.conf` (the
hardware sink target, currently a Pi 4B-specific
`alsa_output.platform-fe00b840.mailbox.stereo-fallback`). A different USB
capture, a USB DAC, or a different Pi revision means editing configs by hand.
This spec replaces the hardcoded targets with hubd-managed, user-selectable
device routing: sources (except Bluetooth and Sendspin, which stay name-routed)
and the output device are chosen from a live list of what is actually plugged
in, exposed through the Home Assistant entities the hub already publishes.

---

## 1. Summary

| Aspect | Decision |
|---|---|
| UI surface | Two new Home Assistant `select` entities (MQTT discovery): **Output Device**, **TV Source**. No standalone web UI. |
| Enumeration | hubd lists sinks/sources over its existing Pulse connection on the 2 s state-sync cadence. |
| Application | Runtime retargeting with `sink_input_move` / `source_output_move` (pulsectl) — no config rewrite, no PipeWire restart, no dropout on output changes. |
| Selection key | Stable node-name **prefix** (profile suffix stripped), so the UR23 `analog-stereo`/`stereo-fallback` flip and built-in profile changes do not orphan a selection. |
| Persistence | User selection in `~/.config/audiohub/selection.json` (hubd runs unprivileged); `unit.env` patterns remain the factory default / bootstrap target. |
| Reconciliation | hubd is the sole owner of routing state after boot; declarative configs keep only cold-boot defaults. Same ownership model as duct volumes today. |
| Bluetooth / Sendspin | Unchanged. Both remain stream-name-routed (WP rules + `PIPEWIRE_NODE`); they never appear in the selectable lists. |

## 2. Problem

1. `50-ducts.conf` hardcodes `alsa_output.platform-fe00b840.mailbox.stereo-fallback`
   three times. The platform address is specific to the Pi 4B's DT; on a Pi 5
   (different mailbox address) the ducts target a nonexistent node and the
   unit is silently mute. It is also the only place in the graph that violates
   the "stable names only" principle by baking a full ALSA node name.
2. `40-loopback-tv.conf` hardcodes the UR23 by product string. Any other TV
   capture (another USB S/PDIF receiver, an HDMI capture card) needs a new
   config file per candidate node name.
3. Both fixes require SSH access to the unit. Every other unit behaviour
   (volumes, mute, ducking) is already controllable from Home Assistant.

## 3. Non-goals

- No full patchbay UI (Helvum/qpwgraph-style linking). Routing topology stays
  fixed: sources → buses → ducts → selected output.
- No per-application routing. The bus/duct model is the graph.
- No standalone HTTP UI in this pass. HA is the surface; an HTTP API can layer
  on the same hubd state later.
- Bluetooth and Sendspin routing are not user-selectable; they follow their
  protocols' own connection lifecycle into fixed buses.

## 4. Design

### 4.1 Discovery and filtering

hubd enumerates with `pactl`-equivalent Pulse list calls on its existing
loop-owned connection (no new dependency):

- **Output options** — `sink_list()`, keep hardware sinks: name starts with
  `alsa_output.` (and, defensively, `bluez_sink.` is excluded alongside).
  Exclude: `bus.*`, `auto_null*` (PipeWire's fallback placeholder).
- **TV source options** — `source_list()`, keep hardware capture nodes:
  name starts with `alsa_input.`. Exclude: `*.monitor` (any sink monitor,
  including bus and hardware monitors), `bluez_source.*`.

The bus/duct loopback nodes never appear in either list: loopback capture and
playback sides are client streams (source-outputs / sink-inputs), not source
or sink nodes.

### 4.2 Selection key: stable prefix

ALSA node names carry a trailing profile component that is not stable:
`...analog-stereo` (signal/profile active) vs `...stereo-fallback` (probing) —
the UR23 flip documented in the README, and the same class of change can hit
built-in outputs on profile transitions.

- Option string (the MQTT payload and HA option) = node name with the final
  dot-component stripped:
  `alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01`
  `alsa_output.platform-fe00b840.mailbox`
- Applying a selection resolves the prefix against live nodes (exact prefix
  match, longest live node wins). The current dual-loopback trick becomes
  unnecessary for *selection*, though the bootstrap configs keep it until
  hubd takes over.
- Collision rule: if two live devices share a prefix (two identical dongles),
  the option list falls back to full node names for those entries. USB names
  embed the connection ordinal (`-01`, `-02`), so this is the rare case, but
  it must not silently merge two devices.

### 4.3 Home Assistant entities

MQTT `select` discovery, published by the existing `_publish_discovery` path:

| Entity | unique_id | command topic | state topic |
|---|---|---|---|
| Output Device | `{id}_output` | `{id}/output/set` | `{id}/output/state` |
| TV Source | `{id}_tv_source` | `{id}/tv_source/set` | `{id}/tv_source/state` |

- `options` is published in the discovery payload and **republished whenever
  the discovered device set changes** (device plugged/unplugged), so the HA
  dropdown always reflects reality.
- `state_topic` publishes the **effective** selection — the prefix currently
  applied to the graph — not merely the desired one. If the selected device
  is absent, state stays on the last effective selection.
- Both entities carry the standard availability topic.
- Command payloads are validated against the current options list; unknown
  prefixes are logged and ignored.

### 4.4 Applying a selection

Retargeting uses the move operations pulsectl already exposes; both are
ordinary property changes on existing streams — no stream rebuild:

- **Output** → every `duct.*.playback` sink-input is moved to the resolved
  sink with `sink_input_move()`. All three ducts move together; the output
  device is a unit-wide choice. Instant, no dropout.
- **TV source** → the active TV loopback's capture side (the source-output
  whose `node.name` is `loopback.tv.capture` / `loopback.tv2.capture`) is
  moved to the resolved source with `source_output_move()`. Only the TV path
  gaps (one quantum-scale transition); BT/music are untouched.

Both operations run on the asyncio loop via the existing `AudioControl`
connection, consistent with the threading model (the ducking engine keeps its
own connection and is not involved).

Bootstrap invariant: until hubd applies a selection, the declarative configs
determine routing. hubd's reconciler only moves a stream when its current
target differs from the resolved selection, so a freshly booted unit with
default wiring performs no moves at all (idempotence requirement).

### 4.5 Persistence and reconciliation

- Desired selection: `~/.config/audiohub/selection.json`
  `{"output": "<prefix>", "tv_source": "<prefix>"}`. Written on each accepted
  command. `unit.env` keys (`AUDIOHUB_LINEOUT_PATTERN`,
  `AUDIOHUB_TV_SOURCE_PATTERN`) remain the factory defaults used when the
  file is absent — `/etc/audiohub` stays root-owned and unwritable by hubd.
- Reconciler: a step in the existing 2 s state-sync loop, which already owns
  "compare actual vs published" logic:
  1. Enumerate sinks/sources; republish discovery options if the set changed.
  2. Resolve desired selections against live nodes.
  3. For each duct playback / TV capture whose current target does not match,
     move it (this covers boot, arrival of the selected device, and drift).
  4. Publish effective state on change.
- Device arrival (DAC plugged in, TV receiver re-enumerated): the next sync
  tick resolves the prefix and moves the streams. No restart, no user action.
- Device removal: PipeWire relocates or kills orphaned streams itself; the
  reconciler detects the mismatch and holds the last effective state until
  the selected device returns. `validate.sh`-style logging reports the gap.

### 4.6 Failure modes

| Case | Behaviour |
|---|---|
| Selected output absent at boot | Ducts stay on declarative bootstrap target; state shows last effective; moves when it appears. |
| UR23 profile flip mid-playback | Prefix resolves to the new node name on the next sync tick (≤ 2 s); capture moves. |
| Duplicate identical devices | Option list switches those entries to full node names (§4.2). |
| Unknown/typo payload on `set` | Logged, ignored, state unchanged. |
| hubd down | Declarative configs still route audio (bootstrap targets) — degradation is "no remote control", never "no audio". |
| Moving the TV capture while the TV is off | `source_output_move` on a not-created source-output is a no-op / logged; the reconciler retries once the loopback capture exists. |

## 5. Config surface changes

`unit.env` (no schema break; both keys already exist):

```
AUDIOHUB_LINEOUT_PATTERN=Built-in Audio Stereo   # factory default output
AUDIOHUB_TV_SOURCE_PATTERN=UR23                  # factory default TV source
```

New state file (created on first user selection, never by the installer):

```
~/.config/audiohub/selection.json
```

Config files change only in that the hardcoded targets become clearly
labelled bootstrap defaults; no format change.

## 6. Code changes (by file)

| File | Change |
|---|---|
| `hubd/main.py` | Device enumeration + prefix resolution helpers; two `select` entities in `_publish_discovery`; command handling for `output/set`, `tv_source/set`; reconciler step in the state-sync loop; `AudioControl.move_ducts_to_sink()` / `move_tv_capture_to_source()`. |
| `unit.env.example` | Comments noting the keys are factory defaults overridden by `selection.json`. |
| `docs/qa-disposition` follow-up | The duct hardcode finding (50-ducts.conf) is superseded by this spec once implemented. |
| `README.md` | Architecture section: output/source selection via HA entities; drops the "specific hardware" caveat once verified. |
| `scripts/validate.sh` | New check: both select entities present in discovery and reconciler active (hubd journal line). |

No systemd, installer, or PipeWire/WirePlumber config format changes are
required. `pw-loopback` process spawning is explicitly *not* used — moves
keep the loopback nodes module-owned exactly as today.

## 7. Validation plan

Reproducible on a unit, per the disposition doc's methodology:

1. `mosquitto_sub -t "<id>/#" -v` — see both select states and options.
2. Plug a second output (any USB DAC); within one sync tick the options list
   contains it; select it; `pw-link -l` shows all duct playback ports on the
   new sink; audio continues without dropout.
3. Unplug it; ducts relocate per PipeWire default handling; select the
   built-in again; graph normal, no rogue links (`check_no_feedback` clean).
4. TV source: with only the UR23 present, options show it; toggle the TV's
   power to force the profile flip and confirm the capture re-resolves ≤ 2 s.
5. Cold boot with nothing selected: zero move operations in the hubd journal
   (idempotence), graph identical to today.
6. `selection.json` round-trip: rm the file → factory defaults reapply.

## 8. Open questions

1. Should the output selection also drive the master-volume target
   (`AUDIOHUB_LINEOUT_PATTERN` matching in `AudioControl`), so IR/HA master
   volume follows the selected output? Recommended: yes — master volume
   should attach to whichever sink the ducts feed, otherwise volume controls
   the wrong device after a selection change.
2. TV Source options: expose *all* hardware captures, or only those with an
   S/PDIF-capable profile? Recommended: all hardware captures; profile
   filtering adds coupling for no real-world unit today.
3. Should a BT sink ever be selectable as output (hub feeding a BT speaker)?
   The A2DP sink path exists, but ducking semantics and the single-stack
   guarantee were designed around analog out. Recommended: keep BT excluded
   from the output list this pass; revisit if a use case appears.
