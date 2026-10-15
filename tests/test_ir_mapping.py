#!/usr/bin/env python3
"""Unit tests for hubd's configurable IR key mapping core (spec Section 4.4).

Covers: IRMapStore v2 persistence and validation, key dispatch (mapped,
ignore, unmapped-once, ducking path, event publishing), and hold-to-repeat
rules. The IR editor flows, map v2 migration, Layer 1 entity retirement, and
the IR Remote event entity are covered in tests/test_ir_editor.py.

Run from the repo root:

    .venv/bin/python tests/test_ir_mapping.py

(The repo .venv needs pulsectl/paho-mqtt/evdev — the same system packages the
Pi installs.) No audio stack or broker required; MQTT publishes are recorded
via a stub client.
"""

import json
import os
import sys
import tempfile
import unittest

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
    """Line-out surface hubd's IR dispatch touches (no Pulse connection)."""

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


class TestIRMapStore(unittest.TestCase):
    def test_absent_file_is_factory_map(self):
        with tempfile.TemporaryDirectory() as d:
            store = hubd.IRMapStore(os.path.join(d, "ir_map.json"))
            self.assertEqual(store.mapped(), dict(hubd.IR_FACTORY_MAP))
            self.assertEqual(store.bindings(), {})
            self.assertEqual(store.seen(), [])

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "ir_map.json")
            store = hubd.IRMapStore(path)
            store.set("KEY_MUTE", "ducking_toggle")
            again = hubd.IRMapStore(path)
            self.assertEqual(again.get("KEY_MUTE"), "ducking_toggle")
            # factory entries survive alongside user entries
            self.assertEqual(again.get("KEY_VOLUMEUP"), "volume_up")
            with open(path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["version"], 2)
            self.assertEqual(on_disk["bindings"]["KEY_MUTE"], "ducking_toggle")

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                f.write("{not json")
            store = hubd.IRMapStore(path)
            self.assertEqual(store.mapped(), dict(hubd.IR_FACTORY_MAP))

    def test_non_object_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump(["not", "an", "object"], f)
            store = hubd.IRMapStore(path)
            self.assertEqual(store.mapped(), dict(hubd.IR_FACTORY_MAP))

    def test_invalid_key_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"NOT_A_KEY_NAME": "volume_up",
                           "KEY_PLAYPAUSE": "none"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            self.assertIsNone(store.get("NOT_A_KEY_NAME"))
            self.assertEqual(store.get("KEY_PLAYPAUSE"), "none")

    def test_invalid_action_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_PLAYPAUSE": "explode"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            self.assertIsNone(store.get("KEY_PLAYPAUSE"))

    def test_tombstone_blocks_factory_binding(self):
        # "none" is an explicit unbind: the factory overlay must not re-add
        # the binding (Section 4.4 map v2).
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            store = hubd.IRMapStore(path)
            store.set("KEY_VOLUMEUP", "none")
            again = hubd.IRMapStore(path)
            self.assertEqual(again.get("KEY_VOLUMEUP"), "none")
            self.assertNotIn("KEY_VOLUMEUP", again.mapped())

    def test_set_rejects_unknown_action(self):
        with tempfile.TemporaryDirectory() as d:
            store = hubd.IRMapStore(os.path.join(d, "ir_map.json"))
            with self.assertRaises(ValueError):
                store.set("KEY_MUTE", "explode")

    def test_record_seen_most_recent_first_capped(self):
        # 37 distinct VALID evdev keys: KEY_A..KEY_Z (26) + KEY_F1..KEY_F11.
        # (Linux has no KEY_F0 and stops at KEY_F24; invalid names would be
        # refused by record_seen.)
        keys = [f"KEY_{chr(ord('A') + i)}" for i in range(26)]
        keys += [f"KEY_F{i}" for i in range(1, 12)]
        self.assertEqual(len(keys), hubd.IRMapStore.SEEN_CAP + 5)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            store = hubd.IRMapStore(path)
            added = set()
            for key in keys:
                self.assertEqual(store.record_seen(key), key not in added)
                added.add(key)
            self.assertEqual(len(store.seen()), hubd.IRMapStore.SEEN_CAP)
            self.assertEqual(store.seen()[0], "KEY_F11")  # most recent first
            self.assertNotIn("KEY_A", store.seen())       # oldest evicted
            again = hubd.IRMapStore(path)
            self.assertEqual(again.seen(), store.seen())

    def test_mapped_excludes_inert_and_never_bound(self):
        with tempfile.TemporaryDirectory() as d:
            store = hubd.IRMapStore(os.path.join(d, "ir_map.json"))
            store.set("KEY_MUTE", "none")
            store.set("KEY_PLAYPAUSE", "volume_up")
            self.assertEqual(store.mapped(), {
                "KEY_VOLUMEUP": "volume_up",
                "KEY_VOLUMEDOWN": "volume_down",
                "KEY_PLAYPAUSE": "volume_up",
            })


class TestIRKeyEcodes(unittest.TestCase):
    def test_factory_keys_resolve(self):
        for name in hubd.IR_FACTORY_MAP:
            self.assertIsNotNone(hubd.ECODE_TO_KEY_NAME.get(
                getattr(hubd.evdev.ecodes, name)))

    def test_translation_is_injective_per_canonical_name(self):
        # Each canonical KEY_* name maps back to itself through the reverse map.
        for name in ("KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE", "KEY_PLAYPAUSE"):
            code = getattr(hubd.evdev.ecodes, name)
            self.assertEqual(hubd.ECODE_TO_KEY_NAME.get(code), name)


