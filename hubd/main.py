#!/usr/bin/env python3
"""
hubd - Audio Hub Controller Daemon

Single daemon combining ducking, MQTT/Home Assistant, IR remote, and status
reporting for the Raspberry Pi audio hub.

Threading model:
    - The asyncio event loop owns MQTT command dispatch, all volume/mute
      changes, and the AudioControl Pulse connection.
    - A worker thread runs the ducking engine (own Pulse connection, polling
      every poll_interval seconds).
    - A worker thread runs the IR listener (blocking select on the evdev fd).
    Cross-thread work is marshalled onto the loop with
    loop.call_soon_threadsafe; paho-mqtt's network thread only touches paho
    and posts handlers onto the loop. Shutdown: SIGTERM/SIGINT set a stop
    event; every loop polls it, so the process exits promptly.

Usage:
    hubd [--config CONFIG]

Environment:
    AUDIOHUB_CONFIG - Path to unit configuration file (default: /etc/audiohub/unit.env)
"""

import argparse
import asyncio
import glob
import json
import logging
import os
import re
import secrets
import select
import signal
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

try:
    import evdev
    import paho.mqtt.client as mqtt
    from pulsectl import Pulse
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: apt install python3-evdev python3-paho-mqtt python3-pulsectl")
    sys.exit(1)


# -----------------------------------------------------------------------------
# Logging
# -----------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
)
log = logging.getLogger("hubd")

# Installed-version source for HA discovery (sw_version / origin block). Read
# defensively: a missing file (development machines, tests) reports "dev".
AUDIOHUB_VERSION_PATH = "/etc/audiohub/version"


def audiohub_version() -> str:
    try:
        with open(AUDIOHUB_VERSION_PATH) as f:
            version = f.read().strip()
        if version:
            return version
    except Exception:
        pass
    return "dev"


# Update handshake paths (spec Section 4.7). /run/audiohub is created at boot
# by systemd-tmpfiles (installer-written /etc/tmpfiles.d/audiohub.conf) with
# hub-user ownership: the unprivileged daemon drops request files there and
# reads the status file the root updater writes back. Overridable for tests.
UPDATE_REQUEST_PATH = "/run/audiohub/update-request"
UPDATE_STATUS_PATH = "/run/audiohub/update-status.json"
RELEASE_URL_BASE = "https://github.com/imonlinux/audio-hub/releases/tag/"


# -----------------------------------------------------------------------------
# Configuration
# -----------------------------------------------------------------------------
@dataclass
class HubConfig:
    """Configuration loaded from unit.env file."""

    # Device identity
    hostname: str
    device_id: str
    device_name: str

    # MQTT
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str
    mqtt_password: str
    mqtt_base_topic: str = "homeassistant"

    # Audio
    lineout_sink_pattern: str = "Built-in Audio Stereo"
    bus_tv: str = "bus.tv"
    bus_bt: str = "bus.bt"
    bus_music: str = "bus.music"
    duct_tv: str = "duct.tv"
    duct_bt: str = "duct.bt"
    tv_source_pattern: str = "UR23"
    tv_probe_interval: float = 10.0
    tv_probe_threshold: float = 100.0
    # Once the probe hears audio, keep the sensor ON this long without a
    # confirming probe, so quiet passages of real content don't flicker it
    tv_sensor_hold: float = 30.0

    # Ducking
    ducking_enabled: bool = True
    duck_level: float = 0.20
    duck_restore_delay: float = 2.0
    poll_interval: float = 0.5
    # Sendspin start/stop hook flag file (optional). When its parent dir
    # exists it authoritatively drives ducking: the hook distinguishes
    # playing from paused/stopped, which the stream alone cannot (Sendspin
    # keeps its stream open while paused). Empty = stream-based detection.
    music_flag_path: str = ""

    # IR Remote
    ir_device_path: str = "/dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd"
    ir_volume_step: float = 0.03
    # Hold-to-repeat: EV_KEY value-2 auto-repeat events step volume (only)
    # while the button is held. False restores strict press-only behavior.
    ir_repeat: bool = True
    # Minimum spacing between dispatched auto-repeat steps per key, in
    # milliseconds (the kernel delivers value-2 events at ~30 Hz).
    ir_repeat_interval_ms: int = 150

    # Update preferences (Section 4.7). unit.env is the fleet default;
    # both are overridden at runtime from Home Assistant (settings.json)
    # and read back from there by the root updater.
    auto_update: bool = False
    release_channel: str = "stable"

    @classmethod
    def from_env(cls, path: str = "/etc/audiohub/unit.env") -> "HubConfig":
        """Load configuration from environment file."""
        props = {}
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    props[key.strip()] = value.strip().strip('"')

        def get_bool(key: str, default: bool = False) -> bool:
            return props.get(key, str(default)).lower() in ("1", "true", "yes", "on")

        def get_float(key: str, default: float) -> float:
            return float(props.get(key, str(default)))

        return cls(
            hostname=props.get("AUDIOHUB_HOSTNAME", "audiohub"),
            device_id=props.get("AUDIOHUB_DEVICE_ID", "audio_hub"),
            device_name=props.get("AUDIOHUB_DEVICE_NAME", "Audio Hub"),
            mqtt_host=props.get("AUDIOHUB_MQTT_HOST", "192.168.0.100"),
            mqtt_port=int(props.get("AUDIOHUB_MQTT_PORT", "1883")),
            mqtt_username=props.get("AUDIOHUB_MQTT_USERNAME", "mqtt"),
            mqtt_password=props.get("AUDIOHUB_MQTT_PASSWORD", ""),
            mqtt_base_topic=props.get("AUDIOHUB_MQTT_BASE_TOPIC", "homeassistant"),
            lineout_sink_pattern=props.get("AUDIOHUB_LINEOUT_PATTERN", "Built-in Audio Stereo"),
            bus_tv=props.get("AUDIOHUB_BUS_TV", "bus.tv"),
            bus_bt=props.get("AUDIOHUB_BUS_BT", "bus.bt"),
            bus_music=props.get("AUDIOHUB_BUS_MUSIC", "bus.music"),
            duct_tv=props.get("AUDIOHUB_DUCT_TV", "duct.tv"),
            duct_bt=props.get("AUDIOHUB_DUCT_BT", "duct.bt"),
            tv_source_pattern=props.get("AUDIOHUB_TV_SOURCE_PATTERN", "UR23"),
            tv_probe_interval=get_float("AUDIOHUB_TV_PROBE_INTERVAL", 10.0),
            tv_probe_threshold=get_float("AUDIOHUB_TV_PROBE_THRESHOLD", 100.0),
            tv_sensor_hold=get_float("AUDIOHUB_TV_SENSOR_HOLD", 30.0),
            ducking_enabled=get_bool("AUDIOHUB_DUCKING_ENABLED", True),
            duck_level=get_float("AUDIOHUB_DUCK_LEVEL", 0.20),
            duck_restore_delay=get_float("AUDIOHUB_DUCK_RESTORE_DELAY", 2.0),
            poll_interval=get_float("AUDIOHUB_POLL_INTERVAL", 0.5),
            music_flag_path=props.get("AUDIOHUB_MUSIC_FLAG", ""),
            ir_device_path=props.get("AUDIOHUB_IR_DEVICE", "/dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd"),
            ir_volume_step=get_float("AUDIOHUB_IR_VOLUME_STEP", 0.03),
            ir_repeat=get_bool("AUDIOHUB_IR_REPEAT", True),
            ir_repeat_interval_ms=int(get_float("AUDIOHUB_IR_REPEAT_INTERVAL", 150)),
            auto_update=get_bool("AUDIOHUB_AUTO_UPDATE", False),
            release_channel=props.get("AUDIOHUB_RELEASE_CHANNEL", "stable"),
        )


# -----------------------------------------------------------------------------
# Device Selection (runtime output / TV-source routing)
# -----------------------------------------------------------------------------
# ALSA node names carry a trailing profile component that is not stable
# (...analog-stereo vs ...stereo-fallback on the UR23, profile transitions on
# built-in outputs). Selections therefore use the STABLE PREFIX: the node name
# with the final dot-component stripped. Applying a selection resolves the
# prefix against the live nodes; exact match first, then the longest live node.

TV_CAPTURE_NODES = ("loopback.tv.capture", "loopback.tv2.capture")
DUCT_PLAYBACK_NODES = ("duct.tv.playback", "duct.bt.playback", "duct.music.playback")


def node_prefix(node_name: str) -> str:
    """Stable selection key: node name minus the trailing profile component.
    A name with no room for a profile (fewer than three dot-components) is
    returned unchanged rather than mangled."""
    head, sep, tail = node_name.rpartition(".")
    return head if sep and "." in head else node_name


def hardware_output_names(sink_names: list) -> list:
    """Hardware sinks selectable as the unit output. Excludes the virtual
    buses (no alsa_ prefix), PipeWire's auto_null placeholder, and BT
    speakers (ducking semantics and the single-stack guarantee are designed
    around wired output; revisit if a use case appears)."""
    return sorted(
        n for n in sink_names
        if n.startswith("alsa_output.")
        and not n.startswith("auto_null.")
        and not n.startswith("bluez_sink.")
    )


def hardware_capture_names(source_names: list) -> list:
    """Hardware capture nodes selectable as the TV source. All hardware
    captures are listed (S/PDIF-capability filtering would add device-model
    coupling for no real-world unit); any sink monitor is excluded."""
    return sorted(
        n for n in source_names
        if n.startswith("alsa_input.")
        and not n.endswith(".monitor")
    )


def build_options(node_names: list) -> list:
    """Selection options from live node names: stable prefixes, except a
    prefix shared by multiple live nodes (two identical dongles) falls back
    to full node names so two devices are never silently merged."""
    by_prefix: dict = {}
    for n in node_names:
        by_prefix.setdefault(node_prefix(n), []).append(n)
    options = []
    for prefix, names in sorted(by_prefix.items()):
        if len(names) == 1:
            options.append(prefix)
        else:
            options.extend(sorted(names))
    return sorted(options)


def resolve_option(option: str, node_names: list) -> "str | None":
    """Resolve a selection option to a live node name: exact match first,
    else the longest live node carrying the option as a prefix."""
    if option in node_names:
        return option
    matches = [n for n in node_names if n.startswith(option + ".")]
    if not matches:
        return None
    return max(matches, key=len)


