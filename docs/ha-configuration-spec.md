# Home Assistant Configuration & IR Editor Spec

Status: APPROVED (2026-10-15) — reviewed against the code on `feat/ir-remap`
(f01382c) and HA core source; supersedes the HA surface of `feat/ir-remap`
Layer 1 (commit `f01382c`). The Layer 1 internals (`IRMapStore`, `IRHandler`
event translation, loop-thread dispatch, tests) are kept. Review outcomes are
folded in; the former open questions are recorded as decisions (§9).

Motivation: the project goal is that a unit is fully configurable from Home
Assistant once it is on the network. Today three things stand in the way:
behaviour settings live in the root-owned `unit.env` (SSH + edit + restart),
the IR remap UI grows one entity per key with no way to unbind, and teaching
the FLIRC a new remote button still needs the FLIRC GUI on another computer.
This spec makes the HA device page the unit's settings page, replaces the IR
UI with a fixed-size editor, exposes every keypress to HA automations, and
lays out how updates and FLIRC learning join the same surface.

---

## 1. Summary

| Aspect | Decision |
|---|---|
| UI surface | HA device page via MQTT discovery only. No web UI. Every entity gets an `entity_category`, so HA splits the page into **Controls / Configuration / Diagnostic**. |
| Runtime settings | New `~/.config/audiohub/settings.json` (hubd-owned, same model as `selection.json`). Precedence: `settings.json` > `unit.env` > built-in default. |
| Bootstrap settings | Stay in `unit.env`: anything needed *before* MQTT works, or whose change would rename entities. |
| IR editor | Fixed set of entities regardless of how many keys are mapped: Key select, Action select, Identify button, status + mappings sensors. "None" unbinds. |
| IR to HA | MQTT `event` entity **IR Remote**: every press fires an HA event, mapped or not, so any remote button can drive any HA automation. |
| Hold-to-repeat | Rate-limited on the loop thread (default 150 ms per step). |
| Updates | Native HA `update` entity (Settings → Updates) backed by the existing `audiohub-update`, triggered through a root path unit, with a request token echoed into the status file to close the stale-status race (§4.7). |
| FLIRC learning | Phase 4, gated on verification: "Teach New Button" uses `flirc_util` on the Pi so no second computer is needed. |

## 2. Problem

1. **Settings need SSH.** Duck level, release delay, IR step, IR repeat, TV
   detection threshold and update preferences are read once from
   `/etc/audiohub/unit.env` at startup. hubd runs unprivileged and cannot
   write that file, so HA cannot change them. The **Audio Ducking** switch
   is a partial exception: it works, but its state is not persisted and
   reverts to `unit.env` on restart.
2. **Flat device page.** No discovery payload sets `entity_category`, so
   volume sliders, IR plumbing and status sensors appear in one list.
3. **IR entity sprawl.** Layer 1 publishes one `select` per bound key
   (`IR KEY_F13 Action`). Keys can only be set to `ignore`, never removed;
   deleting `ir_map.json` leaves orphan entities in HA. Options are raw
   identifiers (`volume_up`, `ducking_toggle`).
4. **Bind ritual.** Binding needs three entities in a fixed order (choose
   action → press Next Keypress → read a text status sensor). Choosing the
   action *before* knowing which key will be captured is backwards.
5. **Repeat rate.** The FLIRC is a USB keyboard; the kernel generates EV_KEY
   value-2 repeats at ~30 Hz after 250 ms. At 3 %/step, holding Vol+ for one
   second moves volume on the order of 60 %, with a Pulse round-trip and an
   MQTT publish per step.
6. **Remote is hub-only.** Buttons can only trigger the four built-in
   actions; nothing reaches HA automations (Music Assistant play/pause,
   lights, scenes).
7. **FLIRC programming needs another PC.** Binding maps *keyboard keys* the
   FLIRC already emits. A remote button the FLIRC has never been taught
   produces nothing, and teaching it requires the FLIRC GUI elsewhere
   (`docs/rpi-audio-hub-howto.md` §5.3).

## 3. Non-goals

- No standalone web UI or HA custom integration/Lovelace card. MQTT
  discovery entities only.
- MQTT credentials, broker address and `DEVICE_ID` are not HA-editable
  (chicken-and-egg; changing `DEVICE_ID` renames every entity).
- No arbitrary-command IR actions (shell, HTTP). Anything beyond the
  built-in actions goes through HA via the IR Remote event (§4.5).
