#!/usr/bin/env python3
"""Unit tests for hubd's runtime settings (ha-configuration-spec Sections
4.2, 4.3, 4.6).

Covers: SettingsStore precedence/clamping/validation/reset, the HA settings
command path (percent travel, clamped echo, reset button), the IR repeat
rate limiter, discovery payload categories/origin, and the ducking engine's
live runtime readers.

Run from the repo root:

    .venv/bin/python tests/test_settings.py

No audio stack or broker required; MQTT publishes are recorded via a stub
client.
"""

import json
import os
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hubd"))
import main as hubd  # noqa: E402


class RecordingClient:
    """Stub MQTT client: records every publish instead of sending."""

    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False):
        self.published.append((topic, payload))
        return True

    def by_topic(self):
        return {t: p for t, p in self.published}


class FakeAudio:
    def __init__(self):
        self.volume = 0.5
        self.muted = False

    def get_lineout_volume(self):
        return self.volume

    def set_lineout_volume(self, level):
        self.volume = level

    def get_lineout_mute(self):
        return self.muted

    def set_lineout_mute(self, muted):
        self.muted = muted


class FakeDucking:
    def __init__(self):
        self.ducking_enabled = True
        self.calls = []

    def set_enabled(self, enabled):
        self.calls.append(enabled)
        self.ducking_enabled = enabled


def make_config(**overrides):
    kwargs = dict(
        hostname="t", device_id="t_dev", device_name="T",
        mqtt_host="x", mqtt_port=1883, mqtt_username="u", mqtt_password="p")
    kwargs.update(overrides)
    return hubd.HubConfig(**kwargs)