class SelectionStore:
    """Desired output / TV-source selection, persisted to
    ~/.config/audiohub/selection.json (hubd runs unprivileged; /etc/audiohub
    stays root-owned). Values are selection options (stable prefixes, or full
    node names in the duplicate-device case) or None = factory default from
    unit.env. Thread-safe: shared between the asyncio loop (commands,
    reconciler) and the ducking thread (TV probe)."""

    KEYS = ("output", "tv_source")

    def __init__(self, path: "str | None" = None):
        self.path = path or os.path.expanduser(
            os.path.join("~", ".config", "audiohub", "selection.json"))
        self._lock = threading.Lock()
        self._desired: dict = {k: None for k in self.KEYS}
        self._load()

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
            for k in self.KEYS:
                v = data.get(k)
                if isinstance(v, str) and v:
                    self._desired[k] = v
        except FileNotFoundError:
            pass
        except Exception as e:
            log.warning(f"Ignoring unreadable selection store {self.path}: {e}")

    def get(self, key: str) -> "str | None":
        with self._lock:
            return self._desired.get(key)

    def set(self, key: str, option: str):
        with self._lock:
            self._desired[key] = option
            snapshot = dict(self._desired)
        self._persist(snapshot)

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._desired)

    def _persist(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(snapshot, f, indent=2)
            os.replace(tmp, self.path)
        except Exception as e:
            # Selection stays effective for this session; the store is
            # re-created on the next accepted command.
            log.warning(f"Could not persist selection store: {e}")


# -----------------------------------------------------------------------------
# Runtime Settings
# -----------------------------------------------------------------------------
# Runtime-adjustable settings (spec Section 4.2), edited from Home Assistant
# and persisted to ~/.config/audiohub/settings.json. Precedence per key:
# settings.json (values the user changed) > unit.env fleet default > built-in
# default. unit.env and the built-in default both live in HubConfig, so the
# fall-through is a plain attribute read.

SETTING_DEFS = {
    # key: kind + validation surface. "percent" is stored 0.0-1.0 and
    # travels over MQTT as 0-100.
    "ducking_enabled":       {"kind": "bool"},
    "duck_level":            {"kind": "percent", "min": 0.0, "max": 1.0},
    "duck_restore_delay":    {"kind": "float", "min": 0.0, "max": 30.0},
    "ir_volume_step":        {"kind": "percent", "min": 0.01, "max": 0.10},
    "ir_repeat":             {"kind": "bool"},
    "ir_repeat_interval_ms": {"kind": "int", "min": 100, "max": 1000},
    "tv_probe_threshold":    {"kind": "float", "min": 10.0, "max": 5000.0},
    "tv_probe_interval":     {"kind": "float", "min": 2.0, "max": 60.0},
    "auto_update":           {"kind": "bool"},
    "release_channel":       {"kind": "select", "options": ("stable", "canary")},
}
# Percent-kind settings: MQTT payload = stored value * 100.
SETTING_MQTT_PERCENT = ("duck_level", "ir_volume_step")


def _clamp(value, lo, hi):
    return max(lo, min(hi, value))


class SettingsStore:
    """Runtime settings, modelled on SelectionStore: atomic persist
    (`.tmp` + os.replace), thread-safe (the ducking thread reads duck level
    and release delay each poll). The file holds only keys changed from HA;
    absent keys fall through to the HubConfig bootstrap value, keeping
    unit.env meaningful as the fleet default and making Reset a value wipe."""

    def __init__(self, config: HubConfig, path: "str | None" = None):
        self.cfg = config
        self.path = path or os.path.expanduser(
            os.path.join("~", ".config", "audiohub", "settings.json"))
        self._lock = threading.Lock()
        self._values: dict = {}
        self._load()

    def _coerce(self, key: str, value):
        """Validate/clamp one value against its definition. Returns the
        normalized value, or None when it is unusable."""
        d = SETTING_DEFS[key]
        kind = d["kind"]
        try:
            if kind == "bool":
                return bool(value)
            if kind in ("percent", "float"):
                return _clamp(float(value), d["min"], d["max"])
            if kind == "int":
                return int(_clamp(float(value), d["min"], d["max"]))
            if kind == "select":
                return value if value in d["options"] else None
        except (TypeError, ValueError):
            pass
        return None

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning(f"Ignoring unreadable settings store {self.path}: {e}")
            return
        values = data.get("values") if isinstance(data, dict) else None
        if not isinstance(values, dict):
            log.warning(f"Ignoring malformed settings store {self.path}")
            return
        with self._lock:
            for key, value in values.items():
                if key not in SETTING_DEFS:
                    log.warning(f"Settings: dropping unknown key {key!r}")
                    continue
                v = self._coerce(key, value)
                if v is None:
                    log.warning(
                        f"Settings: dropping {key!r}: invalid value {value!r}")
                    continue
                self._values[key] = v

    def get(self, key: str):
        """Resolve the precedence chain: user override, else the bootstrap
        value (unit.env entry, else the built-in default)."""
        with self._lock:
            if key in self._values:
                return self._values[key]
        return getattr(self.cfg, key)

    def set(self, key: str, value):
        """Store a user override (validated and clamped). Raises ValueError
        when no coercion can rescue the value (e.g. a bad select option).
        Returns the stored (normalized) value."""
        v = self._coerce(key, value)
        if v is None:
            raise ValueError(f"invalid value for {key}: {value!r}")
        with self._lock:
            self._values[key] = v
            snapshot = dict(self._values)
        self._persist(snapshot)
        return v

    def overrides(self) -> dict:
        with self._lock:
            return dict(self._values)

    def reset(self):
        """Wipe all user overrides; every setting falls through to the
        bootstrap value."""
        with self._lock:
            self._values = {}
        self._persist({})

    def _persist(self, snapshot: dict):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 1, "values": snapshot}, f,
                          indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning(f"Could not persist settings store: {e}")


# -----------------------------------------------------------------------------
# IR Key Mapping
# -----------------------------------------------------------------------------
# Key-to-action mapping is data, not code: persisted at
# ~/.config/audiohub/ir_map.json (v2 format, spec Section 4.4), editable from
# HA via the IR editor, surviving restarts. The factory map (absent file) is
# the out-of-the-box behavior. "none" in the bindings is an explicit
# tombstone: an unbound factory key must not be re-added by the overlay.

IR_ACTIONS = ("volume_up", "volume_down", "mute_toggle", "ducking_toggle", "ignore")
# Actions that repeat while a key is held (EV_KEY value-2), Section 5.4.
IR_REPEATABLE = ("volume_up", "volume_down")
# Identify-capture window: how long an armed identify waits for a keypress.
IR_IDENTIFY_WINDOW_S = 60.0
# After a terminal identify result, the status sensor returns to idle after
# this long.
IR_IDENTIFY_IDLE_RETURN_S = 10.0

IR_FACTORY_MAP = {
    "KEY_VOLUMEUP": "volume_up",
    "KEY_VOLUMEDOWN": "volume_down",
    "KEY_MUTE": "mute_toggle",
}

IR_KEY_LABELS = {
    "KEY_VOLUMEUP": "Volume Up",
    "KEY_VOLUMEDOWN": "Volume Down",
    "KEY_MUTE": "Mute",
}


def ir_key_label(key_name: str) -> str:
    """Human label for the editor's Key select: table for common keys,
    otherwise the name with KEY_ stripped and title-cased (KEY_F13 -> F13)."""
    if key_name in IR_KEY_LABELS:
        return IR_KEY_LABELS[key_name]
    if key_name.startswith("KEY_"):
        return key_name[len("KEY_"):].title()
    return key_name


def ir_key_option(key_name: str) -> str:
    """Select option text: human label plus the canonical name in parentheses,
    so it reads well and round-trips (Section 4.4)."""
    return f"{ir_key_label(key_name)} ({key_name})"


def ir_parse_key_option(option: str) -> "str | None":
    """Canonical KEY_* name from a select option (text inside the final
    parentheses); None if the name is not a real evdev key."""
    m = re.search(r"\(([^()]*)\)\s*$", option)
    name = m.group(1) if m else option.strip()
    code = getattr(evdev.ecodes, name, None)
    return name if isinstance(code, int) else None


# Editor Action select: option label -> action id ("none" = unbound tombstone;
# the editor cannot set "ignore", which remains valid only in hand-edited
# files and is displayed as none).
IR_ACTION_LABELS = {
    "volume_up": "Volume up",
    "volume_down": "Volume down",
    "mute_toggle": "Mute toggle",
    "ducking_toggle": "Ducking toggle",
    "none": "None (HA event only)",
}
IR_ACTION_BY_OPTION = {v: k for k, v in IR_ACTION_LABELS.items()}
# Binding values that leave a key inert (event-only).
IR_INERT = ("none", "ignore")


# input-event-codes.h sentinels that share a code with a real key and would
# otherwise win the reverse map (KEY_MIN_INTERESTING == KEY_MUTE, KEY_MAX is
# a range bound): not real keys, excluded so presses translate to canonical
# names that round-trip through the map file and HA topics.
_ECODE_SENTINELS = {"KEY_MAX", "KEY_MIN_INTERESTING"}


def _ecode_to_key_name() -> dict:
    """Reverse map evdev key code -> canonical KEY_* name (first name wins,
    iterated in sorted order so the result is deterministic; several names
    can share one code)."""
    names: dict = {}
    for name in sorted(dir(evdev.ecodes)):
        if not name.startswith("KEY_") or name in _ECODE_SENTINELS:
            continue
        value = getattr(evdev.ecodes, name)
        if isinstance(value, int) and value not in names:
            names[value] = name
    return names


ECODE_TO_KEY_NAME = _ecode_to_key_name()


class IRMapStore:
    """IR key-to-action map, persisted to ~/.config/audiohub/ir_map.json
    (same arrangement as SelectionStore: hubd runs unprivileged; thread-safe
    across the asyncio loop and the IR worker thread).

    v2 file format (spec Section 4.4):
        {"version": 2,
         "bindings": {KEY: action_id | "none"},
         "seen": [KEY, ...]}          most recent first, capped at 32

    "none" is an explicit tombstone: an unbound factory key stays unbound
    instead of being re-added by the factory overlay. A v1 flat dict (any
    file without "version": 2) loads as bindings, with v1 "ignore" migrated
    to "none"; every key name in a v1 file is kept in _legacy_keys BEFORE
    validation drops malformed entries, so the Layer 1 retirement sweep
    (Section 4.4) can clear entities for dropped entries too. The file is
    rewritten as v2 on the next change, never on load."""

    SEEN_CAP = 32

    def __init__(self, path: "str | None" = None):
        self.path = path or os.path.expanduser(
            os.path.join("~", ".config", "audiohub", "ir_map.json"))
        self._lock = threading.Lock()
        self._bindings: dict = {}       # file bindings: action id or "none"
        self._seen: list = []           # most recent first
        self._legacy_keys: set = set()  # every key name from a pre-migration v1 file
        self._load()

    @staticmethod
    def _valid_key(key) -> bool:
        code = getattr(evdev.ecodes, key, None) if isinstance(key, str) else None
        return isinstance(code, int)

    def _load(self):
        try:
            with open(self.path) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning(f"Ignoring unreadable IR map {self.path}: {e}")
            return
        if not isinstance(data, dict):
            log.warning(f"Ignoring malformed IR map {self.path}: not an object")
            return
        if data.get("version") == 2:
            bindings = data.get("bindings", {})
            seen = data.get("seen", [])
            if not isinstance(bindings, dict):
                bindings = {}
            if not isinstance(seen, list):
                seen = []
        else:
            # v1 flat dict (or unknown format): the whole object is bindings.
            bindings = data
            seen = []
            self._legacy_keys = {k for k in data if isinstance(k, str)}
        with self._lock:
            for key, action in bindings.items():
                if not self._valid_key(key):
                    log.warning(f"IR map: dropping unknown key {key!r}")
                    continue
                if action == "ignore":
                    action = "none"  # v1 migration: ignore -> explicit tombstone
                if action != "none" and action not in IR_ACTIONS:
                    log.warning(f"IR map: dropping {key!r}: unknown action {action!r}")
                    continue
                self._bindings[key] = action
            for key in seen:
                if self._valid_key(key) and key not in self._seen:
                    self._seen.append(key)
            del self._seen[self.SEEN_CAP:]

    def get(self, key_name: str) -> "str | None":
        """Effective binding: file entry first (including "none" tombstones,
        which block the factory overlay), then the factory map, else None."""
        with self._lock:
            if key_name in self._bindings:
                return self._bindings[key_name]
        return IR_FACTORY_MAP.get(key_name)

    def set(self, key_name: str, action: str):
        """Record a binding. action is an IR_ACTIONS id or "none" (unbind
        tombstone). Persists in v2 format."""
        if action != "none" and action not in IR_ACTIONS:
            raise ValueError(f"unknown IR action {action!r}")
        with self._lock:
            self._bindings[key_name] = action
            snapshot = (dict(self._bindings), list(self._seen))
        self._persist(*snapshot)

    def record_seen(self, key_name: str) -> bool:
        """Remember a key seen from the remote (most recent first, capped).
        Invalid key names are refused so the seen list always reloads
        losslessly. Returns True if this key was not seen before."""
        if not self._valid_key(key_name):
            return False
        with self._lock:
            if key_name in self._seen:
                return False
            self._seen.insert(0, key_name)
            del self._seen[self.SEEN_CAP:]
            snapshot = (dict(self._bindings), list(self._seen))
        self._persist(*snapshot)
        return True

    def seen(self) -> list:
        with self._lock:
            return list(self._seen)

    def bindings(self) -> dict:
        """Raw file bindings (action ids and "none" tombstones)."""
        with self._lock:
            return dict(self._bindings)

    def legacy_keys(self) -> set:
        with self._lock:
            return set(self._legacy_keys)

    def mapped(self) -> dict:
        """Effective bindings that carry a real action (tombstones, ignore,
        and never-bound keys excluded) — the IR Mappings sensor view."""
        out = {}
        for key in set(IR_FACTORY_MAP) | set(self._bindings):
            action = self.get(key)
            if action is not None and action not in IR_INERT:
                out[key] = action
        return out

    def retirement_keys(self) -> set:
        """Every key that may have an orphaned Layer 1 per-key select entity:
        v2 file keys (bindings + seen), pre-migration v1 keys (including
        entries validation dropped), and the factory keys."""
        with self._lock:
            keys = set(self._bindings) | set(self._seen) | set(self._legacy_keys)
        return keys | set(IR_FACTORY_MAP)

    def _persist(self, bindings: dict, seen: list):
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"version": 2, "bindings": bindings, "seen": seen},
                          f, indent=2, sort_keys=True)
            os.replace(tmp, self.path)
        except Exception as e:
            # Mapping stays effective for this session; the store is
            # re-created on the next accepted command.
            log.warning(f"Could not persist IR map store: {e}")