- No WiFi configuration from HA. Network bootstrapping stays in
  `unit.env` / NetworkManager.

## 4. Design

### 4.1 Entity categories and device page layout

Every discovery payload carries `entity_category` (omitted = Controls).
Advanced entities also set `enabled_by_default: false` so they exist but stay
hidden until the user enables them. HA rejects `entity_category: config` on
`sensor` and `binary_sensor` entities (mqtt `entity_category_without_config`),
so read-only status sensors are always `diagnostic`.

Target layout (✱ = `enabled_by_default: false`):

| Section | Entities |
|---|---|
| **Controls** | Master Volume, Master Mute, TV / Bluetooth / Music Volume, Audio Ducking |
| **Configuration** | Output Device, TV Source, Duck Level, Duck Release Delay, IR Volume Step, IR Hold-to-Repeat, IR Repeat Interval ✱, IR: Key, IR: Action, IR: Identify Button, IR: Teach New Button (Phase 4), TV Detection Threshold ✱, TV Detection Interval ✱, Auto Update, Release Channel, Reset Settings ✱ |
| **Diagnostic** | Hub Availability, Ducking Active, Music / Bluetooth / TV Playing, IR Remote (event), IR Editor Status, IR Mappings, Firmware (update entity) |

The device block gains `sw_version` (from `/etc/audiohub/version`) and the
discovery payloads gain an `origin` block (`{"name": "audio-hub", "sw": <version>}`).
One `_device_block()` helper replaces the three copies of the device dict in
`main.py`. The version file is read defensively (missing file → `dev`), so
development machines and tests without `/etc/audiohub` work.

### 4.2 Settings store

New `SettingsStore`, modelled on `SelectionStore`:

- File: `~/.config/audiohub/settings.json`, atomic write (`.tmp` +
  `os.replace`), thread-safe (lock; the ducking thread reads duck level and
  release delay).
- Contents: only keys the user has changed from HA. Absent key = fall
  through to `unit.env`, then the built-in default. This keeps `unit.env`
  meaningful as the fleet default and makes "Reset Settings" a value wipe.
- Schema: `{"version": 1, "values": {"duck_level": 0.2, ...}}`. Unknown keys
  are ignored with a warning; out-of-range values are clamped on load.
- `HubConfig` stays the bootstrap object. Runtime readers call
  `settings.get("duck_level")`, which resolves the precedence chain. No
  restart is needed for any setting in §4.3.

Split of today's `unit.env` keys:

| Stays bootstrap-only (`unit.env`) | Becomes runtime (HA → `settings.json`) |
|---|---|
| `HOSTNAME`, `DEVICE_ID`, `DEVICE_NAME` (see §9) | `DUCKING_ENABLED` |
| `MQTT_*` | `DUCK_LEVEL`, `DUCK_RESTORE_DELAY` |
| `BUS_*`, `DUCT_*`, `POLL_INTERVAL` | `IR_VOLUME_STEP`, `IR_REPEAT`, new `IR_REPEAT_INTERVAL` |
| `LINEOUT_PATTERN`, `TV_SOURCE_PATTERN` (already overridden by `selection.json`) | `TV_PROBE_THRESHOLD`, `TV_PROBE_INTERVAL` |
| `IR_DEVICE`, `SENDSPIN_VERSION`, `RELEASE` (pin), `WIFI_CONNECTION` | `AUTO_UPDATE`, `RELEASE_CHANNEL` (§4.7) |

### 4.3 Settings entities

All topics are `{id}/setting/{key}/set` and `{id}/setting/{key}/state`
(retained state). Commands are validated and clamped; the accepted value is
written to `settings.json`, applied live, and echoed on the state topic.
The Audio Ducking switch keeps its existing topics (`{id}/ducking/...`) for
compatibility and gains persistence.

