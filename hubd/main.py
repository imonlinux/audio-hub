#!/usr/bin/env python3
"""
hubd - Audio Hub Controller Daemon

Single asyncio daemon combining ducking, MQTT, IR, and status monitoring.

Usage:
    hubd [--config CONFIG]

Environment:
    AUDIOHUB_CONFIG - Path to unit configuration file (default: /etc/audiohub/unit.env)
"""

import asyncio
import argparse
import ctypes
import evdev
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from threading import Thread

try:
    import paho.mqtt.client as mqtt
    from pulsectl import Pulse
    import pulsectl
except ImportError as e:
    print(f"Missing dependency: {e}")
    print("Install with: pip install paho-mqtt pulsectl evdev")
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

    # Ducking
    ducking_enabled: bool = True
    duck_level: float = 0.20
    duck_restore_delay: float = 2.0
    music_peak_threshold: float = 0.01  # signal level above which music is "active"

    # IR Remote
    ir_device_path: str = "/dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd"
    ir_volume_step: float = 0.03

    # Sendspin
    sendspin_pattern: str = "sendspin"

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

        # Type conversions
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
            ducking_enabled=get_bool("AUDIOHUB_DUCKING_ENABLED", True),
            duck_level=get_float("AUDIOHUB_DUCK_LEVEL", 0.20),
            duck_restore_delay=get_float("AUDIOHUB_DUCK_RESTORE_DELAY", 2.0),
            music_peak_threshold=get_float("AUDIOHUB_MUSIC_PEAK_THRESHOLD", 0.01),
            ir_device_path=props.get("AUDIOHUB_IR_DEVICE", "/dev/input/by-id/usb-flirc.tv_flirc_*-event-kbd"),
            ir_volume_step=get_float("AUDIOHUB_IR_VOLUME_STEP", 0.03),
            sendspin_pattern=props.get("AUDIOHUB_SENDSPIN_PATTERN", "sendspin"),
        )