# -----------------------------------------------------------------------------
# Ducking Engine
# -----------------------------------------------------------------------------
class DuckingEngine:
    """
    Ducking + activity monitor. Runs in a worker thread with its own
    persistent Pulse connection, polling every poll_interval seconds.

    Trigger: any non-corked sink-input routed to bus.music.
    Action:  duck level applied to the duct.tv / duct.bt loopback playback
             streams (sink-inputs whose node.name is duct.tv.playback /
             duct.bt.playback), which carry TV/BT audio into the hardware
             sink. Source bus volumes are untouched, so ducking composes
             cleanly with per-source volume set via MQTT/HA.
    Restore: after duck_restore_delay of continuous inactivity, so track
             gaps do not cause volume flapping.

    Detection is sink-based: any uncorked stream playing into the music bus
    counts as music, regardless of client naming. A paused-but-open stream
    (Sendspin keeps one) reads as active, so ducking holds until the music
    stream actually stops or corks — per spec, ducking restores when the
    music source stops.

    Polling instead of pulsectl event_listen: event_listen combined with
    volume_set segfaults libpulse, and get_peak_sample leaks one fd per call
    in pipewire-pulse. List queries leak nothing, so a plain poll of
    sink_list()/sink_input_list() is the safe building block.
    """

    # Loopback playback node names that carry TV / BT audio to the hardware
    # sink — these (and only these) are attenuated when ducking.
    DUCT_NODES = ("duct.tv.playback", "duct.bt.playback")

    def __init__(self, config: HubConfig, loop: asyncio.AbstractEventLoop,
                 stop_event: threading.Event,
                 selection: "SelectionStore | None" = None,
                 settings: "SettingsStore | None" = None):
        self.cfg = config
        self.loop = loop
        self.stop_event = stop_event
        self.selection = selection
        self.settings = settings
        self.ducking_enabled = config.ducking_enabled
        self.is_ducked = False
        self._pending_enable: bool | None = None
        self._inactive_since: float | None = None
        self._pulse = None
        self._on_change = None
        self._tv_probe_at: float | None = None
        self._tv_cached = False
        self._tv_hold_until = 0.0
        self._tv_probe_at: float | None = None
        self._tv_cached = False
        # Last published status snapshot; published whenever it changes.
        self.state = {
            "ducking_enabled": config.ducking_enabled,
            "ducked": False,
            "music": False,
            "bt": False,
            "tv": False,
        }

    def on_change(self, callback):
        """Register callback(state: dict), invoked on the asyncio loop."""
        self._on_change = callback

    def _setting(self, key: str):
        """Runtime setting with bootstrap fall-through: the settings store
        when one is attached, else the unit.env / built-in value. Called on
        the ducking thread each poll, so live HA changes apply within one
        poll interval."""
        if self.settings is not None:
            return self.settings.get(key)
        return getattr(self.cfg, key)

    def set_enabled(self, enabled: bool):
        """Request enable/disable ducking (thread-safe; applied by the engine
        thread on its next poll — never touch _pulse from another thread)."""
        self._pending_enable = bool(enabled)

    def snapshot(self) -> dict:
        """Current status snapshot (dict copy)."""
        return dict(self.state)

    # -- worker thread --------------------------------------------------------
    def _drop(self):
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                pass
        self._pulse = None

    def _connect(self) -> bool:
        """(Re)establish the Pulse connection, retrying until stopped."""
        self._drop()
        while not self.stop_event.is_set():
            try:
                self._pulse = Pulse("hubd-ducking")
                self._set_duct_level(1.0)
                self.is_ducked = False
                self._inactive_since = None
                log.info("Ducking engine connected; ducts normalized")
                return True
            except Exception as e:
                log.warning(f"Waiting for PulseAudio/PipeWire: {e}")
                self.stop_event.wait(3.0)
        return False

    def _alive(self) -> bool:
        """Round-trip probe: sink_list() on a stale context silently returns
        nothing, so liveness must be checked explicitly."""
        if self._pulse is None:
            return False
        try:
            self._pulse.server_info()
            return True
        except Exception:
            return False

    def _run_sync(self):
        log.info("Ducking engine starting")
        if not self._connect():
            return
        while not self.stop_event.is_set():
            if not self._alive():
                log.warning("Ducking engine lost its Pulse connection; reconnecting")
                if not self._connect():
                    return
                continue
            try:
                self._handle_pending()
                self._check()
                self._verify_duct_levels()
                self._update_status()
            except Exception as e:
                log.debug(f"ducking poll error: {e}")
            self.stop_event.wait(self.cfg.poll_interval)
        self._drop()
        log.info("Ducking engine stopped")

    def _verify_duct_levels(self):
        """Re-assert the expected duct level if anything else changed it.

        Duct volumes are hubd's exclusive domain, but WirePlumber's stream
        state can restore stale values onto the recreated duct streams at
        boot (observed: June-era ducked volumes resurfacing after reboot).
        The poll loop compares actual vs expected and corrects drift."""
        expected = self._cubic(self._setting("duck_level") if self.is_ducked else 1.0)
        try:
            for si in self._pulse.sink_input_list():
                node = si.proplist.get("node.name", "")
                if node not in self.DUCT_NODES:
                    continue
                cur = si.volume.values[0] if si.volume.values else None
                if cur is not None and abs(cur - expected) > 0.02:
                    v = si.volume
                    for i in range(len(v.values)):
                        v.values[i] = expected
                    self._pulse.sink_input_volume_set(si.index, v)
                    log.info(f"Re-asserted {node} to {expected:.2f} (was {cur:.2f})")
        except Exception as e:
            log.debug(f"duct verify error: {e}")

    def _handle_pending(self):
        if self._pending_enable is None:
            return
        self.ducking_enabled = bool(self._pending_enable)
        self._pending_enable = None
        self._inactive_since = None
        log.info(f"Ducking {'enabled' if self.ducking_enabled else 'disabled'}")
        if not self.ducking_enabled and self.is_ducked:
            self._apply(False)

    # -- detection ------------------------------------------------------------
    def _find_sink(self, name: str):
        for s in self._pulse.sink_list():
            if s.name == name:
                return s
        return None

    def _has_uncorked_input(self, sink) -> bool:
        for si in self._pulse.sink_input_list():
            if si.sink == sink.index and not si.corked:
                return True
        return False

    def _music_active(self) -> bool:
        """Music is active when Sendspin's start/stop hook flag file exists
        AND its stream is still open.

        The hook distinguishes playing from paused/stopped, which the stream
        alone cannot: Sendspin keeps its stream open (uncorked) while paused.
        Requiring BOTH covers a sendspin crash mid-song: the flag would
        remain with no stream, and AND-ing unducks instead of latching.
        Hook mode engages once the flag's parent directory exists (the hook
        script creates it on first playback); without it we fall back to
        stream-based detection: any uncorked sink-input on bus.music.
        """
        flag = self.cfg.music_flag_path or os.path.join(
            os.environ.get("XDG_RUNTIME_DIR", ""), "audiohub", "music-playing")
        if os.path.isdir(os.path.dirname(flag)):
            if not os.path.isfile(flag):
                return False
            try:
                bus = self._find_sink(self.cfg.bus_music)
                return bus is not None and self._has_uncorked_input(bus)
            except Exception as e:
                log.debug(f"music_active check failed: {e}")
                return False
        try:
            bus = self._find_sink(self.cfg.bus_music)
            if bus is None:
                return False
            return self._has_uncorked_input(bus)
        except Exception as e:
            log.debug(f"music_active check failed: {e}")
            return False

    def _bt_active(self) -> bool:
        try:
            bus = self._find_sink(self.cfg.bus_bt)
            return bus is not None and self._has_uncorked_input(bus)
        except Exception as e:
            log.debug(f"bt_active check failed: {e}")
            return False

    def _tv_active(self) -> bool:
        """TV Playing = the UR23 is receiving non-silent audio.

        This needs an actual level measurement: the UR23 free-runs its
        internal 48 kHz clock and streams digital silence whenever the
        capture is open (verified: TV unplugged from power still yields
        'Status: Running, Momentary freq = 48000' and an all-zero stream),
        so neither pulse state nor /proc status indicates signal presence.
        Probing is rate-limited to once per TV_PROBE_INTERVAL seconds and
        the ON state holds for TV_SENSOR_HOLD seconds after the last
        audible probe, so quiet passages don't flicker the sensor."""
        now = time.monotonic()
        if self._tv_probe_at is not None and now < self._tv_probe_at:
            return self._tv_cached or now < self._tv_hold_until
        self._tv_probe_at = now + self._setting("tv_probe_interval")
        on = self._probe_tv_level()
        if on:
            self._tv_hold_until = now + self.cfg.tv_sensor_hold
        self._tv_cached = on or now < self._tv_hold_until
        return self._tv_cached

    def _probe_tv_level(self) -> bool:
        """Record ~0.3 s of raw samples from the TV source and measure RMS.
        pw-record has no duration option, so use --raw on stdout with a
        short timeout: on SIGKILL the already-flushed bytes are still
        delivered via TimeoutExpired.stdout — enough for an RMS reading."""
        try:
            # Probe target follows the user's TV Source selection (same rule
            # as master volume tracking the selected output); the unit.env
            # pattern is the factory default used when none is in effect.
            target = None
            if self.selection is not None:
                desired = self.selection.get("tv_source")
                if desired:
                    captures = hardware_capture_names(
                        [s.name for s in self._pulse.source_list()])
                    target = resolve_option(desired, captures)
            if target is None:
                pat = self.cfg.tv_source_pattern.lower()
                for s in self._pulse.source_list():
                    if pat in (s.name or "").lower() or pat in (s.description or "").lower():
                        target = s.name
                        break
            if target is None:
                return False
            data = b""
            try:
                r = subprocess.run(
                    ["pw-record", "--raw", "--target", target, "--rate", "48000",
                     "--channels", "2", "--format", "s16", "-"],
                    capture_output=True, timeout=0.35)
                data = r.stdout or b""
            except subprocess.TimeoutExpired as e:
                data = e.stdout or b""
            if len(data) < 4096:
                return False
            samples = struct.unpack(f"<{len(data) // 2}h", data)
            rms = (sum(x * x for x in samples[::4]) / max(1, len(samples) // 4)) ** 0.5
            return rms > self._setting("tv_probe_threshold")
        except Exception as e:
            log.debug(f"tv level probe failed: {e}")
            return False

    # -- actuation ------------------------------------------------------------
    @staticmethod
    def _cubic(level: float) -> float:
        """Convert a perceptual level (1.0 = full, 0.2 = 20% amplitude) to
        the cubic value PulseAudio expects. Without this, duck_level=0.20
        means an amplitude of 0.008 (-42 dB) — effectively silence.
        (0.2 amplitude is -14 dB, which subjectively reads as roughly
        35-40% loudness.)"""
        if 0.0 < level < 1.0:
            return level ** (1.0 / 3.0)
        return max(0.0, min(1.0, level))

    def _set_duct_level(self, level: float):
        """Set all present duck-target duct streams to a volume level
        (level is perceptual loudness; converted to cubic here)."""
        if self._pulse is None:
            return []
        cubic = self._cubic(level)
        targets = []
        for si in self._pulse.sink_input_list():
            node = si.proplist.get("node.name", "")
            if node in self.DUCT_NODES:
                v = si.volume
                for i in range(len(v.values)):
                    v.values[i] = cubic
                self._pulse.sink_input_volume_set(si.index, v)
                targets.append(node)
        return targets

    def _apply(self, ducked: bool):
        """Apply duck/restore to the duct streams. If no duct streams exist
        yet (graph still assembling), leave state unchanged and retry on the
        next poll."""
        level = self._setting("duck_level") if ducked else 1.0
        targets = self._set_duct_level(level)
        if not targets:
            log.debug(f"No duct targets present yet for duck={ducked}; will retry")
            return
        self.is_ducked = ducked
        log.info(f"Ducking {'ON' if ducked else 'OFF'} level={level:.2f} targets={targets}")

    def _check(self):
        """Evaluate music state and apply/restore ducking (with hold-off)."""
        if not self.ducking_enabled:
            if self.is_ducked:
                self._apply(False)
            self._inactive_since = None
            return

        if self._music_active():
            self._inactive_since = None
            if not self.is_ducked:
                self._apply(True)
        elif self.is_ducked:
            now = time.monotonic()
            if self._inactive_since is None:
                self._inactive_since = now
            elif now - self._inactive_since >= self._setting("duck_restore_delay"):
                self._apply(False)
                self._inactive_since = None

    def _update_status(self):
        new = {
            "ducking_enabled": self.ducking_enabled,
            "ducked": self.is_ducked,
            "music": self._music_active(),
            "bt": self._bt_active(),
            "tv": self._tv_active(),
        }
        if new != self.state:
            self.state = new
            self._notify()

    def _notify(self):
        if self._on_change is None:
            return
        state = dict(self.state)
        try:
            self.loop.call_soon_threadsafe(self._on_change, state)
        except RuntimeError:
            pass


# -----------------------------------------------------------------------------
# MQTT Bridge
# -----------------------------------------------------------------------------
class MQTTBridge:
    """
    MQTT / Home Assistant integration bridge.

    Features:
    - HA MQTT discovery for all entities (availability wired to the LWT)
    - Per-source volume control (tv / bt / music), master volume + mute
    - Ducking enable switch + active indicator
    - Per-source activity binary sensors
    - LWT availability; discovery, availability and full state are
      re-published on every (re)connect
    - Tolerates broker downtime at startup (retry loop) and after
      connection loss (paho auto-reconnect)

    Command/state topic map:
        {id}/volume/set              master volume        (0.0 - 1.0)
        {id}/{tv|bt|music}/volume/set  per-source volume  (0.0 - 1.0)
        {id}/mute/set                master mute          (ON/OFF)
        {id}/ducking/set             ducking enable       (ON/OFF, persisted)
        {id}/setting/{key}/set       runtime setting (Section 4.3; key
                                     "reset" = Reset Settings button)
        {id}/output/set, {id}/tv_source/set   device selection
        {id}/ir_edit/key/set         IR editor: key select (Section 4.4)
        {id}/ir_edit/action/set      IR editor: action select
        {id}/ir_edit/identify/set    IR editor: identify button (PRESS)
        {id}/volume/state, {id}/{source}/volume/state, ...
        {id}/setting/{key}/state     state (retained)
        {id}/ir/event                IR Remote event entity (NOT retained)
    """

    def __init__(self, config: HubConfig, ir_map: "IRMapStore | None" = None,
                 settings: "SettingsStore | None" = None):
        self.cfg = config
        self.client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._volume_callbacks: dict[str, object] = {}
        self._mute_callback = None
        self._ducking_callback = None
        self._selection_callback = None
        self._setting_callback = None
        self._ir_edit_key_callback = None
        self._ir_edit_action_callback = None
        self._ir_edit_identify_callback = None
        self._update_install_callback = None
        self._state_provider = None
        self._selection_provider = None
        self._update_provider = None
        self._ir_map = ir_map
        self.settings = settings
        # Last known select options; updated by publish_selection_discovery.
        self._selection_options: dict = {"output": [], "tv_source": []}
        # Last published IR key-option list; publish_ir_editor_options skips
        # republishing while unchanged.
        self._ir_options_cache: "list | None" = None

    def on_volume_command(self, source: str, callback):
        """Register callback(level: float) for a source or 'master'."""
        self._volume_callbacks[source] = callback

    def on_mute_command(self, callback):
        """Register callback(muted: bool)."""
        self._mute_callback = callback

    def on_ducking_command(self, callback):
        """Register callback(enabled: bool)."""
        self._ducking_callback = callback

    def on_selection_command(self, callback):
        """Register callback(key: 'output'|'tv_source', option: str)."""
        self._selection_callback = callback

    def on_setting_command(self, callback):
        """Register callback(key: str, payload: str) for {id}/setting/{key}/set
        (key 'reset' carries the Reset Settings button press)."""
        self._setting_callback = callback

    def on_ir_edit_key(self, callback):
        """Register callback(option: str) for {id}/ir_edit/key/set."""
        self._ir_edit_key_callback = callback

    def on_ir_edit_action(self, callback):
        """Register callback(option: str) for {id}/ir_edit/action/set."""
        self._ir_edit_action_callback = callback

    def on_ir_edit_identify(self, callback):
        """Register callback() for {id}/ir_edit/identify/set (PRESS)."""
        self._ir_edit_identify_callback = callback

    def on_update_install(self, callback):
        """Register callback() for {id}/update/install (payload "install")."""
        self._update_install_callback = callback

    def attach_state_provider(self, provider):
        """Register fn() -> dict with current state, published on connect.
        Called on the asyncio loop (see _publish_initial_state)."""
        self._state_provider = provider

    def attach_selection_provider(self, provider):
        """Register fn() -> {"options": {...}, "effective": {...}} for the
        device-selection entities, gathered on the asyncio loop at connect."""
        self._selection_provider = provider

    def attach_update_provider(self, provider):
        """Register fn() that refreshes + publishes the Firmware update
        entity state (reads the updater's status file), gathered on the
        asyncio loop at connect and on every state-sync tick."""
        self._update_provider = provider

    def _availability_topic(self) -> str:
        return f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/state"

    def _device_block(self) -> dict:
        """Shared HA device registry block (was three inline copies)."""
        return {
            "identifiers": [self.cfg.device_id],
            "name": self.cfg.device_name,
            "model": "Raspberry Pi 4B",
            "manufacturer": "Custom Audio Hub",
            "sw_version": audiohub_version(),
        }

    def _origin(self) -> dict:
        return {"name": "audio-hub", "sw": audiohub_version()}

    async def run(self, stop_event: threading.Event):
        """Run the MQTT client until stop_event is set."""
        self._loop = asyncio.get_running_loop()
        host, port = self.cfg.mqtt_host, self.cfg.mqtt_port
        log.info(f"MQTT bridge connecting to {host}:{port}")

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"audio-hub-{self.cfg.device_id}",
            clean_session=True,
        )
        self.client.username_pw_set(self.cfg.mqtt_username, self.cfg.mqtt_password)
        self.client.will_set(self._availability_topic(), payload="offline", retain=True)
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_on_failure = True

        # Tolerate broker downtime at startup: retry until connected or stop.
        while not stop_event.is_set():
            try:
                await asyncio.to_thread(self.client.connect, host, port, 60)
                break
            except OSError as e:
                log.warning(f"MQTT connect to {host}:{port} failed: {e}; retrying in 5 s")
                await asyncio.sleep(5)
        if stop_event.is_set():
            return

        # loop_start() auto-reconnects after transient drops; on_connect
        # re-subscribes and re-publishes discovery/availability/state.
        self.client.loop_start()
        while not stop_event.is_set():
            await asyncio.sleep(1)

        try:
            self.client.disconnect()
            self.client.loop_stop()
        except Exception:
            pass
        log.info("MQTT bridge stopped")

    # -- paho network thread ----------------------------------------------------
    def _emit(self, fn):
        """Run fn on the asyncio loop (paho callbacks arrive off-loop)."""
        if self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(fn)
        except RuntimeError:
            pass

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        code = getattr(reason_code, "value", reason_code)
        if code != 0:
            log.error(f"MQTT connect refused: {reason_code}")
            return
        log.info("MQTT connected")

        client.subscribe(f"{self.cfg.device_id}/volume/set")
        for source in ("tv", "bt", "music"):
            client.subscribe(f"{self.cfg.device_id}/{source}/volume/set")
        client.subscribe(f"{self.cfg.device_id}/mute/set")
        client.subscribe(f"{self.cfg.device_id}/ducking/set")
        client.subscribe(f"{self.cfg.device_id}/output/set")
        client.subscribe(f"{self.cfg.device_id}/tv_source/set")
        client.subscribe(f"{self.cfg.device_id}/setting/+/set")
        client.subscribe(f"{self.cfg.device_id}/ir_edit/+/set")
        client.subscribe(f"{self.cfg.device_id}/update/install")

        client.publish(self._availability_topic(), "online", retain=True)
        self._publish_discovery(client)

        # Initial state must be gathered on the loop (AudioControl is not
        # thread-safe); on_connect runs on paho's network thread.
        self._emit(self._publish_initial_state)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties):
        log.warning(f"MQTT disconnected ({reason_code}); auto-reconnect in progress")

    def _on_message(self, client, userdata, msg):
        topic = msg.topic
        try:
            payload = msg.payload.decode("utf-8", "replace").strip()
        except Exception:
            return
        prefix = f"{self.cfg.device_id}/"
        if not topic.startswith(prefix):
            return
        parts = topic[len(prefix):].split("/")
        log.debug(f"MQTT recv {topic} = {payload}")

        try:
            # {id}/{source}/volume/set -> per-source volume
            if len(parts) == 3 and parts[1] == "volume" and parts[2] == "set":
                source = parts[0]
                level = self._parse_level(payload)
                if level is None:
                    return
                cb = self._volume_callbacks.get(source)
                if cb is None:
                    log.warning(f"No volume handler for source '{source}'")
                    return
                self._emit(lambda: cb(level))
            # {id}/ir_edit/+/set -> IR editor (key select, action select,
            # identify button), Section 4.4
            elif len(parts) == 3 and parts[0] == "ir_edit" and parts[2] == "set":
                if parts[1] == "key" and self._ir_edit_key_callback is not None:
                    self._emit(lambda: self._ir_edit_key_callback(payload))
                elif parts[1] == "action" and self._ir_edit_action_callback is not None:
                    self._emit(lambda: self._ir_edit_action_callback(payload))
                elif parts[1] == "identify" and payload == "PRESS" \
                        and self._ir_edit_identify_callback is not None:
                    self._emit(self._ir_edit_identify_callback)
            # {id}/update/install -> Firmware update Install button (Section
            # 4.7); hubd only drops the request file, the root path unit
            # does the privileged work
            elif len(parts) == 2 and parts[0] == "update" and parts[1] == "install":
                if payload == "install" and self._update_install_callback is not None:
                    self._emit(self._update_install_callback)
            # {id}/setting/{key}/set -> runtime setting ("reset" = Reset button)
            elif len(parts) == 3 and parts[0] == "setting" and parts[2] == "set":
                if self._setting_callback is not None:
                    self._emit(lambda: self._setting_callback(parts[1], payload))
            # {id}/volume/set | {id}/mute/set | {id}/ducking/set
            elif len(parts) == 2 and parts[1] == "set":
                cmd = parts[0]
                if cmd == "volume":
                    level = self._parse_level(payload)
                    if level is None:
                        return
                    cb = self._volume_callbacks.get("master")
                    if cb is not None:
                        self._emit(lambda: cb(level))
                elif cmd == "mute" and self._mute_callback is not None:
                    on = payload.upper() in ("ON", "1", "TRUE", "YES")
                    self._emit(lambda: self._mute_callback(on))
                elif cmd == "ducking" and self._ducking_callback is not None:
                    on = payload.upper() in ("ON", "1", "TRUE", "YES")
                    self._emit(lambda: self._ducking_callback(on))
                elif cmd in ("output", "tv_source") and self._selection_callback is not None:
                    self._emit(lambda: self._selection_callback(cmd, payload))
                else:
                    log.debug(f"Unhandled MQTT command: {topic}")
            else:
                log.debug(f"Ignoring MQTT topic: {topic}")
        except Exception as e:
            log.warning(f"Error handling MQTT message {topic}={payload!r}: {e}")

    @staticmethod
    def _parse_level(payload: str) -> float | None:
        try:
            return min(1.0, max(0.0, float(payload)))
        except ValueError:
            log.warning(f"Invalid volume payload: {payload!r}")
            return None

    def _publish_discovery(self, client: mqtt.Client):
        """Publish HA MQTT discovery payloads."""
        base = self.cfg.mqtt_base_topic
        dev_id = self.cfg.device_id
        availability = self._availability_topic()
        device = self._device_block()
        entities = []

        def add(component: str, slug: str, payload: dict):
            entities.append((f"{base}/{component}/{dev_id}_{slug}/config", payload))

        volume_common = {
            "min": 0,
            "max": 100,
            "step": 1,
            "unit_of_measurement": "%",
            "value_template": "{{ value | float * 100 | round(0) | int }}",
            "command_template": "{{ value | float / 100 | round(2) }}",
            "availability_topic": availability,
            "device": device,
        }

        for source, name, icon in (
            ("tv", "TV", "mdi:television"),
            ("bt", "Bluetooth", "mdi:bluetooth-audio"),
            ("music", "Music", "mdi:music"),
        ):
            add("number", f"{source}_volume", {
                "name": f"{name} Volume",
                "unique_id": f"{dev_id}_{source}_volume",
                "state_topic": f"{dev_id}/{source}/volume/state",
                "command_topic": f"{dev_id}/{source}/volume/set",
                "icon": icon,
                **volume_common,
            })

        add("number", "master_volume", {
            "name": "Master Volume",
            "unique_id": f"{dev_id}_master_volume",
            "state_topic": f"{dev_id}/volume/state",
            "command_topic": f"{dev_id}/volume/set",
            "icon": "mdi:volume-high",
            **volume_common,
        })

        add("switch", "mute", {
            "name": "Master Mute",
            "unique_id": f"{dev_id}_mute",
            "state_topic": f"{dev_id}/mute/state",
            "command_topic": f"{dev_id}/mute/set",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": availability,
            "icon": "mdi:volume-off",
            "device": device,
        })

        add("switch", "ducking", {
            "name": "Audio Ducking",
            "unique_id": f"{dev_id}_ducking",
            "state_topic": f"{dev_id}/ducking/state",
            "command_topic": f"{dev_id}/ducking/set",
            "payload_on": "ON",
            "payload_off": "OFF",
            "availability_topic": availability,
            "icon": "mdi:arrow-collapse-down",
            "device": device,
        })

        for key, name, icon in (
            ("ducking", "Ducking Active", "mdi:arrow-collapse-down"),
            ("music", "Music Playing", "mdi:music"),
            ("bt", "Bluetooth Playing", "mdi:bluetooth-audio"),
            ("tv", "TV Playing", "mdi:television"),
        ):
            add("binary_sensor", f"{key}_active", {
                "name": name,
                "unique_id": f"{dev_id}_{key}_active",
                "state_topic": f"{dev_id}/{key}/active",
                "payload_on": "ON",
                "payload_off": "OFF",
                "entity_category": "diagnostic",
                "availability_topic": availability,
                "icon": icon,
                "device": device,
            })

        # Hub Availability mirrors the LWT topic itself. It deliberately
        # carries no availability_topic of its own: the entity must stay
        # registered to report "offline".
        add("binary_sensor", "hub_availability", {
            "name": "Hub Availability",
            "unique_id": f"{dev_id}_hub_availability",
            "state_topic": availability,
            "payload_on": "online",
            "payload_off": "offline",
            "entity_category": "diagnostic",
            "icon": "mdi:server-network",
            "device": device,
        })

        entities.extend(self._selection_discovery(base, dev_id, availability, device))
        entities.extend(self._ir_discovery(base, dev_id, availability, device))
        entities.extend(self._update_discovery(base, dev_id, availability, device))
        if self.settings is not None:
            entities.extend(self._settings_discovery(base, dev_id, availability, device))

        for topic, payload in entities:
            payload["origin"] = self._origin()
            client.publish(topic, json.dumps(payload), retain=True)

        self._retire_layer1(base, dev_id)

        log.info(f"Published {len(entities)} discovery entities")

    def _update_discovery(self, base: str, dev_id: str, availability: str,
                          device: dict) -> list:
        """Firmware update entity (spec Section 4.7). HA renders it under
        Settings -> Updates and on the device page with its standard Install
        button, release link and progress state; the state topic carries the
        JSON the MQTT update platform expects."""
        return [(
            f"{base}/update/{dev_id}_firmware/config",
            {
                "name": "Firmware",
                "unique_id": f"{dev_id}_firmware",
                "entity_category": "diagnostic",
                "state_topic": f"{dev_id}/update/state",
                "command_topic": f"{dev_id}/update/install",
                "payload_install": "install",
                "availability_topic": availability,
                "icon": "mdi:package-up",
                "device": device,
            },
        )]

    def publish_update_state(self, installed: str, latest: str,
                             in_progress: bool):
        """Firmware entity state JSON: installed/latest versions, release
        link and in-progress flag. An unknown latest falls back to the
        installed version (entity reads "up to date" until a check runs)."""
        latest = latest or installed
        payload = {
            "installed_version": installed,
            "latest_version": latest,
            "title": "audio-hub",
            "in_progress": bool(in_progress),
        }
        if latest.startswith("v"):
            payload["release_url"] = RELEASE_URL_BASE + latest
        self._publish(f"{self.cfg.device_id}/update/state", json.dumps(payload))

    def ir_key_options(self) -> list:
        """Sorted union of factory, bound, and seen keys — the IR: Key select
        options and the IR Remote event_types list (Section 4.4)."""
        if self._ir_map is None:
            return sorted(IR_FACTORY_MAP)
        keys = (set(IR_FACTORY_MAP)
                | set(self._ir_map.bindings())
                | set(self._ir_map.seen()))
        return sorted(keys)

    def _ir_discovery(self, base: str, dev_id: str, availability: str,
                      device: dict) -> list:
        """HA payloads for the IR editor entities and the IR Remote event
        entity (spec Sections 4.4/4.5). One fixed entity set that edits one
        mapping at a time — no per-key entities."""
        entities = []
        key_options = self.ir_key_options()
        self._ir_options_cache = key_options
        common = {
            "availability_topic": availability,
            "device": device,
        }
        entities.append((
            f"{base}/select/{dev_id}_ir_edit_key/config",
            {
                "name": "IR: Key",
                "unique_id": f"{dev_id}_ir_edit_key",
                "state_topic": f"{dev_id}/ir_edit/key/state",
                "command_topic": f"{dev_id}/ir_edit/key/set",
                "options": [ir_key_option(k) for k in key_options],
                "entity_category": "config",
                "icon": "mdi:remote",
                **common,
            },
        ))
        entities.append((
            f"{base}/select/{dev_id}_ir_edit_action/config",
            {
                "name": "IR: Action",
                "unique_id": f"{dev_id}_ir_edit_action",
                "state_topic": f"{dev_id}/ir_edit/action/state",
                "command_topic": f"{dev_id}/ir_edit/action/set",
                "options": list(IR_ACTION_LABELS.values()),
                "entity_category": "config",
                "icon": "mdi:remote",
                **common,
            },
        ))
        entities.append((
            f"{base}/button/{dev_id}_ir_edit_identify/config",
            {
                "name": "IR: Identify Button",
                "unique_id": f"{dev_id}_ir_edit_identify",
                "command_topic": f"{dev_id}/ir_edit/identify/set",
                "payload_press": "PRESS",
                "entity_category": "config",
                "icon": "mdi:gesture-tap-button",
                **common,
            },
        ))
        entities.append((
            f"{base}/sensor/{dev_id}_ir_edit_status/config",
            {
                "name": "IR Editor Status",
                "unique_id": f"{dev_id}_ir_edit_status",
                "state_topic": f"{dev_id}/ir_edit/status/state",
                "entity_category": "diagnostic",
                "icon": "mdi:remote",
                **common,
            },
        ))
        entities.append((
            f"{base}/sensor/{dev_id}_ir_mappings/config",
            {
                "name": "IR Mappings",
                "unique_id": f"{dev_id}_ir_mappings",
                "state_topic": f"{dev_id}/ir/mappings/state",
                "json_attributes_topic": f"{dev_id}/ir/mappings/attributes",
                "entity_category": "diagnostic",
                "icon": "mdi:format-list-bulleted",
                **common,
            },
        ))
        entities.append((
            f"{base}/event/{dev_id}_ir_remote/config",
            {
                "name": "IR Remote",
                "unique_id": f"{dev_id}_ir_remote",
                "state_topic": f"{dev_id}/ir/event",
                "event_types": key_options,
                "entity_category": "diagnostic",
                "icon": "mdi:remote",
                **common,
            },
        ))
        return entities

    def _retire_layer1(self, base: str, dev_id: str):
        """Retire Layer 1's per-key select entities and bind-flow trio with
        empty retained payloads (spec Section 4.4). The key set is every key
        that may ever have had a select: v2 file keys, factory keys, and
        every key name in the pre-migration v1 file (including entries
        validation dropped). Retained per-key state payloads are cleared in
        the same sweep. Idempotent; runs every start, so units that skipped
        Layer 1 are unaffected."""
        if self.client is None:
            return
        keys = (self._ir_map.retirement_keys()
                if self._ir_map is not None else set(IR_FACTORY_MAP))
        configs = [f"{base}/select/{dev_id}_ir_{k.lower()}/config"
                   for k in sorted(keys)]
        configs += [
            f"{base}/select/{dev_id}_ir_bind_choose/config",
            f"{base}/button/{dev_id}_ir_bind_arm/config",
            f"{base}/sensor/{dev_id}_ir_bind_status/config",
            f"{base}/sensor/{dev_id}_ir_last_key/config",
        ]
        try:
            for topic in configs:
                self.client.publish(topic, "", retain=True)
            for key in sorted(keys):
                self.client.publish(f"{dev_id}/ir/{key}/state", "", retain=True)
        except Exception as e:
            log.warning(f"Layer 1 retirement sweep failed: {e}")
            return
        log.info(f"Retired {len(configs)} Layer 1 IR entities")

    def _selection_discovery(self, base: str, dev_id: str, availability: str,
                             device: dict) -> list:
        """HA select payloads for the device-selection entities. Options are
        the last known live device set (re-published by the reconciler
        whenever it changes)."""
        entities = []
        for key, name, icon in (
            ("output", "Output Device", "mdi:speaker"),
            ("tv_source", "TV Source", "mdi:television"),
        ):
            entities.append((
                f"{base}/select/{dev_id}_{key}/config",
                {
                    "name": name,
                    "unique_id": f"{dev_id}_{key}",
                    "state_topic": f"{dev_id}/{key}/state",
                    "command_topic": f"{dev_id}/{key}/set",
                    "options": self._selection_options.get(key, []),
                    "entity_category": "config",
                    "availability_topic": availability,
                    "icon": icon,
                    "device": device,
                },
            ))
        return entities

    def _settings_discovery(self, base: str, dev_id: str, availability: str,
                            device: dict) -> list:
        """HA payloads for the runtime-settings entities (spec Section 4.3).
        Percent-kind settings travel 0-100 natively (no value template);
        storage stays 0.0-1.0."""
        entities = []

        def add_setting(component: str, key: str, name: str, icon: str,
                        extra: dict, *, enabled: bool = True):
            entities.append((
                f"{base}/{component}/{dev_id}_setting_{key}/config",
                {
                    "name": name,
                    "unique_id": f"{dev_id}_setting_{key}",
                    "state_topic": f"{dev_id}/setting/{key}/state",
                    "command_topic": f"{dev_id}/setting/{key}/set",
                    "entity_category": "config",
                    "enabled_by_default": enabled,
                    "availability_topic": availability,
                    "icon": icon,
                    "device": device,
                    **extra,
                },
            ))

        add_setting("number", "duck_level", "Duck Level",
                    "mdi:arrow-collapse-down", {
                        "min": 0, "max": 100, "step": 5,
                        "unit_of_measurement": "%", "mode": "slider",
                    })
        add_setting("number", "duck_restore_delay", "Duck Release Delay",
                    "mdi:timer", {
                        "min": 0, "max": 30, "step": 0.5,
                        "unit_of_measurement": "s", "mode": "box",
                    })
        add_setting("number", "ir_volume_step", "IR Volume Step",
                    "mdi:volume-plus", {
                        "min": 1, "max": 10, "step": 1,
                        "unit_of_measurement": "%", "mode": "slider",
                    })
        add_setting("switch", "ir_repeat", "IR Hold-to-Repeat",
                    "mdi:repeat", {})
        add_setting("number", "ir_repeat_interval_ms", "IR Repeat Interval",
                    "mdi:timer-outline", {
                        "min": 100, "max": 1000, "step": 50,
                        "unit_of_measurement": "ms", "mode": "box",
                    }, enabled=False)
        add_setting("number", "tv_probe_threshold", "TV Detection Threshold",
                    "mdi:waveform", {
                        "min": 10, "max": 5000, "step": 10, "mode": "box",
                    }, enabled=False)
        add_setting("number", "tv_probe_interval", "TV Detection Interval",
                    "mdi:timer-sand", {
                        "min": 2, "max": 60, "step": 0.5,
                        "unit_of_measurement": "s", "mode": "box",
                    }, enabled=False)
        add_setting("switch", "auto_update", "Auto Update",
                    "mdi:cellphone-arrow-down", {})
        entities.append((
            f"{base}/select/{dev_id}_setting_release_channel/config",
            {
                "name": "Release Channel",
                "unique_id": f"{dev_id}_setting_release_channel",
                "state_topic": f"{dev_id}/setting/release_channel/state",
                "command_topic": f"{dev_id}/setting/release_channel/set",
                "options": list(SETTING_DEFS["release_channel"]["options"]),
                "entity_category": "config",
                "availability_topic": availability,
                "icon": "mdi:tag-outline",
                "device": device,
            },
        ))
        entities.append((
            f"{base}/button/{dev_id}_setting_reset/config",
            {
                "name": "Reset Settings",
                "unique_id": f"{dev_id}_setting_reset",
                "command_topic": f"{dev_id}/setting/reset/set",
                "payload_press": "PRESS",
                "entity_category": "config",
                "enabled_by_default": False,
                "availability_topic": availability,
                "icon": "mdi:restore",
                "device": device,
            },
        ))
        return entities

    def publish_selection_discovery(self, options: dict):
        """Re-publish both select entities with a fresh options list
        (called by the reconciler when the discovered device set changes,
        and once from the loop at connect via the selection provider)."""
        self._selection_options = {
            "output": sorted(options.get("output", [])),
            "tv_source": sorted(options.get("tv_source", [])),
        }
        if self.client is None:
            return
        base = self.cfg.mqtt_base_topic
        dev_id = self.cfg.device_id
        availability = self._availability_topic()
        device = self._device_block()
        for topic, payload in self._selection_discovery(base, dev_id, availability, device):
            payload["origin"] = self._origin()
            self._publish(topic, json.dumps(payload))

    # -- state publishing (asyncio loop thread) ---------------------------------
    def _publish_initial_state(self):
        if self._state_provider is None or self.client is None:
            return
        try:
            st = self._state_provider()
        except Exception as e:
            log.warning(f"Could not gather initial state: {e}")
            return
        volumes = st.get("volumes", {})
        for source in ("tv", "bt", "music", "master"):
            level = volumes.get(source)
            if level is not None:
                self.publish_volume(source, level)
        self.publish_mute(st.get("muted", False))
        self.publish_ducking_state(st.get("ducking_enabled", True), st.get("ducked", False))
        for key in ("music", "bt", "tv"):
            self.publish_activity(key, st.get(key, False))
        if self._selection_provider is not None:
            try:
                sel = self._selection_provider()
                self.publish_selection_discovery(sel.get("options", {}))
                for key in ("output", "tv_source"):
                    self.publish_selection_state(key, sel.get("effective", {}).get(key))
            except Exception as e:
                log.warning(f"Could not publish selection state: {e}")
        if self.settings is not None:
            for key in SETTING_DEFS:
                self.publish_setting_state(key, self.settings.get(key))
        if self._ir_map is not None:
            options = self.ir_key_options()
            default = options[0] if options else None
            self.publish_ir_edit_key_state(default)
            self.publish_ir_edit_action_state(
                self._ir_map.get(default) if default else None)
            self.publish_ir_edit_status("idle")
            self.publish_ir_mappings()
        if self._update_provider is not None:
            try:
                self._update_provider()
            except Exception as e:
                log.warning(f"Could not publish update state: {e}")

    def publish_volume(self, source: str, level: float):
        topic = (
            f"{self.cfg.device_id}/volume/state"
            if source == "master"
            else f"{self.cfg.device_id}/{source}/volume/state"
        )
        self._publish(topic, f"{level:.2f}")

    def publish_mute(self, muted: bool):
        self._publish(f"{self.cfg.device_id}/mute/state", "ON" if muted else "OFF")

    def publish_ducking_state(self, enabled: bool, ducked: bool):
        self._publish(f"{self.cfg.device_id}/ducking/state", "ON" if enabled else "OFF")
        self._publish(f"{self.cfg.device_id}/ducking/active", "ON" if ducked else "OFF")

    def publish_activity(self, key: str, active: bool):
        self._publish(f"{self.cfg.device_id}/{key}/active", "ON" if active else "OFF")

    def publish_selection_state(self, key: str, option: "str | None"):
        """Publish the EFFECTIVE selection — the option currently applied to
        the graph. None is skipped: HA keeps its last value."""
        if option is None:
            return
        self._publish(f"{self.cfg.device_id}/{key}/state", option)

    def publish_setting_state(self, key: str, value):
        """Echo a setting on {id}/setting/{key}/state. Percent-kind settings
        travel 0-100 (Section 4.3); storage stays 0.0-1.0."""
        if key in SETTING_MQTT_PERCENT:
            payload = str(int(round(value * 100)))
        elif isinstance(value, bool):
            payload = "ON" if value else "OFF"
        elif isinstance(value, float):
            payload = f"{value:g}"
        else:
            payload = str(value)
        self._publish(f"{self.cfg.device_id}/setting/{key}/state", payload)

    # -- IR editor + event publishing (asyncio loop thread) ----------------------
    def publish_ir_edit_key_state(self, key_name: "str | None"):
        """Echo the IR: Key select. The payload is the option text
        ("F13 (KEY_F13)"); None skips (nothing selected yet)."""
        if key_name is None:
            return
        self._publish(f"{self.cfg.device_id}/ir_edit/key/state",
                      ir_key_option(key_name))

    def publish_ir_edit_action_state(self, action: "str | None"):
        """Echo the IR: Action select for the selected key. None (unbound)
        and "ignore" (valid only in hand-edited files) display as the None
        option."""
        if action is None:
            option = IR_ACTION_LABELS["none"]
        else:
            option = IR_ACTION_LABELS.get(action, IR_ACTION_LABELS["none"])
        self._publish(f"{self.cfg.device_id}/ir_edit/action/state", option)

    def publish_ir_edit_status(self, status: str):
        self._publish(f"{self.cfg.device_id}/ir_edit/status/state", status)

    def publish_ir_mappings(self):
        """IR Mappings sensor: state = mapping count, attributes = JSON
        {label: action} of every effective real binding (Section 4.4)."""
        if self._ir_map is None:
            return
        attrs = {ir_key_label(k): a
                 for k, a in sorted(self._ir_map.mapped().items())}
        self._publish(f"{self.cfg.device_id}/ir/mappings/state", str(len(attrs)))
        self._publish(f"{self.cfg.device_id}/ir/mappings/attributes",
                      json.dumps(attrs))

    def publish_ir_editor_options(self):
        """Republish the IR: Key select and IR Remote event entities when the
        key set changed (a new key was seen). Called before the event is
        published so HA has the extended event_types (Section 4.5)."""
        if self.client is None:
            return
        options = self.ir_key_options()
        if options == self._ir_options_cache:
            return
        self._ir_options_cache = options
        base = self.cfg.mqtt_base_topic
        dev_id = self.cfg.device_id
        availability = self._availability_topic()
        device = self._device_block()
        for topic, payload in self._ir_discovery(base, dev_id, availability, device):
            payload["origin"] = self._origin()
            self._publish(topic, json.dumps(payload))
        log.info(f"IR key set changed; republished editor discovery "
                 f"({len(options)} keys)")

    def publish_ir_event(self, key_name: str, action: "str | None"):
        """IR Remote event for a real press — not retained (Section 4.5).
        Mapped keys fire the event as well as the local action."""
        payload = json.dumps({
            "event_type": key_name,
            "action": action if action else "none",
            "label": ir_key_label(key_name),
        })
        if self.client is None:
            return
        try:
            self.client.publish(f"{self.cfg.device_id}/ir/event", payload,
                                retain=False)
        except Exception as e:
            log.debug(f"Publish IR event failed: {e}")

    def _publish(self, topic: str, payload: str):
        if self.client is None:
            return
        try:
            self.client.publish(topic, payload, retain=True)
        except Exception as e:
            log.debug(f"Publish to {topic} failed: {e}")