| Entity | Type | Key | Range / options | Applies to |
|---|---|---|---|---|
| Audio Ducking | switch (Controls) | `ducking_enabled` | ON/OFF | `DuckingEngine.set_enabled` (existing topic `{id}/ducking/set` kept; now persisted) |
| Duck Level | number, slider | `duck_level` | 0–100 %, step 5 | Duck amplitude. Same semantics as today: 20 % = amplitude 0.2 ≈ −14 dB (≈35–40 % perceived). The entity description says so. |
| Duck Release Delay | number, box | `duck_restore_delay` | 0–30 s, step 0.5 | Ducking hold-off |
| IR Volume Step | number, slider | `ir_volume_step` | 1–10 %, step 1 | `_ir_step` |
| IR Hold-to-Repeat | switch | `ir_repeat` | ON/OFF | §4.6 |
| IR Repeat Interval ✱ | number, box | `ir_repeat_interval_ms` | 100–1000 ms, step 50; default 150 | §4.6 |
| TV Detection Threshold ✱ | number, box | `tv_probe_threshold` | 10–5000 RMS | TV Playing probe |
| TV Detection Interval ✱ | number, box | `tv_probe_interval` | 2–60 s | TV Playing probe |
| Auto Update | switch | `auto_update` | ON/OFF | §4.7 |
| Release Channel | select | `release_channel` | `stable`, `canary` | §4.7 |
| Reset Settings ✱ | button | — | `PRESS` | Deletes `settings.json` values (not IR map, not selection), re-applies every setting live from the fall-through values, and republishes every setting state |

The number entities publish `min`/`max`/`step`/`unit_of_measurement`/`mode`
in discovery. Percent values travel as 0–100 on MQTT for these settings (HA
renders them natively); internally they are stored as 0.0–1.0. The volume
entities keep their existing 0.0–1.0 payloads plus templates; this spec does
not change them.

### 4.4 IR editor

Replaces Layer 1's per-key selects and the Bind Action / Next Keypress /
Bind Status trio with a fixed set of entities that edit one mapping at a
time.

| Entity | Type | Category | Topics |
|---|---|---|---|
| IR: Key | select | config | `{id}/ir_edit/key/set`, `/state` |
| IR: Action | select | config | `{id}/ir_edit/action/set`, `/state` |
| IR: Identify Button | button | config | `{id}/ir_edit/identify/set` (`PRESS`) |
| IR Editor Status | sensor | diagnostic | `{id}/ir_edit/status/state` |
| IR Mappings | sensor | diagnostic | state `{id}/ir/mappings/state` (count); attributes `{id}/ir/mappings/attributes` (JSON `{label: action}`) |

**Key select options.** These are the union of factory keys, bound keys,
and keys *seen* from the remote (persisted, most recent 32). Each option is a
human label with the canonical name in parentheses, so it reads well and
round-trips:

```
Volume Up (KEY_VOLUMEUP)
Volume Down (KEY_VOLUMEDOWN)
Mute (KEY_MUTE)
F13 (KEY_F13)
```

The label comes from a small table for common keys, otherwise from the name
with `KEY_` stripped and title-cased. The parser takes the text inside the
final parentheses. Options are republished in discovery whenever the set
changes (a new key is seen), the same as the device-selection selects.

**Action select options**, labels mapped internally to the existing action
ids:

| Option | Action id |
|---|---|
| Volume up | `volume_up` |
| Volume down | `volume_down` |
| Mute toggle | `mute_toggle` |
| Ducking toggle | `ducking_toggle` |
| None (HA event only) | *unbound* |

**Editing flow.**

1. Pick a key in **IR: Key**. **IR: Action** immediately shows that key's
   current action (state publish).
2. Change **IR: Action**. The change is applied and persisted at once; there
   is no Save button. Choosing *None* unbinds the key: it stays in the Key
   list as a seen key and still fires IR Remote events.
3. **IR Mappings** updates; a Markdown card can render its attributes as a
   table.

**Identify flow** (for buttons the FLIRC already sends):

1. Press **IR: Identify Button**. Status: `Press a button on the remote (60 s)`.
   Local dispatch is suppressed while armed, as in Layer 1.
2. The next real press selects that key in **IR: Key** (adding it to the
   options if new) and shows its current action. Status:
   `Captured: F13 (KEY_F13), choose an action`.
3. The user picks the action as in the editing flow. Status returns to
   `idle` 10 s after the capture or a timeout.

Capturing first and choosing second removes Layer 1's ordering pitfall, and
a capture never changes a mapping on its own.

**Map file v2.** `ir_map.json` becomes
`{"version": 2, "bindings": {KEY: action_id | "none"}, "seen": [KEY, ...]}`.
`"none"` is an explicit tombstone so an unbound *factory* key is not
re-added by the factory overlay. Migration: a v1 flat dict loads as
`bindings`; v1 `"ignore"` becomes `"none"`. The file is rewritten as v2 on
the next change.