# -----------------------------------------------------------------------------
# Ducking Engine
# -----------------------------------------------------------------------------
class DuckingEngine:
    """
    Event-driven ducking engine using pulsectl.

    Trigger: a non-corked sink-input routed to bus.music (the Music/Sendspin bus).
    Action: apply the duck level to the duct.tv and duct.bt loopback playback
            streams (sink-inputs whose node.name is duct.tv.playback /
            duct.bt.playback), which feed the hardware sink.
    Restore: after a hold-off delay once music goes inactive, so track gaps
             don't cause volume flapping.

    Ducking targets the dedicated duct streams, NOT the source bus volumes,
    so it composes cleanly with per-source volume set via MQTT/HA.

    Detection is SINK-BASED, not application-name based: any stream routed to
    the music bus counts as music, regardless of how Sendspin names itself.

    pulsectl's event loop is blocking, so _run_sync() runs in a worker thread;
    state-change callbacks are marshalled back to the asyncio loop via
    loop.call_soon_threadsafe.
    """

    # Loopback playback node names that carry TV / Bluetooth audio to the
    # hardware sink — these (and only these) get attenuated when ducking.
    DUCT_NODES = ("duct.tv.playback", "duct.bt.playback")

    def __init__(self, config: HubConfig, loop: asyncio.AbstractEventLoop):
        self.cfg = config
        self.loop = loop
        self.is_ducked = False
        self.ducking_enabled = config.ducking_enabled
        self._inactive_since: float | None = None
        self._pulse = None
        self._on_state_change = None

    def on_state_change(self, callback):
        """Register callback (invoked on the asyncio loop) for state changes."""
        self._on_state_change = callback

    def set_enabled(self, enabled: bool):
        """Enable/disable ducking (thread-safe; called from MQTT commands)."""
        self.ducking_enabled = enabled
        self._inactive_since = None
        if not enabled and self._pulse is not None and self.is_ducked:
            self._apply(False)
        elif enabled and self._pulse is not None:
            self._check()

    async def run(self):
        """Run the ducking engine in a worker thread."""
        log.info("Ducking engine starting")
        await asyncio.to_thread(self._run_sync)

    def _run_sync(self):
        """Polling loop — runs in a worker thread.

        Single persistent Pulse context. Detection uses sink_list/sink_input_list
        (list queries) which create NO streams and so do NOT leak file
        descriptors — unlike get_peak_sample, which leaks ~1 FD/call in
        pipewire-pulse and exhausts the 1024-FD limit in minutes. No event_listen
        (it + volume_set segfaults libpulse); pure polling at 0.5 s.
        """
        try:
            with Pulse("hubd-ducking") as pulse:
                self._pulse = pulse
                self._set_duct_level(1.0)
                self.is_ducked = False
                self._inactive_since = None
                log.info("Ducking ducts normalized to full on startup")
                while True:
                    try:
                        self._check()
                    except Exception as e:
                        log.debug(f"ducking check error: {e}")
                    time.sleep(0.5)
        except Exception as e:
            log.error(f"Ducking engine error: {e}")

    def _music_active(self) -> bool:
        """True if a non-corked sink-input is routed to bus.music.

        Leak-free: sink_list/sink_input_list create no streams. This is the
        detection strategy the legacy system used. Limitation: a source that
        keeps an uncorked-but-silent stream open while paused (Sendspin does)
        will read as active, so ducking holds until the stream corks or closes
        (i.e. until music STOPS, not merely pauses). Per the project spec,
        ducking restores "when the music source stops" — so this satisfies the
        requirement for stop, and pause-hold is acceptable. get_peak_sample
        would detect true silence but leaks FDs in pipewire-pulse (unfixable
        client-side), so it is not used.
        """
        try:
            music_idx = None
            for s in self._pulse.sink_list():
                if s.name == self.cfg.bus_music:
                    music_idx = s.index
                    break
            if music_idx is None:
                return False
            for si in self._pulse.sink_input_list():
                if si.sink == music_idx and not si.corked:
                    return True
        except Exception as e:
            log.debug(f"music_active check failed: {e}")
        return False

    def _set_duct_level(self, level: float):
        """Unconditionally set all duck-target ducts to a volume level."""
        if self._pulse is None:
            return []
        targets = []
        for si in self._pulse.sink_input_list():
            node = si.proplist.get("node.name", "")
            if node in self.DUCT_NODES:
                v = si.volume
                for i in range(len(v.values)):
                    v.values[i] = level
                self._pulse.sink_input_volume_set(si.index, v)
                targets.append(node)
        return targets

    def _apply(self, ducked: bool):
        """Set duct stream volumes to duck level (or 1.0 to restore)."""
        level = self.cfg.duck_level if ducked else 1.0
        targets = self._set_duct_level(level)
        self.is_ducked = ducked
        log.info(
            f"Ducking {'ON' if ducked else 'OFF'} level={level:.2f} targets={targets}"
        )
        if self._on_state_change:
            try:
                self.loop.call_soon_threadsafe(self._on_state_change, ducked)
            except RuntimeError:
                pass

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
        else:
            if self.is_ducked:
                now = time.monotonic()
                if self._inactive_since is None:
                    self._inactive_since = now
                elif now - self._inactive_since >= self.cfg.duck_restore_delay:
                    self._apply(False)
                    self._inactive_since = None