# -----------------------------------------------------------------------------
# IR Handler
# -----------------------------------------------------------------------------
class IRHandler:
    """
    FLIRC USB IR receiver listener (worker thread).

    Uses select()+read() rather than evdev's async_read_loop(): in this
    environment async_read_loop exits immediately with a device EOF,
    silently dropping the FLIRC fd (verified during development). select()
    is proven reliable. No exclusive grab: on a headless hub nothing else
    consumes the keys, and the grab triggers the same EOF issue.

    The device is (re)opened in a retry loop, so hotplug or late USB
    enumeration is tolerated; if the device disappears mid-listen the loop
    returns to reopening.
    """

    KEY_DOWN = 1   # evdev key press value
    KEY_REPEAT = 2  # kernel auto-repeat while a key is held

    def __init__(self, config: HubConfig, stop_event: threading.Event):
        self.cfg = config
        self.stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._key_callback = None

    def on_key(self, callback):
        """Register the single key callback: called as
        callback(key_name, is_repeat) on the asyncio loop. All mapping,
        repeat-gating, and bind-capture decisions happen there; this worker
        only translates events."""
        self._key_callback = callback

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    def _run_sync(self):
        warned = False
        while not self.stop_event.is_set():
            dev, path = self._open_device()
            if dev is None:
                if not warned:
                    log.warning(f"IR device not found: {self.cfg.ir_device_path}; retrying every 5 s")
                    warned = True
                self.stop_event.wait(5.0)
                continue
            warned = False
            log.info(f"IR device opened: {dev.name} ({path})")
            try:
                while not self.stop_event.is_set():
                    try:
                        r, _, _ = select.select([dev.fd], [], [], 1.0)
                    except (OSError, ValueError):
                        break
                    if not r:
                        continue
                    try:
                        events = dev.read()
                    except OSError as e:
                        log.warning(f"IR device lost: {e}; will reopen")
                        break
                    for event in events:
                        self._handle(event)
            finally:
                dev.close()
        log.info("IR handler stopped")

    def _open_device(self):
        paths = glob.glob(self.cfg.ir_device_path)
        if not paths:
            return None, None
        path = paths[0]
        try:
            return evdev.InputDevice(path), path
        except (OSError, PermissionError) as e:
            log.debug(f"Cannot open IR device {path}: {e}")
            return None, None

    def _handle(self, event):
        if event.type != evdev.ecodes.EV_KEY:
            return
        if event.value not in (self.KEY_DOWN, self.KEY_REPEAT):
            return
        key_name = ECODE_TO_KEY_NAME.get(event.code)
        if key_name is None:
            return  # code with no KEY_* name: nothing sensible to report
        self._dispatch_key(key_name, event.value == self.KEY_REPEAT)

    def _dispatch_key(self, key_name: str, is_repeat: bool):
        if self._key_callback is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(
                self._key_callback, key_name, is_repeat)
        except RuntimeError:
            pass