**Retiring Layer 1 entities.** On connect, hubd publishes empty retained
payloads to the config topics Layer 1 may have created:
`{base}/select/{id}_ir_{key_lower}/config` for the union of **every key in
the v2 map file, every factory key, and every key name present in the
pre-migration v1 file** (collected before validation drops malformed
entries, so a dropped entry's entity is still swept), plus
`{base}/select/{id}_ir_bind_choose/config`,
`{base}/button/{id}_ir_bind_arm/config`,
`{base}/sensor/{id}_ir_bind_status/config` and
`{base}/sensor/{id}_ir_last_key/config` (superseded by §4.5). The retained
`{id}/ir/{KEY}/state` payloads of retired keys are cleared in the same
sweep. This is idempotent and runs every start, so units that skipped
Layer 1 are unaffected.

### 4.5 IR Remote event entity

MQTT `event` entity **IR Remote** (diagnostic), `unique_id {id}_ir_remote`,
`state_topic {id}/ir/event`, `event_types` = every option in the IR: Key list
(raw `KEY_*` names). On every real press (not repeats, not while Identify is
armed) hubd publishes, **not retained**:

```json
{"event_type": "KEY_F13", "action": "none", "label": "F13"}
```

- Mapped keys fire the event as well as the local action, so HA can observe
  everything (and automations can add behaviour to a mapped key).
- When a new key is seen, the discovery payload is republished with the
  extended `event_types` before the event is published. Known limitation:
  HA processes discovery updates asynchronously, so the **first** press of
  a never-before-seen key can still race the re-registration and be missed
  by automations; the second press always arrives. The Identify flow
  mitigates this in practice, because a key becomes `seen` (and its options
  republished) at capture time, before anyone automates it.
- Typical use: bind Play/Pause on the TV remote to *None* and write an HA
  automation "IR Remote event KEY_PLAYPAUSE → Music Assistant media_play_pause".

### 4.6 Hold-to-repeat

- Repeats (EV_KEY value 2) for repeatable actions are dispatched at most once
  per `ir_repeat_interval_ms` per key (monotonic timestamp check on the loop
  thread). The kernel still delivers ~30 Hz; hubd drops the excess.
- Repeat steps do not publish MQTT volume state; the 2 s state sync
  publishes the final value. The first (value-1) press still publishes
  immediately, so a single tap feels responsive in HA.
- `ir_repeat = OFF` restores strict press-only behaviour (Layer 1 semantics).

### 4.7 Updates from HA

**Entity.** MQTT `update` entity **Firmware** (diagnostic),
`unique_id {id}_firmware`:

- `state_topic {id}/update/state`, JSON:
  `{"installed_version": "v1.2.0", "latest_version": "v1.3.0", "title": "audio-hub", "release_url": "...", "in_progress": false}`
- `command_topic {id}/update/install`, `payload_install: "install"`.

It appears in HA under Settings → Updates and on the device page with HA's
standard Install button, release link and progress state.

**Privilege boundary.** hubd is unprivileged and `audiohub-update` runs as
root. Nothing gains privilege; root watches a request file:

| Piece | Owner | Purpose |
|---|---|---|
| `/etc/tmpfiles.d/audiohub.conf`: `d /run/audiohub 0755 <hub-user> <hub-user>` | installer | Request/status directory hubd can write |
| `audiohub-update-request.path`: `PathExists=/run/audiohub/update-request` | root | Fires the service below |
| `audiohub-update-request.service`: `rm -f` the request file, then `audiohub-update` (manual mode) | root | Install now |
| `audiohub-update --check --status-file /run/audiohub/update-status.json` | root, daily timer + after each install | Writes installed/latest/in-progress for hubd to publish |

**Request/status handshake.** hubd writes the request file containing a
fresh random token and sets `in_progress: true`. The updater echoes that
token into `update-status.json` on completion (success or failure). hubd
only exits `in_progress` when the status file carries its own token —
otherwise the previous run's status (e.g. yesterday's daily `--check`,
`in_progress` false) would flap the entity back to "up to date" mid-install.
hubd reads `update-status.json` on the 2 s sync loop. An install restarts
the audio stack and hubd. The update entity reconnects with the new
`installed_version`, which is the expected sequence. The `--status-file`
option and the daily `--check` timer are new; the weekly
`audiohub-update.timer` is unchanged.