class HubDaemonFixture:
    """HubDaemon isolated from HOME and the audio stack."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        self.daemon = hubd.HubDaemon(make_config())
        self.daemon.audio = FakeAudio()
        self.daemon.ducking = FakeDucking()
        self.client = RecordingClient()
        self.daemon.mqtt.client = self.client
        return self.daemon

    def __exit__(self, *exc):
        if self._old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()


class ImmediateLoop:
    """Runs call_soon_threadsafe callbacks synchronously (tests the full
    paho _on_message -> loop -> handler path without a real loop)."""

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def mqtt_msg(topic: str, payload: str):
    return type("M", (), {"topic": topic, "payload": payload.encode()})()


class TestSettingsStore(unittest.TestCase):
    def test_absent_file_falls_through_to_bootstrap(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(duck_level=0.3)
            store = hubd.SettingsStore(cfg, os.path.join(d, "settings.json"))
            self.assertEqual(store.get("duck_level"), 0.3)
            self.assertEqual(store.overrides(), {})

    def test_override_beats_bootstrap(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(duck_level=0.3)
            path = os.path.join(d, "settings.json")
            store = hubd.SettingsStore(cfg, path)
            store.set("duck_level", 0.5)
            self.assertEqual(store.get("duck_level"), 0.5)
            again = hubd.SettingsStore(cfg, path)
            self.assertEqual(again.get("duck_level"), 0.5)
            # untouched key still falls through
            self.assertEqual(again.get("duck_restore_delay"),
                             cfg.duck_restore_delay)

    def test_set_clamps_to_range(self):
        with tempfile.TemporaryDirectory() as d:
            store = hubd.SettingsStore(
                make_config(), os.path.join(d, "s.json"))
            self.assertEqual(store.set("duck_level", 1.7), 1.0)
            self.assertEqual(store.set("ir_repeat_interval_ms", 10), 100)
            self.assertEqual(store.get("ir_repeat_interval_ms"), 100)

    def test_set_rejects_bad_select_option(self):
        with tempfile.TemporaryDirectory() as d:
            store = hubd.SettingsStore(
                make_config(), os.path.join(d, "s.json"))
            with self.assertRaises(ValueError):
                store.set("release_channel", "beta")
            self.assertEqual(store.get("release_channel"), "stable")

    def test_load_clamps_out_of_range(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"version": 1, "values":
                           {"duck_level": 4.0, "duck_restore_delay": -3}}, f)
            store = hubd.SettingsStore(make_config(), path)
            self.assertEqual(store.get("duck_level"), 1.0)
            self.assertEqual(store.get("duck_restore_delay"), 0.0)

    def test_load_drops_unknown_and_invalid(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"version": 1, "values":
                           {"not_a_setting": 1,
                            "release_channel": "nope",
                            "duck_level": "oops"}}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.SettingsStore(make_config(), path)
            self.assertEqual(store.overrides(), {})

    def test_corrupt_file_falls_back_to_bootstrap(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                f.write("{not json")
            cfg = make_config(ir_repeat=False)
            store = hubd.SettingsStore(cfg, path)
            self.assertIs(store.get("ir_repeat"), False)

    def test_non_dict_values_ignored(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "settings.json")
            with open(path, "w") as f:
                json.dump({"version": 1, "values": ["nope"]}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.SettingsStore(make_config(), path)
            self.assertEqual(store.overrides(), {})

    def test_reset_wipes_overrides(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(duck_level=0.3)
            path = os.path.join(d, "settings.json")
            store = hubd.SettingsStore(cfg, path)
            store.set("duck_level", 0.9)
            store.reset()
            self.assertEqual(store.get("duck_level"), 0.3)
            again = hubd.SettingsStore(cfg, path)
            self.assertEqual(again.overrides(), {})

    def test_every_def_has_a_bootstrap_attribute(self):
        cfg = make_config()
        for key in hubd.SETTING_DEFS:
            self.assertTrue(hasattr(cfg, key),
                            f"SETTING_DEFS key {key} has no HubConfig field")


class TestSettingsCommands(unittest.TestCase):
    def send(self, daemon, key, payload):
        daemon.mqtt._loop = ImmediateLoop()
        daemon.mqtt._on_message(
            None, None, mqtt_msg(f"t_dev/setting/{key}/set", payload))

    def state(self, daemon, key):
        return daemon.mqtt.client.by_topic().get(f"t_dev/setting/{key}/state")

    def test_percent_command_travels_0_100(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_level", "35")
            self.assertAlmostEqual(daemon.settings.get("duck_level"), 0.35)
            self.assertEqual(self.state(daemon, "duck_level"), "35")

    def test_out_of_range_command_is_clamped_and_echoed(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_level", "150")
            self.assertEqual(daemon.settings.get("duck_level"), 1.0)
            self.assertEqual(self.state(daemon, "duck_level"), "100")

    def test_invalid_command_ignored(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_level", "loud")
            self.assertNotIn("t_dev/setting/duck_level/state",
                             daemon.mqtt.client.by_topic())
            self.assertEqual(daemon.settings.overrides(), {})

    def test_bool_and_int_commands(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "ir_repeat", "OFF")
            self.assertIs(daemon.settings.get("ir_repeat"), False)
            self.assertEqual(self.state(daemon, "ir_repeat"), "OFF")
            self.send(daemon, "ir_repeat_interval_ms", "250")
            self.assertEqual(daemon.settings.get("ir_repeat_interval_ms"), 250)
            self.assertEqual(self.state(daemon, "ir_repeat_interval_ms"), "250")

    def test_select_command(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "release_channel", "canary")
            self.assertEqual(daemon.settings.get("release_channel"), "canary")
            self.send(daemon, "release_channel", "beta")
            self.assertEqual(daemon.settings.get("release_channel"), "canary")

    def test_unknown_key_ignored(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "wifi_password", "hunter2")
            self.assertNotIn("wifi_password", daemon.settings.overrides())

    def test_float_command_formats_without_trailing_zero(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_restore_delay", "4.5")
            self.assertEqual(self.state(daemon, "duck_restore_delay"), "4.5")
            self.send(daemon, "duck_restore_delay", "4")
            self.assertEqual(self.state(daemon, "duck_restore_delay"), "4")

    def test_reset_button_wipes_and_republishes(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_level", "90")
            self.send(daemon, "ir_repeat", "OFF")
            self.send(daemon, "reset", "PRESS")
            self.assertEqual(daemon.settings.overrides(), {})
            # fall-through values republished for every defined setting
            for key in hubd.SETTING_DEFS:
                self.assertIn(f"t_dev/setting/{key}/state",
                              daemon.mqtt.client.by_topic())
            self.assertEqual(self.state(daemon, "duck_level"), "20")
            self.assertEqual(self.state(daemon, "ir_repeat"), "ON")

    def test_reset_ignores_other_payloads(self):
        with HubDaemonFixture() as daemon:
            self.send(daemon, "duck_level", "90")
            self.send(daemon, "reset", "PRESSURE")
            self.assertEqual(daemon.settings.get("duck_level"), 0.9)

    def test_ducking_switch_persists(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ducking_command(False)
            self.assertEqual(daemon.ducking.calls, [False])
            self.assertIs(daemon.settings.get("ducking_enabled"), False)
            with open(daemon.settings.path) as f:
                on_disk = json.load(f)
            self.assertIs(on_disk["values"]["ducking_enabled"], False)


class TestRepeatLimiter(unittest.TestCase):
    def test_first_repeat_passes_rapid_second_dropped(self):
        with HubDaemonFixture() as daemon:
            clock = {"t": 1000.0}
            with mock.patch.object(hubd.time, "monotonic",
                                   side_effect=lambda: clock["t"]):
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                self.assertAlmostEqual(daemon.audio.volume,
                                       0.5 + daemon.cfg.ir_volume_step)
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                # inside the 150 ms window: dropped
                self.assertAlmostEqual(daemon.audio.volume,
                                       0.5 + daemon.cfg.ir_volume_step)
                clock["t"] += 0.2
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                self.assertAlmostEqual(daemon.audio.volume,
                                       0.5 + 2 * daemon.cfg.ir_volume_step)

    def test_interval_follows_runtime_setting(self):
        with HubDaemonFixture() as daemon:
            daemon.settings.set("ir_repeat_interval_ms", 1000)
            clock = {"t": 1000.0}
            with mock.patch.object(hubd.time, "monotonic",
                                   side_effect=lambda: clock["t"]):
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                clock["t"] += 0.2  # past the old 150 ms, inside 1 s
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                self.assertAlmostEqual(daemon.audio.volume,
                                       0.5 + daemon.cfg.ir_volume_step)

    def test_repeat_steps_stay_off_mqtt_press_publishes(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key("KEY_VOLUMEUP", True)  # repeat
            self.assertNotIn("t_dev/volume/state",
                             daemon.mqtt.client.by_topic())
            daemon._on_ir_key("KEY_VOLUMEUP", False)  # real press
            self.assertIn("t_dev/volume/state", daemon.mqtt.client.by_topic())

    def test_limiter_is_per_key(self):
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_PLAYPAUSE", "volume_up")
            clock = {"t": 1000.0}
            with mock.patch.object(hubd.time, "monotonic",
                                   side_effect=lambda: clock["t"]):
                daemon._on_ir_key("KEY_VOLUMEUP", True)
                daemon._on_ir_key("KEY_PLAYPAUSE", True)
                # a different key inside the window is not rate-limited
                self.assertAlmostEqual(daemon.audio.volume,
                                       0.5 + 2 * daemon.cfg.ir_volume_step)

    def test_disabled_by_setting(self):
        with HubDaemonFixture() as daemon:
            daemon.settings.set("ir_repeat", False)
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5)


class TestDiscoveryCategories(unittest.TestCase):
    def discover(self, daemon):
        daemon.mqtt._publish_discovery(daemon.mqtt.client)
        out = {}
        for topic, payload in daemon.mqtt.client.published:
            if topic.endswith("/config"):
                out[topic] = json.loads(payload)
        return out

    def test_origin_and_sw_version_on_every_payload(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            self.assertTrue(configs)
            for topic, payload in configs.items():
                self.assertEqual(payload.get("origin", {}).get("name"),
                                 "audio-hub", topic)
                self.assertEqual(payload["origin"]["sw"],
                                 hubd.audiohub_version(), topic)
                self.assertEqual(payload["device"]["sw_version"],
                                 hubd.audiohub_version(), topic)

    def test_activity_sensors_are_diagnostic(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            for slug in ("ducking_active", "music_active", "bt_active",
                         "tv_active", "hub_availability"):
                payload = configs[f"homeassistant/binary_sensor/t_dev_{slug}/config"]
                self.assertEqual(payload["entity_category"], "diagnostic", slug)

    def test_hub_availability_has_no_self_availability(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            payload = configs["homeassistant/binary_sensor/t_dev_hub_availability/config"]
            self.assertNotIn("availability_topic", payload)
            self.assertEqual(payload["payload_on"], "online")
            self.assertEqual(payload["payload_off"], "offline")

    def test_selection_selects_are_config(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            for key in ("output", "tv_source"):
                payload = configs[f"homeassistant/select/t_dev_{key}/config"]
                self.assertEqual(payload["entity_category"], "config", key)

    def test_settings_entities_present_with_ranges(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            duck = configs["homeassistant/number/t_dev_setting_duck_level/config"]
            self.assertEqual(duck["entity_category"], "config")
            self.assertEqual(duck["min"], 0)
            self.assertEqual(duck["max"], 100)
            self.assertEqual(duck["step"], 5)
            self.assertEqual(duck["mode"], "slider")
            interval = configs[
                "homeassistant/number/t_dev_setting_ir_repeat_interval_ms/config"]
            self.assertFalse(interval["enabled_by_default"])
            self.assertEqual(interval["unit_of_measurement"], "ms")
            channel = configs[
                "homeassistant/select/t_dev_setting_release_channel/config"]
            self.assertEqual(channel["options"], ["stable", "canary"])
            reset = configs["homeassistant/button/t_dev_setting_reset/config"]
            self.assertEqual(reset["payload_press"], "PRESS")
            self.assertFalse(reset["enabled_by_default"])
            for key in hubd.SETTING_DEFS:
                # ducking_enabled keeps the existing {id}/ducking switch
                # (Section 4.3, first table row); reset is not a SETTING.
                if key == "ducking_enabled":
                    continue
                component = ("select" if key == "release_channel" else None)
                if component:
                    self.assertIn(
                        f"homeassistant/{component}/t_dev_setting_{key}/config",
                        configs, key)
                    continue
                prefix = "switch" if SETTING_IS_SWITCH(key) else "number"
                self.assertIn(f"homeassistant/{prefix}/t_dev_setting_{key}/config",
                              configs, key)

    def test_volume_controls_stay_uncategorized(self):
        with HubDaemonFixture() as daemon:
            configs = self.discover(daemon)
            for slug in ("master_volume", "mute", "ducking"):
                comp = "number" if slug.endswith("volume") else "switch"
                payload = configs[f"homeassistant/{comp}/t_dev_{slug}/config"]
                self.assertNotIn("entity_category", payload, slug)


def SETTING_IS_SWITCH(key: str) -> bool:
    return key in ("ducking_enabled", "ir_repeat", "auto_update")


class TestEngineRuntimeSettings(unittest.TestCase):
    class FakeSI:
        def __init__(self, node, level):
            self.index = 7
            self.proplist = {"node.name": node}
            self.volume = type("Vol", (), {"values": [level]})()

    class FakePulse:
        def __init__(self, inputs):
            self.inputs = inputs
            self.set_calls = []

        def sink_input_list(self):
            return self.inputs

        def sink_input_volume_set(self, index, v):
            self.set_calls.append(list(v.values))

    def engine(self, cfg, settings):
        e = hubd.DuckingEngine(cfg, None, threading.Event(), None, settings)
        e._pulse = self.FakePulse(
            [self.FakeSI("duct.tv.playback", 1.0),
             self.FakeSI("duct.bt.playback", 1.0)])
        return e

    def test_duck_level_read_live_from_settings(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(duck_level=0.2)
            settings = hubd.SettingsStore(cfg, os.path.join(d, "s.json"))
            engine = self.engine(cfg, settings)
            engine.is_ducked = True
            engine._verify_duct_levels()
            expected = 0.2 ** (1.0 / 3.0)
            for call in engine._pulse.set_calls:
                self.assertAlmostEqual(call[0], expected, places=6)
            # live change applies on the next poll, no restart
            settings.set("duck_level", 0.5)
            engine._pulse.set_calls.clear()
            engine._verify_duct_levels()
            for call in engine._pulse.set_calls:
                self.assertAlmostEqual(call[0], 0.5 ** (1.0 / 3.0), places=6)

    def test_setting_helper_falls_through_to_cfg(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = make_config(tv_probe_threshold=555.0)
            settings = hubd.SettingsStore(cfg, os.path.join(d, "s.json"))
            engine = self.engine(cfg, settings)
            self.assertEqual(engine._setting("tv_probe_threshold"), 555.0)
            settings.set("tv_probe_threshold", 700.0)
            self.assertEqual(engine._setting("tv_probe_threshold"), 700.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