# -----------------------------------------------------------------------------
# Audio Control (PulseAudio)
# -----------------------------------------------------------------------------
class AudioControl:
    """
    Volume and mute control over a single persistent Pulse connection.

    All methods must be called from the asyncio loop thread (the ducking
    engine keeps its own connection in its own thread). If pipewire-pulse
    restarts, the connection is re-established on the next call.
    """

    def __init__(self, config: HubConfig, selection: "SelectionStore | None" = None):
        self.cfg = config
        self.selection = selection
        self._pulse = None

    def _conn(self):
        if self._pulse is None:
            self._pulse = Pulse("hubd-audioctl")
        return self._pulse

    def _drop(self, err: Exception):
        log.warning(f"Pulse connection error: {err}; reconnecting on next call")
        try:
            if self._pulse is not None:
                self._pulse.close()
        except Exception:
            pass
        self._pulse = None

    def _run(self, op):
        """Run op(pulse) with one transparent retry: if the persistent
        connection went stale (pipewire-pulse restarted), the first call
        fails, the retry reconnects — the caller never sees it."""
        try:
            return op(self._conn())
        except Exception as e:
            log.info(f"Pulse connection stale ({e}); reconnecting")
            self._drop(e)
        try:
            return op(self._conn())
        except Exception as e:
            self._drop(e)
            log.warning(f"Pulse call failed after reconnect: {e}")
            return None

    def _find_lineout(self, pulse):
        """Master volume/mute target. The user's Output Device selection
        wins when it (still) resolves to a live hardware sink; if it is
        absent (device unplugged), fall back to the unit.env pattern so the
        master control keeps driving whatever is actually wired while the
        reconciler holds routing state. Pattern is also the factory default
        when no selection is in effect."""
        if self.selection is not None:
            desired = self.selection.get("output")
            if desired:
                name = resolve_option(
                    desired, hardware_output_names([s.name for s in pulse.sink_list()]))
                if name:
                    for s in pulse.sink_list():
                        if s.name == name:
                            return s
                log.debug(f"Selected output {desired!r} absent; master control on the pattern device")
        pat = self.cfg.lineout_sink_pattern.lower()
        for s in pulse.sink_list():
            if pat in (s.name or "").lower() or pat in (s.description or "").lower():
                return s
        return None

    def _find_tv_source(self, pulse):
        """Factory-default TV source: substring pattern match (name or
        description), unchanged legacy behavior."""
        pat = self.cfg.tv_source_pattern.lower()
        for s in pulse.source_list():
            if pat in (s.name or "").lower() or pat in (s.description or "").lower():
                return s
        return None

    def factory_defaults(self) -> dict:
        """Factory-default selection options (stable prefixes) from the
        unit.env patterns — used when selection.json holds no entry."""
        def op(p):
            out = self._find_lineout(p)
            tv = self._find_tv_source(p)
            return {
                "output": node_prefix(out.name) if out else None,
                "tv_source": node_prefix(tv.name) if tv else None,
            }
        return self._run(op) or {"output": None, "tv_source": None}

    def routing_snapshot(self) -> "dict | None":
        """Everything the selection reconciler needs in one round trip:
        live sink/source name->index maps, each duct playback stream's
        current sink index, and each TV loopback capture's current source
        index."""
        def op(p):
            sinks = {s.name: s.index for s in p.sink_list()}
            sources = {s.name: s.index for s in p.source_list()}
            ducts = {}
            for si in p.sink_input_list():
                node = si.proplist.get("node.name", "")
                if node in DUCT_PLAYBACK_NODES:
                    ducts[node] = si.sink
            captures = {}
            for so in p.source_output_list():
                node = so.proplist.get("node.name", "")
                if node in TV_CAPTURE_NODES:
                    captures[node] = so.source
            return {"sinks": sinks, "sources": sources,
                    "ducts": ducts, "tv_captures": captures}
        return self._run(op)

    def move_ducts_to_sink(self, sink_index: int) -> int:
        """Move every duct playback stream to the given sink. Returns the
        number of streams actually moved (idempotent: streams already on the
        target are untouched, so a freshly booted unit on default wiring
        performs no moves)."""
        def op(p):
            moved = 0
            for si in p.sink_input_list():
                node = si.proplist.get("node.name", "")
                if node in DUCT_PLAYBACK_NODES and si.sink != sink_index:
                    p.sink_input_move(si.index, sink_index)
                    moved += 1
            return moved
        return self._run(op) or 0

    def move_tv_capture_to_source(self, source_index: int, attached_to: set) -> int:
        """Move TV loopback capture streams to the given source. Only
        captures currently ATTACHED to one of `attached_to` (live hardware
        sources) are moved: an unattached capture is waiting for its declared
        candidate target, and moving it would defeat the dual-candidate
        design that follows the UR23 profile flip."""
        def op(p):
            moved = 0
            for so in p.source_output_list():
                node = so.proplist.get("node.name", "")
                if node in TV_CAPTURE_NODES and so.source in attached_to \
                        and so.source != source_index:
                    p.source_output_move(so.index, source_index)
                    moved += 1
            return moved
        return self._run(op) or 0

    def _find_bus(self, pulse, name: str):
        for s in pulse.sink_list():
            if s.name == name:
                return s
        return None

    def _set_sink_volume(self, pulse, sink, level: float):
        v = sink.volume
        for i in range(len(v.values)):
            v.values[i] = level
        pulse.sink_volume_set(sink.index, v)

    # -- line-out (master) ------------------------------------------------------
    def get_lineout_volume(self) -> float | None:
        def op(p):
            s = self._find_lineout(p)
            if s is None or not s.volume.values:
                return None
            return float(s.volume.values[0])
        return self._run(op)

    def set_lineout_volume(self, level: float):
        def op(p):
            s = self._find_lineout(p)
            if s is None:
                log.warning(f"Line-out sink not found (pattern {self.cfg.lineout_sink_pattern!r})")
                return
            self._set_sink_volume(p, s, level)
        self._run(op)

    def get_lineout_mute(self) -> bool:
        def op(p):
            s = self._find_lineout(p)
            return bool(s.mute) if s is not None else False
        return self._run(op) or False

    def set_lineout_mute(self, muted: bool):
        def op(p):
            s = self._find_lineout(p)
            if s is None:
                log.warning(f"Line-out sink not found (pattern {self.cfg.lineout_sink_pattern!r})")
                return
            p.sink_mute(s.index, bool(muted))
        self._run(op)

    # -- virtual buses (per-source volume) ---------------------------------------
    def get_bus_volume(self, bus_name: str) -> float | None:
        def op(p):
            s = self._find_bus(p, bus_name)
            if s is None or not s.volume.values:
                return None
            return float(s.volume.values[0])
        return self._run(op)

    def set_bus_volume(self, bus_name: str, level: float):
        def op(p):
            s = self._find_bus(p, bus_name)
            if s is None:
                log.warning(f"Bus sink '{bus_name}' not found")
                return
            self._set_sink_volume(p, s, level)
        self._run(op)