class TestIRDispatch(unittest.TestCase):
    def press(self, daemon, key_name):
        daemon._on_ir_key(key_name, False)

    def test_mapped_volume_up_fires(self):
        with HubDaemonFixture() as daemon:
            self.press(daemon, "KEY_VOLUMEUP")
            self.assertAlmostEqual(daemon.audio.volume, 0.5 + daemon.cfg.ir_volume_step)

    def test_mapped_volume_down_fires(self):
        with HubDaemonFixture() as daemon:
            self.press(daemon, "KEY_VOLUMEDOWN")
            self.assertAlmostEqual(daemon.audio.volume, 0.5 - daemon.cfg.ir_volume_step)

    def test_mapped_mute_toggles(self):
        with HubDaemonFixture() as daemon:
            self.press(daemon, "KEY_MUTE")
            self.assertTrue(daemon.audio.muted)

    def test_ignore_is_inert(self):
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_MUTE", "ignore")
            self.press(daemon, "KEY_MUTE")
            self.assertFalse(daemon.audio.muted)

    def test_none_is_inert(self):
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_MUTE", "none")
            self.press(daemon, "KEY_MUTE")
            self.assertFalse(daemon.audio.muted)

    def test_ducking_toggle_reaches_ducking_path(self):
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_MUTE", "ducking_toggle")
            self.press(daemon, "KEY_MUTE")
            self.assertEqual(daemon.ducking.calls, [False])
            self.press(daemon, "KEY_MUTE")
            self.assertEqual(daemon.ducking.calls, [False, True])

    def test_unmapped_key_logs_once_and_fires_event(self):
        with HubDaemonFixture() as daemon:
            client = daemon.mqtt.client
            with self.assertLogs(hubd.log, level="INFO") as captured:
                self.press(daemon, "KEY_PLAYPAUSE")
                self.press(daemon, "KEY_PLAYPAUSE")
            sightings = [m for m in captured.output if "KEY_PLAYPAUSE" in m
                         and "unmapped" in m]
            self.assertEqual(len(sightings), 1)
            payload = json.loads(
                client.by_topic()["t_dev/ir/event"])
            self.assertEqual(payload["event_type"], "KEY_PLAYPAUSE")
            self.assertEqual(payload["action"], "none")
            self.assertEqual(payload["label"], "Playpause")

    def test_mapped_press_fires_event_and_action(self):
        with HubDaemonFixture() as daemon:
            self.press(daemon, "KEY_VOLUMEUP")
            payload = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/ir/event"])
            self.assertEqual(payload, {"event_type": "KEY_VOLUMEUP",
                                       "action": "volume_up",
                                       "label": "Volume Up"})
            self.assertAlmostEqual(daemon.audio.volume,
                                   0.5 + daemon.cfg.ir_volume_step)

    def test_worker_translates_press_and_repeat(self):
        with HubDaemonFixture() as daemon:
            seen = []
            handler = hubd.IRHandler(daemon.cfg, daemon.stop_event)

            class FakeLoop:
                def call_soon_threadsafe(self, cb, *args):
                    seen.append((cb, args))

            handler.set_loop(FakeLoop())
            handler.on_key(lambda name, rep: None)
            handler._handle(type("E", (), {"type": hubd.evdev.ecodes.EV_KEY,
                                           "value": 1,
                                           "code": hubd.evdev.ecodes.KEY_VOLUMEUP})())
            handler._handle(type("E", (), {"type": hubd.evdev.ecodes.EV_KEY,
                                           "value": 2,
                                           "code": hubd.evdev.ecodes.KEY_VOLUMEUP})())
            handler._handle(type("E", (), {"type": hubd.evdev.ecodes.EV_KEY,
                                           "value": 0,
                                           "code": hubd.evdev.ecodes.KEY_VOLUMEUP})())
            handler._handle(type("E", (), {"type": 99, "value": 1,
                                           "code": hubd.evdev.ecodes.KEY_VOLUMEUP})())
            self.assertEqual([(args[0], args[1]) for _, args in seen],
                             [("KEY_VOLUMEUP", False), ("KEY_VOLUMEUP", True)])


class TestIRRepeat(unittest.TestCase):
    def test_repeat_steps_volume_when_enabled(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5 + daemon.cfg.ir_volume_step)

    def test_repeat_never_fires_toggles(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key("KEY_MUTE", True)
            self.assertFalse(daemon.audio.muted)
            daemon.ir_map.set("KEY_MUTE", "ducking_toggle")
            daemon._on_ir_key("KEY_MUTE", True)
            self.assertEqual(daemon.ducking.calls, [])

    def test_repeat_on_toggling_bound_volume_key_fires(self):
        # Repeat applies to the ACTION (volume), not the physical key.
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_PLAYPAUSE", "volume_up")
            daemon._on_ir_key("KEY_PLAYPAUSE", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5 + daemon.cfg.ir_volume_step)

    def test_repeat_disabled_by_config(self):
        with HubDaemonFixture() as daemon:
            daemon.cfg.ir_repeat = False
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5)
            # strict press-only: real presses still fire
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            self.assertAlmostEqual(daemon.audio.volume, 0.5 + daemon.cfg.ir_volume_step)

    def test_repeat_suppressed_while_identify_armed(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5)



def setitem_home(value):
    if value is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
