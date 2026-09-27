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
        )


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

    def __init__(self, config: HubConfig, loop: asyncio.AbstractEventLoop, stop_event: threading.Event):
        self.cfg = config
        self.loop = loop
        self.stop_event = stop_event
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
        expected = self._cubic(self.cfg.duck_level if self.is_ducked else 1.0)
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
        self._tv_probe_at = now + self.cfg.tv_probe_interval
        on = self._probe_tv_level()
        if on:
            self._tv_hold_until = now + self.cfg.tv_sensor_hold
        self._tv_cached = on or now < self._tv_hold_until
        return self._tv_cached

    def _probe_tv_level(self) -> bool:
        """Record ~0.3 s of raw samples from the UR23 and measure RMS.
        pw-record has no duration option, so use --raw on stdout with a
        short timeout: on SIGKILL the already-flushed bytes are still
        delivered via TimeoutExpired.stdout — enough for an RMS reading."""
        try:
            pat = self.cfg.tv_source_pattern.lower()
            target = None
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
            return rms > self.cfg.tv_probe_threshold
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
        level = self.cfg.duck_level if ducked else 1.0
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
            elif now - self._inactive_since >= self.cfg.duck_restore_delay:
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
        {id}/ducking/set             ducking enable       (ON/OFF)
        {id}/volume/state, {id}/{source}/volume/state, ...  state (retained)
    """

    def __init__(self, config: HubConfig):
        self.cfg = config
        self.client: mqtt.Client | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._volume_callbacks: dict[str, object] = {}
        self._mute_callback = None
        self._ducking_callback = None
        self._state_provider = None

    def on_volume_command(self, source: str, callback):
        """Register callback(level: float) for a source or 'master'."""
        self._volume_callbacks[source] = callback

    def on_mute_command(self, callback):
        """Register callback(muted: bool)."""
        self._mute_callback = callback

    def on_ducking_command(self, callback):
        """Register callback(enabled: bool)."""
        self._ducking_callback = callback

    def attach_state_provider(self, provider):
        """Register fn() -> dict with current state, published on connect.
        Called on the asyncio loop (see _publish_initial_state)."""
        self._state_provider = provider

    def _availability_topic(self) -> str:
        return f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/state"

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
        device = {
            "identifiers": [dev_id],
            "name": self.cfg.device_name,
            "model": "Raspberry Pi 4B",
            "manufacturer": "Custom Audio Hub",
        }
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
                "availability_topic": availability,
                "icon": icon,
                "device": device,
            })

        for topic, payload in entities:
            client.publish(topic, json.dumps(payload), retain=True)

        log.info(f"Published {len(entities)} discovery entities")

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

    KEY_DOWN = 1  # evdev key press value

    def __init__(self, config: HubConfig, stop_event: threading.Event):
        self.cfg = config
        self.stop_event = stop_event
        self._loop: asyncio.AbstractEventLoop | None = None
        self._callbacks: dict[str, object] = {}

    def on_volume_up(self, callback):
        self._callbacks["up"] = callback

    def on_volume_down(self, callback):
        self._callbacks["down"] = callback

    def on_mute(self, callback):
        self._callbacks["mute"] = callback

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
        if event.value != self.KEY_DOWN:
            return
        if event.code == evdev.ecodes.KEY_VOLUMEUP:
            log.info("IR: volume up")
            self._dispatch(self._callbacks.get("up"))
        elif event.code == evdev.ecodes.KEY_VOLUMEDOWN:
            log.info("IR: volume down")
            self._dispatch(self._callbacks.get("down"))
        elif event.code == evdev.ecodes.KEY_MUTE:
            log.info("IR: mute")
            self._dispatch(self._callbacks.get("mute"))

    def _dispatch(self, callback):
        if callback is None or self._loop is None:
            return
        try:
            self._loop.call_soon_threadsafe(callback)
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

    def __init__(self, config: HubConfig):
        self.cfg = config
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
        pat = self.cfg.lineout_sink_pattern.lower()
        for s in pulse.sink_list():
            if pat in (s.name or "").lower() or pat in (s.description or "").lower():
                return s
        return None

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
        self.mqtt = MQTTBridge(config)
        self.audio = AudioControl(config)
        self.ir = IRHandler(config, self.stop_event)
        self.ducking: DuckingEngine | None = None

        # MQTT command handlers (run on the asyncio loop)
        self.mqtt.on_volume_command("tv", self._on_tv_volume)
        self.mqtt.on_volume_command("bt", self._on_bt_volume)
        self.mqtt.on_volume_command("music", self._on_music_volume)
        self.mqtt.on_volume_command("master", self._on_master_volume)
        self.mqtt.on_mute_command(self._on_mute_command)
        self.mqtt.on_ducking_command(self._on_ducking_command)
        self.mqtt.attach_state_provider(self._snapshot)

        # IR callbacks (run on the asyncio loop)
        self.ir.on_volume_up(self._ir_volume_up)
        self.ir.on_volume_down(self._ir_volume_down)
        self.ir.on_mute(self._ir_mute)

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
        if self.ducking is not None:
            self.ducking.set_enabled(enabled)
            # State is published when the engine applies it (_on_hub_state).

    # -- IR callbacks (loop thread) -------------------------------------------------
    def _ir_volume_up(self):
        self._ir_step(+1)

    def _ir_volume_down(self):
        self._ir_step(-1)

    def _ir_step(self, sign: int):
        current = self.audio.get_lineout_volume()
        if current is None:
            return
        new = min(1.0, max(0.0, current + sign * self.cfg.ir_volume_step))
        self.audio.set_lineout_volume(new)
        self.mqtt.publish_volume("master", new)
        log.info(f"IR volume: {current:.2f} -> {new:.2f}")

    def _ir_mute(self):
        muted = not self.audio.get_lineout_mute()
        self.audio.set_lineout_mute(muted)
        self.mqtt.publish_mute(muted)
        log.info(f"IR mute -> {'on' if muted else 'off'}")

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
        (wpctl, another client, IR from a previous instance, ...)."""
        last: dict = {}
        while not self.stop_event.is_set():
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

        self.ducking = DuckingEngine(self.cfg, loop, self.stop_event)
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
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")
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