# -----------------------------------------------------------------------------
# Main Daemon
# -----------------------------------------------------------------------------
class HubDaemon:
    """Main hub daemon coordinating all subsystems."""

    def __init__(self, config: HubConfig):
        self.cfg = config
        self.stop_event = threading.Event()
        self.ir_map = IRMapStore()
        self.settings = SettingsStore(config)
        self.mqtt = MQTTBridge(config, self.ir_map, self.settings)
        self.selection = SelectionStore()
        self.audio = AudioControl(config, self.selection)
        self.ir = IRHandler(config, self.stop_event)
        self.ducking: DuckingEngine | None = None
        # Reconciler bookkeeping: last published option lists and the last
        # EFFECTIVE selection (what the graph is actually fed).
        self._sel_options: dict = {"output": [], "tv_source": []}
        self._sel_effective: dict = {"output": None, "tv_source": None}
        # IR state: first-sighting dedup for unmapped keys, the key currently
        # selected in the IR editor, the armed identify window, a generation
        # counter guarding the delayed return-to-idle publish, and per-key
        # last-repeat timestamps for the rate limiter (Section 5.4).
        self._ir_unmapped_seen: set[str] = set()
        self._ir_edit_key: "str | None" = None
        self._ir_identify: "dict | None" = None
        self._ir_status_generation = 0
        self._ir_last_repeat: dict[str, float] = {}

        # Update entity (Section 4.7): the token of the install request we
        # dropped (None = nothing pending), the last state read from the
        # updater's status file, and the last payload published (dedup —
        # the 2 s tick must not spam the retained topic).
        self._update_pending_token: "str | None" = None
        self._update_state: dict = {"installed": audiohub_version(),
                                    "latest": "", "error": None}
        self._update_last_payload: "str | None" = None

        # MQTT command handlers (run on the asyncio loop)
        self.mqtt.on_volume_command("tv", self._on_tv_volume)
        self.mqtt.on_volume_command("bt", self._on_bt_volume)
        self.mqtt.on_volume_command("music", self._on_music_volume)
        self.mqtt.on_volume_command("master", self._on_master_volume)
        self.mqtt.on_mute_command(self._on_mute_command)
        self.mqtt.on_ducking_command(self._on_ducking_command)
        self.mqtt.on_selection_command(self._on_selection_command)
        self.mqtt.on_setting_command(self._on_setting_command)
        self.mqtt.on_ir_edit_key(self._on_ir_edit_key)
        self.mqtt.on_ir_edit_action(self._on_ir_edit_action)
        self.mqtt.on_ir_edit_identify(self._on_ir_edit_identify)
        self.mqtt.on_update_install(self._on_update_install)
        self.mqtt.attach_state_provider(self._snapshot)
        self.mqtt.attach_selection_provider(self._selection_snapshot)
        self.mqtt.attach_update_provider(self._update_snapshot)

        # IR keypresses (run on the asyncio loop)
        self.ir.on_key(self._on_ir_key)

    # -- MQTT command handlers (loop thread) --------------------------------------
    def _on_tv_volume(self, level: float):
        self.audio.set_bus_volume(self.cfg.bus_tv, level)
        self.mqtt.publish_volume("tv", level)

    def _on_bt_volume(self, level: float):
        self.audio.set_bus_volume(self.cfg.bus_bt, level)
        self.mqtt.publish_volume("bt", level)

    def _on_music_volume(self, level: float):
        self.audio.set_bus_volume(self.cfg.bus_music, level)
        self.mqtt.publish_volume("music", level)

    def _on_master_volume(self, level: float):
        self.audio.set_lineout_volume(level)
        self.mqtt.publish_volume("master", level)

    def _on_mute_command(self, muted: bool):
        self.audio.set_lineout_mute(muted)
        self.mqtt.publish_mute(muted)

    def _on_ducking_command(self, enabled: bool):
        log.info(f"Ducking enable command: {enabled}")
        # Persisted so the switch survives restarts (spec Section 4.3);
        # unit.env remains the fleet default until changed from HA.
        self.settings.set("ducking_enabled", enabled)
        if self.ducking is not None:
            self.ducking.set_enabled(enabled)
            # State is published when the engine applies it (_on_hub_state).

    def _on_setting_command(self, key: str, payload: str):
        """HA command for a runtime setting, or the Reset Settings button
        (key 'reset'). Invalid commands are logged and ignored; accepted
        values are clamped, persisted, applied live, and echoed on the
        state topic (spec Section 4.3)."""
        if key == "reset":
            if payload == "PRESS":
                self._reset_settings()
            return
        if key not in SETTING_DEFS:
            log.warning(f"Ignoring unknown setting {key!r}")
            return
        kind = SETTING_DEFS[key]["kind"]
        try:
            if kind == "bool":
                value = payload.upper() in ("ON", "1", "TRUE", "YES")
            elif kind == "percent":
                value = float(payload) / 100.0
            elif kind == "int":
                value = int(float(payload))
            else:
                value = payload
            stored = self.settings.set(key, value)
        except (ValueError, TypeError) as e:
            log.warning(f"Ignoring invalid {key} command {payload!r}: {e}")
            return
        self._apply_setting(key, stored)
        self.mqtt.publish_setting_state(key, stored)
        log.info(f"Setting {key} -> {stored}")

    def _apply_setting(self, key: str, value):
        """Live-apply an accepted setting. The ducking engine reads duck
        level / release delay / TV probe values from the store every poll,
        so only the enable flag needs explicit forwarding here."""
        if key == "ducking_enabled" and self.ducking is not None:
            self.ducking.set_enabled(value)

    def _reset_settings(self):
        """Reset Settings button: wipe user overrides, re-apply, and
        re-publish every setting from its fall-through value. IR mappings
        and device selections are untouched."""
        self.settings.reset()
        if self.ducking is not None:
            self.ducking.set_enabled(self.settings.get("ducking_enabled"))
        for key in SETTING_DEFS:
            self.mqtt.publish_setting_state(key, self.settings.get(key))
        log.info("Settings reset to unit.env / built-in defaults")

    def _on_selection_command(self, key: str, payload: str):
        """Output Device / TV Source select command from HA."""
        if payload not in self._sel_options.get(key, []):
            log.warning(f"Ignoring {key} selection {payload!r}: not in current options")
            return
        self.selection.set(key, payload)
        log.info(f"{key} selection -> {payload}")
        try:
            # Apply immediately; the reconciler tick covers retries and drift.
            self._reconcile_selection_tick()
        except Exception as e:
            log.warning(f"{key} selection apply failed (will retry): {e}")

    # -- IR key handling (loop thread) -----------------------------------------------
    def _on_ir_key(self, key_name: str, is_repeat: bool):
        """Every event the remote emits, mapped or not. All decisions live
        here (loop thread): seen tracking, identify-capture capture, event
        publishing, map lookup, repeat gating, unmapped-key logging,
        dispatch (Sections 4.4/4.5)."""
        if is_repeat:
            # Auto-repeat fires only volume actions, only when enabled, and
            # never during an armed identify window (5.4). Rate-limited per
            # key: the kernel delivers value-2 events at ~30 Hz; only one
            # step per ir_repeat_interval_ms is dispatched. Repeat steps
            # stay off MQTT (the 2 s state sync publishes the final value)
            # and never fire IR Remote events.
            if self._ir_identify is not None:
                return
            action = self.ir_map.get(key_name)
            if action in IR_REPEATABLE and self.settings.get("ir_repeat"):
                now = time.monotonic()
                interval_s = self.settings.get("ir_repeat_interval_ms") / 1000.0
                if now - self._ir_last_repeat.get(key_name, 0.0) < interval_s:
                    return
                self._ir_last_repeat[key_name] = now
                self._ir_dispatch(action, publish=False)
            return
        # Real press: the key becomes seen first, so the Key select options
        # and event_types grow before the event is published (Section 4.5).
        if self.ir_map.record_seen(key_name):
            self.mqtt.publish_ir_editor_options()
        # Capture wins over event/dispatch: the identify press is consumed,
        # never fires its own action, and never publishes an event.
        if self._ir_identify is not None:
            self._ir_identify_capture(key_name)
            return
        action = self.ir_map.get(key_name)
        self.mqtt.publish_ir_event(key_name, action)
        if action is None or action in IR_INERT:
            if key_name not in self._ir_unmapped_seen:
                self._ir_unmapped_seen.add(key_name)
                log.info(f"IR: unmapped key {key_name} seen")
            return
        self._ir_dispatch(action)

    def _ir_dispatch(self, action: str, publish: bool = True):
        if action == "volume_up":
            self._ir_step(+1, publish)
        elif action == "volume_down":
            self._ir_step(-1, publish)
        elif action == "mute_toggle":
            self._ir_mute()
        elif action == "ducking_toggle":
            current = self.ducking.ducking_enabled if self.ducking is not None else True
            self._on_ducking_command(not current)
        # "ignore"/"none" are inert (event-only)

    # -- IR editor (loop thread) ------------------------------------------------------
    def _ir_editor_default_key(self) -> "str | None":
        """Key the Action select edits when no key was picked yet: the first
        option of the key set, matching the connect-time echo."""
        options = self.mqtt.ir_key_options()
        return options[0] if options else None

    def _on_ir_edit_key(self, option: str):
        """IR: Key select command: select the key and show its current
        action (Section 4.4 editing flow, step 1)."""
        key = ir_parse_key_option(option)
        if key is None:
            log.warning(f"Ignoring IR editor key {option!r}: unknown key")
            return
        self._ir_edit_key = key
        self.mqtt.publish_ir_edit_key_state(key)
        self.mqtt.publish_ir_edit_action_state(self.ir_map.get(key))
        log.info(f"IR editor: key {key}")

    def _on_ir_edit_action(self, option: str):
        """IR: Action select command: apply and persist immediately, no Save
        button (Section 4.4 editing flow, step 2). "None" unbinds the key
        (explicit tombstone); the key stays listed and keeps firing events."""
        action = IR_ACTION_BY_OPTION.get(option)
        if action is None:
            log.warning(f"Ignoring IR editor action {option!r}: unknown")
            return
        key = self._ir_edit_key or self._ir_editor_default_key()
        if key is None:
            log.warning("Ignoring IR editor action: no key selected")
            return
        self._ir_edit_key = key
        self.ir_map.set(key, action)
        self.mqtt.publish_ir_edit_action_state(action)
        self.mqtt.publish_ir_mappings()
        log.info(f"IR editor: {key} -> {action}")

    def _on_ir_edit_identify(self):
        """IR: Identify Button press: arm the capture window (Section 4.4
        identify flow, step 1)."""
        self._ir_status_generation += 1
        self._ir_identify = {"deadline": time.monotonic() + IR_IDENTIFY_WINDOW_S}
        self.mqtt.publish_ir_edit_status(
            "Press a button on the remote (60 s)")
        log.info(f"IR identify armed: waiting for a keypress "
                 f"({IR_IDENTIFY_WINDOW_S:.0f} s window)")

    def _ir_identify_capture(self, key_name: str):
        """A real press during the identify window: select the key, show its
        current action, never change the mapping (Section 4.4, step 2)."""
        self._ir_identify = None
        self._ir_status_generation += 1
        gen = self._ir_status_generation
        self._ir_edit_key = key_name
        self.mqtt.publish_ir_edit_key_state(key_name)
        self.mqtt.publish_ir_edit_action_state(self.ir_map.get(key_name))
        self.mqtt.publish_ir_edit_status(
            f"Captured: {ir_key_label(key_name)} ({key_name}), choose an action")
        log.info(f"IR identify captured {key_name}")
        self._ir_idle_later(gen)

    def _ir_identify_expire(self):
        """Identify window elapsed with no keypress."""
        self._ir_identify = None
        self._ir_status_generation += 1
        gen = self._ir_status_generation
        self.mqtt.publish_ir_edit_status("timeout")
        log.info("IR identify: timed out")
        self._ir_idle_later(gen)

    def _ir_idle_later(self, gen: int):
        """Return the editor status to idle after 10 s unless a newer
        identify transition happened in the meantime."""
        def to_idle():
            if self._ir_status_generation == gen and self._ir_identify is None:
                self.mqtt.publish_ir_edit_status("idle")
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.call_later(IR_IDENTIFY_IDLE_RETURN_S, to_idle)

    def _ir_identify_tick(self):
        if self._ir_identify is not None \
                and time.monotonic() >= self._ir_identify["deadline"]:
            self._ir_identify_expire()

    def _ir_step(self, sign: int, publish: bool = True):
        current = self.audio.get_lineout_volume()
        if current is None:
            return
        step = self.settings.get("ir_volume_step")
        new = min(1.0, max(0.0, current + sign * step))
        self.audio.set_lineout_volume(new)
        if publish:
            self.mqtt.publish_volume("master", new)
        log.info(f"IR volume: {current:.2f} -> {new:.2f}")

    def _ir_mute(self):
        muted = not self.audio.get_lineout_mute()
        self.audio.set_lineout_mute(muted)
        self.mqtt.publish_mute(muted)
        log.info(f"IR mute -> {'on' if muted else 'off'}")

    # -- update entity (Section 4.7, loop thread) --------------------------------
    def _on_update_install(self):
        """HA pressed Install: drop the request file for the root path unit
        and flip the entity to in-progress. hubd gains no privilege — the
        request file is just a token the root updater consumes. A second
        request while one is pending is ignored (the updater also refuses
        the file until its run finishes)."""
        if self._update_pending_token is not None:
            log.warning("Update: install requested while one is in progress; ignored")
            return
        token = secrets.token_hex(16)
        try:
            os.makedirs(os.path.dirname(UPDATE_REQUEST_PATH), exist_ok=True)
            tmp = UPDATE_REQUEST_PATH + ".tmp"
            with open(tmp, "w") as f:
                f.write(token)
            os.replace(tmp, UPDATE_REQUEST_PATH)
        except OSError as e:
            log.error(f"Update: cannot write request file "
                      f"{UPDATE_REQUEST_PATH}: {e}")
            return
        self._update_pending_token = token
        log.info(f"Update: install requested from HA (token {token[:8]}...)")
        self._publish_update()

    def _update_refresh_state(self):
        """Read the updater's status file (fresh from the daily check, or
        the completion record of our own request). The state only counts as
        ours when the status echoes the pending token — a status file left
        by yesterday's daily check must never flap an in-progress entity
        back to 'up to date' mid-install (Section 4.7)."""
        try:
            with open(UPDATE_STATUS_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.debug(f"update status unreadable: {e}")
            return
        if not isinstance(data, dict):
            return
        if self._update_pending_token is not None:
            if data.get("token") != self._update_pending_token:
                return
            self._update_pending_token = None
            err = data.get("error")
            log.info("Update: requested run finished"
                     + (f" with error: {err}" if err else " successfully"))
        self._update_state = {
            "installed": str(data.get("installed_version") or audiohub_version()),
            "latest": str(data.get("latest_version") or ""),
            "error": data.get("error") or None,
        }

    def _publish_update(self):
        """Publish the Firmware entity state, skipping identical payloads
        (the tick runs every 2 s against a retained topic)."""
        installed = self._update_state.get("installed") or audiohub_version()
        latest = self._update_state.get("latest") or ""
        payload = json.dumps({
            "installed_version": installed,
            "latest_version": latest or installed,
            "in_progress": self._update_pending_token is not None,
        })
        if payload != self._update_last_payload:
            self._update_last_payload = payload
            self.mqtt.publish_update_state(installed, latest,
                                           self._update_pending_token is not None)

    def _update_snapshot(self):
        """Provider for the MQTT bridge at connect: refresh from the status
        file, then publish (a fresh process starts not-in-progress; after an
        install the new process reconnects reporting the new version — the
        expected sequence)."""
        self._update_refresh_state()
        self._publish_update()

    def _update_status_tick(self):
        self._update_refresh_state()
        self._publish_update()

    # -- device selection reconciler (loop thread) ------------------------------
    def _selection_snapshot(self) -> dict:
        """Provider for the MQTT bridge at connect: run one reconcile tick
        (fresh options, applies any pending selection) and report the
        result."""
        try:
            self._reconcile_selection_tick()
        except Exception as e:
            log.debug(f"selection snapshot reconcile failed: {e}")
        return {"options": dict(self._sel_options),
                "effective": dict(self._sel_effective)}

    def _reconcile_selection_tick(self):
        """Device-selection reconciler (2 s cadence, on the loop). hubd is
        the sole owner of routing state after boot; the declarative configs
        are only cold-boot defaults. One tick: enumerate, republish options
        when the device set changed, move streams whose target differs from
        the resolved selection (covers boot, device arrival, and drift), and
        publish effective state on change."""
        snap = self.audio.routing_snapshot()
        if snap is None:
            return
        outputs = hardware_output_names(list(snap["sinks"]))
        captures = hardware_capture_names(list(snap["sources"]))

        options = {"output": build_options(outputs),
                   "tv_source": build_options(captures)}
        if options != self._sel_options:
            self._sel_options = options
            self.mqtt.publish_selection_discovery(options)

        defaults = self.audio.factory_defaults()
        effective = dict(self._sel_effective)
        for key, names in (("output", outputs), ("tv_source", captures)):
            want = self.selection.get(key) or defaults.get(key)
            if not want:
                continue
            resolved = resolve_option(want, names)
            if resolved is None:
                # Selected device absent: hold the last effective state.
                log.debug(f"{key} selection {want!r} absent; holding {effective.get(key)!r}")
                continue
            effective[key] = want
            if key == "output":
                sink_index = snap["sinks"][resolved]
                off_target = [n for n, i in snap["ducts"].items() if i != sink_index]
                if off_target:
                    moved = self.audio.move_ducts_to_sink(sink_index)
                    log.info(f"Output {want!r}: moved {moved} duct stream(s) to {resolved}")
            else:
                hardware_idx = {snap["sources"][n] for n in captures}
                off_target = [n for n, i in snap["tv_captures"].items()
                              if i in hardware_idx and i != snap["sources"][resolved]]
                if off_target:
                    moved = self.audio.move_tv_capture_to_source(
                        snap["sources"][resolved], hardware_idx)
                    log.info(f"TV source {want!r}: moved {moved} capture stream(s) to {resolved}")
        if effective != self._sel_effective:
            self._sel_effective = effective
            for key in ("output", "tv_source"):
                self.mqtt.publish_selection_state(key, effective.get(key))

    # -- state ----------------------------------------------------------------------
    def _snapshot(self) -> dict:
        snap = {
            "volumes": {
                "tv": self.audio.get_bus_volume(self.cfg.bus_tv),
                "bt": self.audio.get_bus_volume(self.cfg.bus_bt),
                "music": self.audio.get_bus_volume(self.cfg.bus_music),
                "master": self.audio.get_lineout_volume(),
            },
            "muted": self.audio.get_lineout_mute(),
        }
        if self.ducking is not None:
            snap.update(self.ducking.snapshot())
        return snap

    def _on_hub_state(self, state: dict):
        self.mqtt.publish_ducking_state(state["ducking_enabled"], state["ducked"])
        for key in ("music", "bt", "tv"):
            self.mqtt.publish_activity(key, state[key])

    def _request_stop(self):
        log.info("Shutdown requested")
        self.stop_event.set()

    async def _state_sync_loop(self):
        """Periodically compare actual audio state with what was last
        published, so HA stays truthful about volumes changed outside hubd
        (wpctl, another client, IR from a previous instance, ...), and run
        the device-selection reconciler on the same cadence."""
        log.info("State sync + selection reconciler active (2 s cadence)")
        last: dict = {}
        while not self.stop_event.is_set():
            try:
                self._reconcile_selection_tick()
            except Exception as e:
                log.debug(f"selection reconcile error: {e}")
            try:
                self._ir_identify_tick()
            except Exception as e:
                log.debug(f"ir identify tick error: {e}")
            try:
                self._update_status_tick()
            except Exception as e:
                log.debug(f"update status tick error: {e}")
            snap = {}
            for source, bus in (("tv", self.cfg.bus_tv), ("bt", self.cfg.bus_bt),
                                ("music", self.cfg.bus_music)):
                v = self.audio.get_bus_volume(bus)
                if v is not None:
                    snap[source] = round(v, 2)
            m = self.audio.get_lineout_volume()
            if m is not None:
                snap["master"] = round(m, 2)
            snap["muted"] = self.audio.get_lineout_mute()

            for key, val in snap.items():
                if last.get(key) != val:
                    if key == "muted":
                        self.mqtt.publish_mute(val)
                    else:
                        self.mqtt.publish_volume(key, val)
            last = snap
            await asyncio.sleep(2.0)

    async def run(self):
        log.info("Hub daemon starting")
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._request_stop)

        self.ducking = DuckingEngine(self.cfg, loop, self.stop_event,
                                     self.selection, self.settings)
        self.ducking.on_change(self._on_hub_state)
        # The IR worker thread dispatches keypresses onto this loop; without
        # this the handler logs keys but silently drops them.
        self.ir.set_loop(loop)

        threads = [
            threading.Thread(target=self.ducking._run_sync, name="ducking", daemon=True),
            threading.Thread(target=self.ir._run_sync, name="ir", daemon=True),
        ]
        for t in threads:
            t.start()
        sync_task = asyncio.create_task(self._state_sync_loop())
        try:
            await self.mqtt.run(self.stop_event)
        finally:
            self.stop_event.set()
            sync_task.cancel()
            for t in threads:
                t.join(timeout=3)
        log.info("Hub daemon stopped")


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Audio Hub Controller Daemon")
    parser.add_argument(
        "--config",
        default=os.environ.get("AUDIOHUB_CONFIG", "/etc/audiohub/unit.env"),
        help="Path to unit configuration file",
    )
    parser.add_argument("--version", action="version",
                        version="%(prog)s " + audiohub_version())
    args = parser.parse_args()

    try:
        config = HubConfig.from_env(args.config)
        log.info(f"Loaded config from {args.config}")
        log.info(f"Device: {config.device_name} ({config.device_id})")
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        sys.exit(1)

    daemon = HubDaemon(config)
    asyncio.run(daemon.run())


if __name__ == "__main__":
    main()