# -----------------------------------------------------------------------------
# MQTT Bridge
# -----------------------------------------------------------------------------
class MQTTBridge:
    """
    MQTT / Home Assistant integration bridge.

    Features:
    - HA MQTT Discovery for all entities
    - Per-source volume control
    - Master volume/mute
    - Ducking enable switch
    - Source status binary sensors
    - LWT for availability
    """

    def __init__(self, config: HubConfig):
        self.cfg = config
        self.client: mqtt.Client | None = None
        self._last_state = {}
        self._volume_callbacks = {}
        self._mute_callbacks = {}
        self._ducking_callback = None

    def on_volume_command(self, source: str, callback):
        """Register callback for volume commands."""
        self._volume_callbacks[source] = callback

    def on_mute_command(self, callback):
        """Register callback for mute commands."""
        self._mute_callbacks = callback

    def on_ducking_command(self, callback):
        """Register callback for ducking enable commands."""
        self._ducking_callback = callback

    async def run(self):
        """Run MQTT client."""
        log.info(f"MQTT bridge connecting to {self.cfg.mqtt_host}:{self.cfg.mqtt_port}")

        self.client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"audio-hub-{self.cfg.device_id}",
            clean_session=True,
        )
        self.client.username_pw_set(self.cfg.mqtt_username, self.cfg.mqtt_password)
        self.client.will_set(
            f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/state",
            payload="offline",
            retain=True,
        )

        self.client.on_connect = self._on_connect
        self.client.on_message = self._on_message

        self.client.connect(self.cfg.mqtt_host, self.cfg.mqtt_port, keepalive=60)
        self.client.loop_start()

        # Publish availability
        await self._publish_availability(True)

        # Run indefinitely
        while self.client.is_connected():
            await asyncio.sleep(1)

        log.warning("MQTT disconnected")

    async def _publish_availability(self, online: bool):
        """Publish availability state."""
        self.client.publish(
            f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/state",
            "online" if online else "offline",
            retain=True,
        )

    def _on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected")

            # Subscribe to command topics
            for source in ("tv", "bt", "music"):
                client.subscribe(f"{self.cfg.device_id}/{source}/volume/set")
                log.debug(f"Subscribed: {self.cfg.device_id}/{source}/volume/set")

            client.subscribe(f"{self.cfg.device_id}/volume/set")
            client.subscribe(f"{self.cfg.device_id}/mute/set")
            client.subscribe(f"{self.cfg.device_id}/ducking/set")

            # Publish discovery
            self._publish_discovery(client)
        else:
            log.error(f"MQTT connect failed: {reason_code}")

    def _on_message(self, client, userdata, msg):
        """Handle incoming MQTT commands."""
        topic = msg.topic
        payload = msg.payload.decode().strip()

        log.debug(f"MQTT: {topic} = {payload}")

        # Parse topic
        parts = topic.removeprefix(f"{self.cfg.device_id}/").split("/")
        if len(parts) < 2:
            return

        if parts[1] == "volume" and parts[2] == "set":
            if len(parts) == 3:  # master volume
                if self._volume_callbacks.get("master"):
                    self._volume_callbacks["master"](float(payload))
            else:  # source volume
                source = parts[0]
                if source in self._volume_callbacks:
                    self._volume_callbacks[source](float(payload))

        elif parts[1] == "mute" and parts[2] == "set":
            if self._mute_callbacks:
                self._mute_callbacks(payload == "ON")

        elif parts[1] == "ducking" and parts[2] == "set":
            if self._ducking_callback:
                self._ducking_callback(payload == "ON")

    def _publish_discovery(self, client: mqtt.Client):
        """Publish HA MQTT discovery payloads."""
        device = {
            "identifiers": [self.cfg.device_id],
            "name": self.cfg.device_name,
            "model": "Raspberry Pi 4B",
            "manufacturer": "Custom Audio Hub",
        }

        entities = []

        # Per-source volume
        for source, name in [("tv", "TV"), ("bt", "Bluetooth"), ("music", "Music")]:
            entities.append((
                f"{self.cfg.mqtt_base_topic}/number/{self.cfg.device_id}_{source}_volume/config",
                {
                    "name": f"{name} Volume",
                    "unique_id": f"{self.cfg.device_id}_{source}_volume",
                    "state_topic": f"{self.cfg.device_id}/{source}/volume/state",
                    "command_topic": f"{self.cfg.device_id}/{source}/volume/set",
                    "min": 0,
                    "max": 100,
                    "step": 1,
                    "unit_of_measurement": "%",
                    "value_template": "{{ value | float * 100 | round(0) | int }}",
                    "command_template": "{{ value | float / 100 | round(2) }}",
                    "icon": "mdi:volume-high" if source == "music" else "mdi:speaker",
                    "device": device,
                }
            ))

        # Master volume
        entities.append((
            f"{self.cfg.mqtt_base_topic}/number/{self.cfg.device_id}_master_volume/config",
            {
                "name": "Master Volume",
                "unique_id": f"{self.cfg.device_id}_master_volume",
                "state_topic": f"{self.cfg.device_id}/volume/state",
                "command_topic": f"{self.cfg.device_id}/volume/set",
                "min": 0,
                "max": 100,
                "step": 1,
                "unit_of_measurement": "%",
                "value_template": "{{ value | float * 100 | round(0) | int }}",
                "command_template": "{{ value | float / 100 | round(2) }}",
                "icon": "mdi:volume-high",
                "device": device,
            }
        ))

        # Master mute
        entities.append((
            f"{self.cfg.mqtt_base_topic}/switch/{self.cfg.device_id}_mute/config",
            {
                "name": "Master Mute",
                "unique_id": f"{self.cfg.device_id}_mute",
                "state_topic": f"{self.cfg.device_id}/mute/state",
                "command_topic": f"{self.cfg.device_id}/mute/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:volume-off",
                "device": device,
            }
        ))

        # Ducking enable
        entities.append((
            f"{self.cfg.mqtt_base_topic}/switch/{self.cfg.device_id}_ducking/config",
            {
                "name": "Audio Ducking",
                "unique_id": f"{self.cfg.device_id}_ducking",
                "state_topic": f"{self.cfg.device_id}/ducking/state",
                "command_topic": f"{self.cfg.device_id}/ducking/set",
                "payload_on": "ON",
                "payload_off": "OFF",
                "icon": "mdi:arrow-collapse-down",
                "device": device,
            }
        ))

        # Availability sensor
        entities.append((
            f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/config",
            {
                "name": "Hub Availability",
                "unique_id": f"{self.cfg.device_id}_availability",
                "state_topic": f"{self.cfg.mqtt_base_topic}/sensor/{self.cfg.device_id}_availability/state",
                "device_class": "connectivity",
                "icon": "mdi:lan-connect",
                "device": device,
            }
        ))

        # Publish all
        for topic, payload in entities:
            client.publish(topic, json.dumps(payload), retain=True)

        log.info(f"Published {len(entities)} discovery entities")

    async def publish_state(self, source: str, volume: float | None = None, muted: bool = False):
        """Publish state updates."""
        if volume is not None:
            self.client.publish(
                f"{self.cfg.device_id}/{source}/volume/state",
                f"{volume:.2f}",
                retain=True,
            )

        if muted is not None and source == "master":
            self.client.publish(
                f"{self.cfg.device_id}/mute/state",
                "ON" if muted else "OFF",
                retain=True,
            )

    async def publish_ducking_state(self, enabled: bool, ducked: bool):
        """Publish ducking state."""
        self.client.publish(
            f"{self.cfg.device_id}/ducking/state",
            "ON" if enabled else "OFF",
            retain=True,
        )


