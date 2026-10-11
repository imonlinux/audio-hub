#!/usr/bin/env python3
"""Unit tests for hubd's configurable IR key mapping (spec Section 7.1).

Covers: IRMapStore persistence and validation, key dispatch (mapped, ignore,
unmapped-once, ducking path), hold-to-repeat rules, the bind-capture state
machine (arm, bind, rebind, timeout, dispatch suppression, status transitions),
direct per-key select commands, and the IR discovery payloads.

Run from the repo root:

    .venv/bin/python tests/test_ir_mapping.py

(The repo .venv needs pulsectl/paho-mqtt/evdev — the same system packages the
Pi installs.) No audio stack or broker required; MQTT publishes are recorded
via a stub client.
"""

import asyncio
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
            self.assertEqual(store.snapshot(), dict(hubd.IR_FACTORY_MAP))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "ir_map.json")
            store = hubd.IRMapStore(path)
            store.set("KEY_PLAYPAUSE", "ignore")
            store.set("KEY_MUTE", "ducking_toggle")
            again = hubd.IRMapStore(path)
            self.assertEqual(again.get("KEY_PLAYPAUSE"), "ignore")
            self.assertEqual(again.get("KEY_MUTE"), "ducking_toggle")
            # factory entries survive alongside user entries
            self.assertEqual(again.get("KEY_VOLUMEUP"), "volume_up")
            with open(path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["KEY_PLAYPAUSE"], "ignore")

    def test_corrupt_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                f.write("{not json")
            store = hubd.IRMapStore(path)
            self.assertEqual(store.snapshot(), dict(hubd.IR_FACTORY_MAP))

    def test_non_object_file_falls_back_to_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump(["not", "an", "object"], f)
            store = hubd.IRMapStore(path)
            self.assertEqual(store.snapshot(), dict(hubd.IR_FACTORY_MAP))

    def test_invalid_key_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"NOT_A_KEY_NAME": "volume_up",
                           "KEY_PLAYPAUSE": "ignore"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            self.assertIsNone(store.get("NOT_A_KEY_NAME"))
            self.assertEqual(store.get("KEY_PLAYPAUSE"), "ignore")

    def test_invalid_action_dropped(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_PLAYPAUSE": "explode"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            self.assertIsNone(store.get("KEY_PLAYPAUSE"))

    def test_factory_entry_cannot_be_removed_by_file(self):
        # A file entry cannot erase a factory binding; it can only rebind it.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_VOLUMEUP": "ignore"}, f)
            store = hubd.IRMapStore(path)
            self.assertEqual(store.get("KEY_VOLUMEUP"), "ignore")


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

    def test_ducking_toggle_reaches_ducking_path(self):
        with HubDaemonFixture() as daemon:
            daemon.ir_map.set("KEY_MUTE", "ducking_toggle")
            self.press(daemon, "KEY_MUTE")
            self.assertEqual(daemon.ducking.calls, [False])
            self.press(daemon, "KEY_MUTE")
            self.assertEqual(daemon.ducking.calls, [False, True])

    def test_unmapped_key_logs_once_and_publishes_last_key(self):
        with HubDaemonFixture() as daemon:
            client = daemon.mqtt.client
            with self.assertLogs(hubd.log, level="INFO") as captured:
                self.press(daemon, "KEY_PLAYPAUSE")
                self.press(daemon, "KEY_PLAYPAUSE")
            sightings = [m for m in captured.output if "KEY_PLAYPAUSE" in m]
            self.assertEqual(len(sightings), 1)
            self.assertIn("unmapped", sightings[0])
            payload = client.by_topic().get("t_dev/ir_last_key/state")
            self.assertEqual(payload, "KEY_PLAYPAUSE")

    def test_mapped_press_publishes_last_key(self):
        with HubDaemonFixture() as daemon:
            self.press(daemon, "KEY_VOLUMEUP")
            self.assertEqual(daemon.mqtt.client.by_topic().get("t_dev/ir_last_key/state"),
                             "KEY_VOLUMEUP")

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

    def test_repeat_suppressed_while_armed(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertAlmostEqual(daemon.audio.volume, 0.5)


class TestIRCapture(unittest.TestCase):
    def status(self, client):
        return client.by_topic().get("t_dev/ir_bind/status/state")

    def test_arm_publishes_armed_status(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            self.assertEqual(self.status(daemon.mqtt.client), "armed: volume_up")
            self.assertIsNotNone(daemon._ir_bind)

    def test_bind_new_key(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_PLAYPAUSE", False)
            self.assertEqual(daemon.ir_map.get("KEY_PLAYPAUSE"), "volume_up")
            self.assertEqual(self.status(daemon.mqtt.client), "bound: KEY_PLAYPAUSE -> volume_up")
            self.assertIsNone(daemon._ir_bind)
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir/KEY_PLAYPAUSE/state"), "volume_up")
            self.assertIn("t_dev/ir_last_key/state", topics)

    def test_binding_press_does_not_fire_its_action(self):
        # Rebinding Vol+ must not jump the volume (spec 5.6 step 2).
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            self.assertAlmostEqual(daemon.audio.volume, 0.5)
            self.assertEqual(daemon.ir_map.get("KEY_VOLUMEUP"), "volume_up")

    def test_dispatch_suppressed_while_armed(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_MUTE", False)  # press commits a bind instead
            self.assertFalse(daemon.audio.muted)

    def test_rebind_existing_key(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_choose("mute_toggle")
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            self.assertEqual(daemon.ir_map.get("KEY_VOLUMEUP"), "mute_toggle")
            self.assertEqual(self.status(daemon.mqtt.client),
                             "bound: KEY_VOLUMEUP -> mute_toggle")
            persisted = hubd.IRMapStore(daemon.ir_map.path)
            self.assertEqual(persisted.get("KEY_VOLUMEUP"), "mute_toggle")

    def test_rearm_restarts_window(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            first_deadline = daemon._ir_bind["deadline"]
            daemon._on_ir_bind_arm()
            self.assertGreater(daemon._ir_bind["deadline"], first_deadline)
            self.assertEqual(self.status(daemon.mqtt.client), "armed: volume_up")

    def test_timeout(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._ir_bind["deadline"] = 0.0  # window already elapsed
            daemon._ir_bind_tick()
            self.assertEqual(self.status(daemon.mqtt.client), "timeout")
            self.assertIsNone(daemon._ir_bind)
            # factory map untouched
            self.assertEqual(daemon.ir_map.get("KEY_VOLUMEUP"), "volume_up")

    def test_bind_ignored_after_timeout(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_arm()
            daemon._ir_bind_expire()
            daemon._on_ir_key("KEY_PLAYPAUSE", False)
            self.assertIsNone(daemon.ir_map.get("KEY_PLAYPAUSE"))

    def test_status_returns_to_idle_after_terminal_result(self):
        async def scenario(daemon):
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_PLAYPAUSE", False)
            self.assertEqual(self.status(daemon.mqtt.client),
                             "bound: KEY_PLAYPAUSE -> volume_up")
            await asyncio.sleep(hubd.IR_BIND_IDLE_RETURN_S + 0.15)
            self.assertEqual(self.status(daemon.mqtt.client), "idle")

        original = hubd.IR_BIND_IDLE_RETURN_S
        hubd.IR_BIND_IDLE_RETURN_S = 0.05
        try:
            with HubDaemonFixture() as daemon:
                asyncio.run(scenario(daemon))
        finally:
            hubd.IR_BIND_IDLE_RETURN_S = original

    def test_rearm_blocks_stale_idle_return(self):
        async def scenario(daemon):
            daemon._on_ir_bind_arm()
            daemon._on_ir_key("KEY_PLAYPAUSE", False)  # terminal: bound
            daemon._on_ir_bind_arm()                   # re-arm before idle return
            armed = self.status(daemon.mqtt.client)
            await asyncio.sleep(hubd.IR_BIND_IDLE_RETURN_S + 0.15)
            # The stale return-to-idle from the bind must not clobber the re-arm.
            self.assertEqual(self.status(daemon.mqtt.client), armed)

        original = hubd.IR_BIND_IDLE_RETURN_S
        hubd.IR_BIND_IDLE_RETURN_S = 0.05
        try:
            with HubDaemonFixture() as daemon:
                asyncio.run(scenario(daemon))
        finally:
            hubd.IR_BIND_IDLE_RETURN_S = original


class TestIRKeyCommand(unittest.TestCase):
    def test_direct_set_updates_store_and_publishes(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key_command("KEY_MUTE", "ducking_toggle")
            self.assertEqual(daemon.ir_map.get("KEY_MUTE"), "ducking_toggle")
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir/KEY_MUTE/state"), "ducking_toggle")

    def test_direct_set_new_key_publishes_discovery(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key_command("KEY_PLAYPAUSE", "ignore")
            base = daemon.cfg.mqtt_base_topic
            self.assertIn(f"{base}/select/t_dev_ir_key_playpause/config",
                          daemon.mqtt.client.by_topic())

    def test_unknown_key_rejected(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key_command("NOT_A_KEY_NAME", "volume_up")
            self.assertIsNone(daemon.ir_map.get("NOT_A_KEY_NAME"))
            self.assertNotIn("t_dev/ir/NOT_A_KEY_NAME/state",
                             daemon.mqtt.client.by_topic())

    def test_unknown_action_rejected(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key_command("KEY_MUTE", "explode")
            self.assertEqual(daemon.ir_map.get("KEY_MUTE"), "mute_toggle")


class TestIRBindChoose(unittest.TestCase):
    def test_choose_publishes_state(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_choose("ducking_toggle")
            self.assertEqual(daemon._ir_bind_chosen, "ducking_toggle")
            self.assertEqual(daemon.mqtt.client.by_topic().get("t_dev/ir_bind/choose/state"),
                             "ducking_toggle")

    def test_invalid_choice_ignored(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_bind_choose("ignore")     # not a bind target
            daemon._on_ir_bind_choose("explode")    # not an action
            self.assertEqual(daemon._ir_bind_chosen, "volume_up")


class TestIRDiscovery(unittest.TestCase):
    def _bridge_with_map(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = tmp.name
        self.addCleanup(setitem_home, old_home)
        store = hubd.IRMapStore()
        cfg = make_config()
        return hubd.MQTTBridge(cfg, store)

    def _discovery(self, bridge):
        base = bridge.cfg.mqtt_base_topic
        availability = bridge._availability_topic()
        device = {"identifiers": ["t_dev"], "name": "T", "model": "m", "manufacturer": "c"}
        return dict(bridge._ir_discovery(base, "t_dev", availability, device))

    def test_payload_shape_and_topic_names(self):
        bridge = self._bridge_with_map()
        base = bridge.cfg.mqtt_base_topic
        by_topic = self._discovery(bridge)

        vol_up = by_topic[f"{base}/select/t_dev_ir_key_volumeup/config"]
        self.assertEqual(vol_up["name"], "IR Vol+ Action")
        self.assertEqual(vol_up["unique_id"], "t_dev_ir_key_volumeup")
        self.assertEqual(vol_up["state_topic"], "t_dev/ir/KEY_VOLUMEUP/state")
        self.assertEqual(vol_up["command_topic"], "t_dev/ir/KEY_VOLUMEUP/set")
        self.assertEqual(vol_up["options"], list(hubd.IR_ACTIONS))
        self.assertEqual(vol_up["availability_topic"], bridge._availability_topic())
        self.assertIn("device", vol_up)

        # the three factory keys each get a select
        for suffix in ("ir_key_volumeup", "ir_key_volumedown", "ir_key_mute"):
            self.assertIn(f"{base}/select/t_dev_{suffix}/config", by_topic)

        last = by_topic[f"{base}/sensor/t_dev_ir_last_key/config"]
        self.assertEqual(last["name"], "IR Last Key")
        self.assertEqual(last["state_topic"], "t_dev/ir_last_key/state")

        choose = by_topic[f"{base}/select/t_dev_ir_bind_choose/config"]
        self.assertEqual(choose["options"], list(hubd.IR_BIND_ACTIONS))
        self.assertNotIn("ignore", choose["options"])

        arm = by_topic[f"{base}/button/t_dev_ir_bind_arm/config"]
        self.assertEqual(arm["payload_press"], "PRESS")
        self.assertEqual(arm["command_topic"], "t_dev/ir_bind/arm/set")

        status = by_topic[f"{base}/sensor/t_dev_ir_bind_status/config"]
        self.assertEqual(status["state_topic"], "t_dev/ir_bind/status/state")

    def test_factory_map_drives_entity_set(self):
        bridge = self._bridge_with_map()
        base = bridge.cfg.mqtt_base_topic
        by_topic = self._discovery(bridge)
        selects = [t for t in by_topic if "/select/" in t and "_ir_" in t
                   and "bind" not in t]
        self.assertEqual(len(selects), 3)

    def test_initial_state_publishes_key_states_choose_and_idle(self):
        bridge = self._bridge_with_map()
        bridge.client = RecordingClient()
        bridge.attach_state_provider(lambda: {})
        bridge._publish_initial_state()
        topics = bridge.client.by_topic()
        self.assertEqual(topics.get("t_dev/ir/KEY_VOLUMEUP/state"), "volume_up")
        self.assertEqual(topics.get("t_dev/ir/KEY_VOLUMEDOWN/state"), "volume_down")
        self.assertEqual(topics.get("t_dev/ir/KEY_MUTE/state"), "mute_toggle")
        self.assertEqual(topics.get("t_dev/ir_bind/choose/state"), "volume_up")
        self.assertEqual(topics.get("t_dev/ir_bind/status/state"), "idle")


def setitem_home(value):
    if value is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = value


if __name__ == "__main__":
    unittest.main(verbosity=2)