**Preferences.** Auto Update and Release Channel are stored in
`settings.json`, but the root updater reads `unit.env`. `audiohub-update`
gains: read `AUTO_UPDATE` / `RELEASE_CHANNEL` from the hub user's
`settings.json` when present, falling back to `unit.env`. The installer
records the hub user in `/etc/audiohub/hub-user` so root tools can find the
file. An explicit `AUDIOHUB_RELEASE` pin in `unit.env` still wins over
everything (deliberate admin override).

### 4.8 FLIRC on-device learning (Phase 4, gated)

Adds **IR: Teach New Button** (button, config), shown only when
`flirc_util` is installed and can open the receiver (`flirc_util version`
succeeds at hubd start).

1. Press it. hubd picks the first spare key from `KEY_F13`…`KEY_F24` (not
   bound, not seen). Status: `Press the new button on the remote (30 s)`.
2. hubd runs `flirc_util record <key>` as a subprocess with a 30 s timeout,
   with the evdev listener paused, since the FLIRC is in record mode.
3. On success the key is added to IR: Key, selected, and the IR: Action
   select waits for a choice, the same end state as Identify. On failure
   (timeout, "button already exists", device busy) the status shows the
   `flirc_util` message and nothing changes.

Prerequisites to verify before building:
- the `apt.flirc.tv` package installs on 64-bit Trixie;
- `flirc_util record` accepts a target key argument in that form (verify
  the exact syntax against the installed version);
- a udev rule gives the hub user access to the FLIRC hidraw node;
- `flirc_util` and hubd's evdev reader can share the device, or the reader
  must be closed during `record`.

If any of these fails, Phase 4 is dropped and the howto keeps the
"program the FLIRC on another PC" step.

### 4.9 Failure modes

| Case | Behaviour |
|---|---|
| `settings.json` unreadable/corrupt | Warning logged; all settings fall through to `unit.env`; the next HA change rewrites the file. |
| Out-of-range command (e.g. duck level 150) | Clamped to range, applied, clamped value published as state. |
| HA retained command replayed at hubd start | Same validation path; idempotent. |
| Identify armed and no press | Times out after 60 s; status `timeout` → `idle`. |
| Key option payload not in the list | Logged, ignored, state unchanged. |
| New key seen while HA offline | Added to the `seen` list; options and `event_types` republished on reconnect. |
| Update request while one is running | `in_progress` already true; the request file is ignored until the run finishes. |
| Update fails | Updater writes the failure to the status file **echoing the request token**; the entity exits `in_progress` with the old `installed_version`; the journal has details. |
| Status file older than the request (no token match yet) | hubd stays `in_progress`; the entity does not flap to "up to date" mid-install. |
| `flirc_util` missing or not permitted | Teach New Button is not published; Identify still works. |

## 5. Config surface changes

`unit.env.example`: runtime keys are commented as "fleet default, overridden
from Home Assistant (settings.json)". New key `AUDIOHUB_IR_REPEAT_INTERVAL=150`.

New hubd-owned files (created on first change, never by the installer):

```
~/.config/audiohub/settings.json
~/.config/audiohub/ir_map.json   (v2 format, migrated in place)
```

New root-owned pieces: `/etc/audiohub/hub-user`,
`/etc/tmpfiles.d/audiohub.conf`, `audiohub-update-request.{path,service}`,
`audiohub-update-check.{service,timer}` (daily `--check`).

## 6. Code changes (by file)