# -----------------------------------------------------------------------------
# IR Handler
# -----------------------------------------------------------------------------
class IRHandler:
    """
    FLIRC USB IR receiver input handler.

    Handles VOLUMEUP, VOLUMEDOWN (with repeat), and MUTE toggle.
    Uses exclusive device grab to prevent keypresses reaching console.
    """

    KEY_DOWN = 1  # evdev key press value

    def __init__(self, config: HubConfig):
        self.cfg = config
        self._device: evdev.InputDevice | None = None
        self.loop = None  # set in run()
        self._volume_up_callback = None
        self._volume_down_callback = None
        self._mute_callback = None

    def on_volume_up(self, callback):
        self._volume_up_callback = callback

    def on_volume_down(self, callback):
        self._volume_down_callback = callback

    def on_mute(self, callback):
        self._mute_callback = callback

    async def run(self):
        """Run IR loop in a worker thread (blocking select + read)."""
        log.info("IR handler starting")
        if self.loop is None:
            self.loop = asyncio.get_running_loop()
        await asyncio.to_thread(self._run_sync)

    def _dispatch(self, callback):
        """Marshal a keypress callback onto the asyncio loop (thread-safe)."""
        if callback is None:
            return
        try:
            self.loop.call_soon_threadsafe(callback)
        except RuntimeError:
            pass

    def _run_sync(self):
        """Blocking IR read loop — runs in a worker thread.

        Uses select()+dev.read() rather than evdev's async_read_loop(): in this
        environment async_read_loop + the exclusive grab exits immediately
        (device EOF), silently dropping the FLIRC fd so no keypresses are ever
        seen. select()+read() is proven reliable here (verified by raw capture).
        No exclusive grab: on a headless Pi nothing else consumes the keys, and
        skipping the grab avoids the EOF-on-read issue. Key actions are
        dispatched back to the asyncio loop via call_soon_threadsafe.
        """
        import glob, select, time
        devices = glob.glob(self.cfg.ir_device_path)
        if not devices:
            log.warning(f"IR device not found: {self.cfg.ir_device_path}")
            log.info("IR control disabled")
            return
        device_path = devices[0]
        try:
            self._device = evdev.InputDevice(device_path)
            log.info(f"IR device opened: {self._device.name}")
        except (FileNotFoundError, PermissionError) as e:
            log.warning(f"Cannot open IR device: {e}")
            log.info("IR control disabled")
            return

        log.info("IR handler listening")
        try:
            while True:
                r, _, _ = select.select([self._device.fd], [], [], 1.0)
                if not r:
                    continue
                try:
                    events = self._device.read()
                except OSError as e:
                    log.warning(f"IR read error: {e}; retrying")
                    time.sleep(0.5)
                    continue
                for event in events:
                    if event.type != evdev.ecodes.EV_KEY:
                        continue
                    if event.value != self.KEY_DOWN:
                        continue
                    if event.code == evdev.ecodes.KEY_VOLUMEUP:
                        log.info("IR: VOLUME UP")
                        self._dispatch(self._volume_up_callback)
                    elif event.code == evdev.ecodes.KEY_VOLUMEDOWN:
                        log.info("IR: VOLUME DOWN")
                        self._dispatch(self._volume_down_callback)
                    elif event.code == evdev.ecodes.KEY_MUTE:
                        log.info("IR: MUTE")
                        self._dispatch(self._mute_callback)
        except asyncio.CancelledError:
            log.info("IR handler cancelled")
        finally:
            if self._device:
                self._device.close()



# -----------------------------------------------------------------------------
# Audio Control (PulseAudio)
# -----------------------------------------------------------------------------
class AudioControl:
    """Control volume and mute via PulseAudio."""

    def __init__(self, config: HubConfig):
        self.cfg = config
        self._pulse = None

    def get_lineout_sink(self):
        """Find the line-out/hardware sink."""
        with Pulse() as pulse:
            for sink in pulse.sink_list():
                if self.cfg.lineout_sink_pattern.lower() in sink.name.lower():
                    return sink
                if self.cfg.lineout_sink_pattern.lower() in sink.description.lower():
                    return sink
        return None

    def set_volume(self, sink, level: float):
        """Set sink volume (0.0 - 1.0)."""
        volume = sink.volume
        for i in range(len(volume.values)):
            volume.values[i] = level
        with Pulse() as pulse:
            pulse.sink_volume_set(sink.index, volume)

    def get_volume(self, sink) -> float:
        """Get current sink volume."""
        return sink.volume.values[0] if sink.volume.values else 0.0

    def set_mute(self, sink, muted: bool):
        """Set sink mute state."""
        with Pulse() as pulse:
            pulse.sink_mute(sink.index, muted)

    def is_muted(self, sink) -> bool:
        """Check if sink is muted."""
        return sink.mute == 1

    def get_bus_volume(self, bus_name: str) -> float:
        """Get volume of a virtual bus sink."""
        with Pulse() as pulse:
            for sink in pulse.sink_list():
                if bus_name in sink.name:
                    return sink.volume.values[0] if sink.volume.values else 0.0
        return 0.0

    def set_bus_volume(self, bus_name: str, level: float):
        """Set volume of a virtual bus sink."""
        with Pulse() as pulse:
            for sink in pulse.sink_list():
                if bus_name in sink.name:
                    volume = sink.volume
                    for i in range(len(volume.values)):
                        volume.values[i] = level
                    pulse.sink_volume_set(sink.index, volume)
                    return


# -----------------------------------------------------------------------------
# Main Daemon
# -----------------------------------------------------------------------------
class HubDaemon:
    """Main hub daemon coordinating all subsystems."""

    def __init__(self, config: HubConfig):
        self.cfg = config
        self.ducking = None  # created in run() where the asyncio loop exists
        self.mqtt = MQTTBridge(config)
        self.ir = IRHandler(config)
        self.audio = AudioControl(config)

        # Wire up callbacks
        self.mqtt.on_volume_command("tv", self._on_tv_volume)
        self.mqtt.on_volume_command("bt", self._on_bt_volume)
        self.mqtt.on_volume_command("music", self._on_music_volume)
        self.mqtt.on_volume_command("master", self._on_master_volume)
        self.mqtt.on_mute_command(self._on_mute_command)
        self.mqtt.on_ducking_command(self._on_ducking_command)

        # Line-out sink reference
        self._lineout_sink = None

    def _on_ducking_state_change(self, ducked: bool):
        """Handle ducking state changes."""
        asyncio.create_task(self.mqtt.publish_ducking_state(
            self.cfg.ducking_enabled, ducked
        ))