| File | Change |
|---|---|
| `hubd/main.py` | `SettingsStore`; runtime reads through it (ducking, IR, TV probe); settings entities and command routing (`{id}/setting/+/set`); `_device_block()` + `origin`; `entity_category` / `enabled_by_default` on every payload; IR editor entities, v2 map + migration, identify flow, Layer 1 entity retirement; IR Remote event entity; repeat rate limit; update entity, request file + token handshake, status-file polling; (Phase 4) `flirc_util` teach flow. |
| `scripts/audiohub-update` | `--status-file` (echoes the request token); read auto-update/channel from the hub user's `settings.json` with `unit.env` fallback. |
| `systemd/system/` | `audiohub-update-request.path`/`.service`, `audiohub-update-check.service`/`.timer`. |
| `install.sh` | Write `/etc/audiohub/hub-user`; tmpfiles entry; enable the new units; (Phase 4) optional `flirc_util` install + udev rule. |
| `unit.env.example` | Comments per §5; `AUDIOHUB_IR_REPEAT_INTERVAL`. |
| `scripts/validate.sh` | Checks: every discovery config has `entity_category` where expected; no retained Layer 1 IR config topics; settings file parses if present; path unit active. |
| `README.md` / howto | "Configure from Home Assistant" section; IR section rewritten around the editor and event entity. |
| `tests/` | SettingsStore precedence/clamping/reset; v1→v2 map migration incl. `ignore`→`none`, factory tombstones, and the retirement sweep covering dropped v1 entries; editor flows (select key → action state, None unbinds, identify capture, timeout); repeat limiter timing; event payloads and `event_types` growth; Layer 1 retirement topics; update status parsing incl. the token handshake. Layer 1 tests kept where internals are unchanged. |
| `tests/run.sh` | Run the new test modules (Layer 1's `test_ir_mapping.py` was never wired into the runner — fixed in Phase 1). |

## 7. Validation plan

1. **Device page layout:** open the device in HA. Controls / Configuration /
   Diagnostic sections match §4.1; ✱ entities are present but disabled.
2. **Settings round-trip:** change Duck Level to 40 %. Ducking applies the
   new level on the next cycle without a restart, `settings.json` contains
   it, and the value survives a hubd restart and a reboot. Reset Settings
   returns it to the `unit.env` value.
3. **Persistence of ducking switch:** turn Audio Ducking off, reboot, and
   confirm it is still off.
4. **IR editor:** select Mute in IR: Key → IR: Action shows "Mute toggle";
   set it to Ducking toggle; the remote's mute button now toggles ducking;
   set it to None; the button does nothing locally and IR Remote still logs
   the event.
5. **Identify:** press Identify, press an unused button that the FLIRC
   already knows; IR: Key jumps to it; assign Volume up; it works.
6. **Migration:** on a unit with a Layer 1 `ir_map.json` and retained
   per-key selects, start the new hubd: old entities disappear from HA,
   mappings are preserved, the file is v2 — including a v1 entry with an
   unknown action (dropped by migration, still swept from the broker).
7. **Repeat:** hold Vol+ for 2 s; the volume rises in steady steps of about
   7 per second (150 ms interval), not 30, and HA shows the final value
   within 2 s.
8. **Event automation:** create an automation on IR Remote `KEY_PLAYPAUSE`
   → `media_player.media_play_pause` (Music Assistant); press the button;
   music pauses.
9. **Update entity:** with a newer release published, the entity shows it
   in Settings → Updates; Install runs the updater as root (journal), the
   unit restarts its audio stack, and the entity reports the new installed
   version.
10. **Update status race:** while an install requested from HA is running,
    the entity must stay `in_progress` even though the daily-check status
    file from the previous day still exists (token handshake, §4.7).
11. **Regression:** run `tests/run.sh`; volumes, ducking engage and
    release, TV / BT / Sendspin audio, and the device-selection selects all
    behave as before.

## 8. Phasing

| Phase | Scope | Depends on |
|---|---|---|
| 1 | Entity categories, `_device_block()`, `SettingsStore`, settings entities, persisted ducking switch, repeat limiter | — |
| 2 | IR editor, map v2 + migration, Layer 1 retirement, IR Remote event entity | 1 |
| 3 | Update entity, request path unit + token handshake, `--status-file`, daily check timer | 1 |
| 4 | FLIRC Teach New Button | 2 + §4.8 verification |

Phases 1 and 2 land as separate commits on `feat/ir-remap`; the Pi deploys
once Phase 2 is in, so the Layer 1 IR surface never runs in production and
the interim double-IR page exists only between commits, not on hardware.
Phase 3 is independent of IR and deploys separately.

## 9. Decisions (formerly open questions)

1. **Remove the IR Last Key sensor** in favour of the IR Remote event entity
   — **DECIDED: yes** (2026-10-15). The event entity shows the same last-key
   state plus history.
2. **Mapped keys also fire HA events** — **DECIDED: yes** (2026-10-15). The
   `action` field in the payload makes filtering trivial; the double-action
   hazard is opt-in by the automation author.
3. **Device name from HA** — **DECIDED: defer** (2026-10-15). Entity-rename
   churn, Bluetooth alias privileges, and Music Assistant treating a renamed
   player as new outweigh the value; stays in `unit.env`.
4. **Allow Install from HA** — **DECIDED: yes** (2026-10-15), with the
   request-token handshake from §4.7. HA's update card is an explicit,
   deliberate action, and the weekly timer already restarts audio
   unattended.
5. **Default repeat interval 150 ms** — **DECIDED: 150 ms** (2026-10-15);
   tune on hardware.