# Volume handlers moved to individual methods

    def _on_mute_command(self, muted: bool):
        """Handle mute commands from MQTT."""
        sink = self.audio.get_lineout_sink()
        if sink:
            self.audio.set_mute(sink, muted)
            asyncio.create_task(self.mqtt.publish_state("master", muted=muted))

    def _on_ducking_command(self, enabled: bool):
        """Handle ducking enable commands from MQTT."""
        log.info(f"Ducking enable command: {enabled}")
        if self.ducking is not None:
            self.ducking.set_enabled(enabled)

    def _on_master_volume(self, level: float):
        """Handle master volume commands."""
        sink = self.audio.get_lineout_sink()
        if sink:
            self.audio.set_volume(sink, level)
            asyncio.create_task(self.mqtt.publish_state("master", volume=level))

    def _on_tv_volume(self, level: float):
        """Handle TV volume commands."""
        self.audio.set_bus_volume(self.cfg.bus_tv, level)
        asyncio.create_task(self.mqtt.publish_state("tv", volume=level))

    def _on_bt_volume(self, level: float):
        """Handle BT volume commands."""
        self.audio.set_bus_volume(self.cfg.bus_bt, level)
        asyncio.create_task(self.mqtt.publish_state("bt", volume=level))

    def _on_music_volume(self, level: float):
        """Handle music volume commands."""
        self.audio.set_bus_volume(self.cfg.bus_music, level)
        asyncio.create_task(self.mqtt.publish_state("music", volume=level))

    def _ir_volume_up(self):
        """Handle IR volume up."""
        sink = self.audio.get_lineout_sink()
        if sink:
            current = self.audio.get_volume(sink)
            new_vol = min(1.0, current + self.cfg.ir_volume_step)
            self.audio.set_volume(sink, new_vol)
            log.info(f"IR: volume up {current:.2f} -> {new_vol:.2f}")
            asyncio.create_task(self.mqtt.publish_state("master", volume=new_vol))

    def _ir_volume_down(self):
        """Handle IR volume down."""
        sink = self.audio.get_lineout_sink()
        if sink:
            current = self.audio.get_volume(sink)
            new_vol = max(0.0, current - self.cfg.ir_volume_step)
            self.audio.set_volume(sink, new_vol)
            log.info(f"IR: volume down {current:.2f} -> {new_vol:.2f}")
            asyncio.create_task(self.mqtt.publish_state("master", volume=new_vol))

    def _ir_mute(self):
        """Handle IR mute toggle."""
        sink = self.audio.get_lineout_sink()
        if sink:
            currently_muted = self.audio.is_muted(sink)
            self.audio.set_mute(sink, not currently_muted)
            log.info(f"IR: mute {'enabled' if not currently_muted else 'disabled'}")
            asyncio.create_task(self.mqtt.publish_state("master", muted=not currently_muted))

    async def run(self):
        """Run all subsystems."""
        log.info("Hub daemon starting")

        # Create the ducking engine now that the asyncio loop is running
        loop = asyncio.get_running_loop()
        self.ducking = DuckingEngine(self.cfg, loop)
        self.ducking.on_state_change(self._on_ducking_state_change)

        # Wire IR callbacks
        self.ir.on_volume_up(self._ir_volume_up)
        self.ir.on_volume_down(self._ir_volume_down)
        self.ir.on_mute(self._ir_mute)

        # Run all tasks
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.ducking.run())
            tg.create_task(self.mqtt.run())
            tg.create_task(self.ir.run())


# -----------------------------------------------------------------------------
# Entry Point
# -----------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Audio Hub Controller Daemon")
    parser.add_argument(
        "--config",
        default=os.environ.get("AUDIOHUB_CONFIG", "/etc/audiohub/unit.env"),
        help="Path to unit configuration file"
    )
    parser.add_argument("--version", action="version", version="%(prog)s 1.0.0")
    args = parser.parse_args()

    # Load configuration
    try:
        config = HubConfig.from_env(args.config)
        log.info(f"Loaded config from {args.config}")
        log.info(f"Device: {config.device_name} ({config.device_id})")
    except Exception as e:
        log.error(f"Failed to load config: {e}")
        sys.exit(1)

    # Run daemon
    daemon = HubDaemon(config)
    try:
        asyncio.run(daemon.run())
    except KeyboardInterrupt:
        log.info("Shutting down")


if __name__ == "__main__":
    main()
