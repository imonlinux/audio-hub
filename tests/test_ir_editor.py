#!/usr/bin/env python3
"""Unit tests for the IR editor, map v2 migration, Layer 1 entity retirement,
and the IR Remote event entity (spec Sections 4.4/4.5).

Run from the repo root:

    .venv/bin/python tests/test_ir_editor.py

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


def setitem_home(value):
    if value is None:
        os.environ.pop("HOME", None)
    else:
        os.environ["HOME"] = value


class HubDaemonFixture:
    """HubDaemon isolated from HOME and the audio stack."""

    def __init__(self, v1_map=None):
        self.v1_map = v1_map

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        if self.v1_map is not None:
            cfg_dir = os.path.join(self._tmp.name, ".config", "audiohub")
            os.makedirs(cfg_dir, exist_ok=True)
            with open(os.path.join(cfg_dir, "ir_map.json"), "w") as f:
                json.dump(self.v1_map, f)
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


def discovery_by_topic(bridge):
    base = bridge.cfg.mqtt_base_topic
    availability = bridge._availability_topic()
    device = {"identifiers": ["t_dev"], "name": "T", "model": "m", "manufacturer": "c"}
    return base, dict(bridge._ir_discovery(base, "t_dev", availability, device))


class TestMapV2Migration(unittest.TestCase):
    def test_v1_flat_dict_loads_as_bindings_ignore_becomes_none(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_PLAYPAUSE": "ignore",
                           "KEY_F13": "volume_up"}, f)
            store = hubd.IRMapStore(path)
            self.assertEqual(store.get("KEY_PLAYPAUSE"), "none")
            self.assertEqual(store.get("KEY_F13"), "volume_up")
            # load never rewrites; the file becomes v2 on the next change
            with open(path) as f:
                self.assertNotIn("version", json.load(f))
            store.set("KEY_F13", "none")
            with open(path) as f:
                on_disk = json.load(f)
            self.assertEqual(on_disk["version"], 2)
            self.assertEqual(on_disk["bindings"]["KEY_PLAYPAUSE"], "none")

    def test_legacy_keys_collected_before_validation(self):
        # Dropped entries (unknown key, unknown action) still surface in
        # legacy_keys so the retirement sweep clears their entities.
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_PLAYPAUSE": "ignore",
                           "NOT_A_KEY_NAME": "volume_up",
                           "KEY_F13": "explode"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            self.assertEqual(store.legacy_keys(),
                             {"KEY_PLAYPAUSE", "NOT_A_KEY_NAME", "KEY_F13"})

    def test_v2_file_keeps_seen_and_no_legacy(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            store = hubd.IRMapStore(path)
            store.record_seen("KEY_PLAYPAUSE")
            again = hubd.IRMapStore(path)
            self.assertEqual(again.seen(), ["KEY_PLAYPAUSE"])
            self.assertEqual(again.legacy_keys(), set())

    def test_retirement_keys_union(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "ir_map.json")
            with open(path, "w") as f:
                json.dump({"KEY_PLAYPAUSE": "volume_up",
                           "NOT_A_KEY_NAME": "volume_up"}, f)
            with self.assertLogs(hubd.log, level="WARNING"):
                store = hubd.IRMapStore(path)
            store.set("KEY_MUTE", "none")
            store.record_seen("KEY_F13")
            self.assertEqual(
                store.retirement_keys(),
                {"KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE",       # factory
                 "KEY_PLAYPAUSE", "KEY_MUTE",                        # bindings
                 "KEY_F13",                                          # seen
                 "NOT_A_KEY_NAME"})                                  # legacy


class TestIRDiscovery(unittest.TestCase):
    def test_fixed_entity_set_and_payload_shape(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("HOME")
            os.environ["HOME"] = d
            try:
                bridge = hubd.MQTTBridge(make_config(), hubd.IRMapStore())
            finally:
                setitem_home(old)
        base, by_topic = discovery_by_topic(bridge)

        key_sel = by_topic[f"{base}/select/t_dev_ir_edit_key/config"]
        self.assertEqual(key_sel["name"], "IR: Key")
        self.assertEqual(key_sel["state_topic"], "t_dev/ir_edit/key/state")
        self.assertEqual(key_sel["command_topic"], "t_dev/ir_edit/key/set")
        self.assertEqual(key_sel["entity_category"], "config")
        self.assertEqual(key_sel["options"], [
            "Mute (KEY_MUTE)",
            "Volume Down (KEY_VOLUMEDOWN)",
            "Volume Up (KEY_VOLUMEUP)",
        ])

        action = by_topic[f"{base}/select/t_dev_ir_edit_action/config"]
        self.assertEqual(action["options"], list(hubd.IR_ACTION_LABELS.values()))
        self.assertEqual(action["entity_category"], "config")

        identify = by_topic[f"{base}/button/t_dev_ir_edit_identify/config"]
        self.assertEqual(identify["payload_press"], "PRESS")
        self.assertEqual(identify["command_topic"], "t_dev/ir_edit/identify/set")

        status = by_topic[f"{base}/sensor/t_dev_ir_edit_status/config"]
        self.assertEqual(status["state_topic"], "t_dev/ir_edit/status/state")
        self.assertEqual(status["entity_category"], "diagnostic")

        mappings = by_topic[f"{base}/sensor/t_dev_ir_mappings/config"]
        self.assertEqual(mappings["state_topic"], "t_dev/ir/mappings/state")
        self.assertEqual(mappings["json_attributes_topic"],
                         "t_dev/ir/mappings/attributes")

        event = by_topic[f"{base}/event/t_dev_ir_remote/config"]
        self.assertEqual(event["name"], "IR Remote")
        self.assertEqual(event["state_topic"], "t_dev/ir/event")
        self.assertEqual(event["event_types"], sorted(hubd.IR_FACTORY_MAP))

        # no Layer 1 entities in discovery
        for stale in ("ir_last_key", "ir_bind_choose", "ir_bind_arm",
                      "ir_bind_status", "ir_key_volumeup"):
            self.assertNotIn(f"{base}/select/t_dev_{stale}/config", by_topic)
            self.assertNotIn(f"{base}/sensor/t_dev_{stale}/config", by_topic)
            self.assertNotIn(f"{base}/button/t_dev_{stale}/config", by_topic)

    def test_options_follow_seen_keys(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("HOME")
            os.environ["HOME"] = d
            try:
                store = hubd.IRMapStore()
                store.record_seen("KEY_F13")
                bridge = hubd.MQTTBridge(make_config(), store)
            finally:
                setitem_home(old)
        base, by_topic = discovery_by_topic(bridge)
        key_sel = by_topic[f"{base}/select/t_dev_ir_edit_key/config"]
        self.assertIn("F13 (KEY_F13)", key_sel["options"])
        self.assertIn("KEY_F13",
                      by_topic[f"{base}/event/t_dev_ir_remote/config"]["event_types"])

    def test_initial_state_publishes_editor_defaults(self):
        with HubDaemonFixture() as daemon:
            bridge = daemon.mqtt
            bridge.attach_state_provider(lambda: {})
            bridge._publish_initial_state()
            topics = bridge.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir_edit/key/state"),
                             "Mute (KEY_MUTE)")
            self.assertEqual(topics.get("t_dev/ir_edit/action/state"),
                             "Mute toggle")
            self.assertEqual(topics.get("t_dev/ir_edit/status/state"), "idle")
            self.assertEqual(topics.get("t_dev/ir/mappings/state"), "3")
            attrs = json.loads(topics["t_dev/ir/mappings/attributes"])
            self.assertEqual(attrs, {"Mute": "mute_toggle",
                                     "Volume Down": "volume_down",
                                     "Volume Up": "volume_up"})


class TestIRRetirementSweep(unittest.TestCase):
    V1 = {"KEY_PLAYPAUSE": "ignore", "NOT_A_KEY_NAME": "volume_up"}

    def sweep_topics(self, daemon):
        daemon.mqtt._publish_discovery(daemon.mqtt.client)
        base = daemon.cfg.mqtt_base_topic
        return base, daemon.mqtt.client.by_topic()

    def test_layer1_entities_and_states_cleared(self):
        with HubDaemonFixture(v1_map=self.V1) as daemon:
            base, topics = self.sweep_topics(daemon)
            # per-key selects for factory, bound, seen, and legacy keys
            for key in ("KEY_VOLUMEUP", "KEY_VOLUMEDOWN", "KEY_MUTE",
                        "KEY_PLAYPAUSE", "NOT_A_KEY_NAME"):
                self.assertEqual(
                    topics.get(f"{base}/select/t_dev_ir_{key.lower()}/config"), "")
            # bind trio + last key sensor
            for suffix, component in (("ir_bind_choose", "select"),
                                      ("ir_bind_arm", "button"),
                                      ("ir_bind_status", "sensor"),
                                      ("ir_last_key", "sensor")):
                self.assertEqual(
                    topics.get(f"{base}/{component}/t_dev_{suffix}/config"), "")
            # retained per-key state payloads cleared
            for key in ("KEY_VOLUMEUP", "KEY_PLAYPAUSE"):
                self.assertEqual(topics.get(f"t_dev/ir/{key}/state"), "")
            # ...and the new entities are present in the same pass
            self.assertIn(f"{base}/select/t_dev_ir_edit_key/config", topics)

    def test_sweep_is_idempotent(self):
        with HubDaemonFixture(v1_map=self.V1) as daemon:
            base, topics = self.sweep_topics(daemon)
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(
                topics[f"{base}/select/t_dev_ir_key_volumeup/config"], "")

    def test_sweep_without_layer1_history_is_harmless(self):
        # A unit that skipped Layer 1 (no v1 file): sweep still runs, only
        # factory keys are targeted.
        with HubDaemonFixture() as daemon:
            base, topics = self.sweep_topics(daemon)
            self.assertEqual(
                topics.get(f"{base}/select/t_dev_ir_key_volumeup/config"), "")
            self.assertNotIn(f"{base}/select/t_dev_ir_key_playpause/config",
                             topics)


class TestIREditFlow(unittest.TestCase):
    def test_key_select_shows_current_action(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_key("Volume Down (KEY_VOLUMEDOWN)")
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir_edit/key/state"),
                             "Volume Down (KEY_VOLUMEDOWN)")
            self.assertEqual(topics.get("t_dev/ir_edit/action/state"),
                             "Volume down")

    def test_key_select_rejects_unknown_key(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_key("Fake (NOT_A_KEY_NAME)")
            self.assertNotIn("t_dev/ir_edit/key/state",
                             daemon.mqtt.client.by_topic())

    def test_action_change_applies_and_persists(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_key("Mute (KEY_MUTE)")
            daemon._on_ir_edit_action("Ducking toggle")
            self.assertEqual(daemon.ir_map.get("KEY_MUTE"), "ducking_toggle")
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir_edit/action/state"),
                             "Ducking toggle")
            self.assertEqual(topics.get("t_dev/ir/mappings/state"), "3")
            attrs = json.loads(topics["t_dev/ir/mappings/attributes"])
            self.assertEqual(attrs["Mute"], "ducking_toggle")
            persisted = hubd.IRMapStore(daemon.ir_map.path)
            self.assertEqual(persisted.get("KEY_MUTE"), "ducking_toggle")

    def test_none_unbinds_with_tombstone(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_key("Volume Up (KEY_VOLUMEUP)")
            daemon._on_ir_edit_action("None (HA event only)")
            self.assertEqual(daemon.ir_map.get("KEY_VOLUMEUP"), "none")
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir/mappings/state"), "2")
            # the key stays listed (seen) and keeps firing events
            self.press_and_check_event(daemon, "KEY_VOLUMEUP", "none")

    def press_and_check_event(self, daemon, key, action):
        n = len([t for t, _ in daemon.mqtt.client.published
                 if t == "t_dev/ir/event"])
        daemon._on_ir_key(key, False)
        events = [p for t, p in daemon.mqtt.client.published
                  if t == "t_dev/ir/event"]
        self.assertEqual(len(events), n + 1)
        self.assertEqual(json.loads(events[-1])["action"], action)

    def test_action_change_without_key_pick_uses_default(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_action("Ducking toggle")
            self.assertEqual(daemon.ir_map.get("KEY_MUTE"), "ducking_toggle")

    def test_unknown_action_option_rejected(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_key("Mute (KEY_MUTE)")
            daemon._on_ir_edit_action("Explode")
            self.assertEqual(daemon.ir_map.get("KEY_MUTE"), "mute_toggle")


class TestIRIdentifyFlow(unittest.TestCase):
    def status(self, daemon):
        return daemon.mqtt.client.by_topic().get("t_dev/ir_edit/status/state")

    def test_arm_publishes_status(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            self.assertEqual(self.status(daemon),
                             "Press a button on the remote (60 s)")
            self.assertIsNotNone(daemon._ir_identify)

    def test_capture_selects_key_without_changing_mapping(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_F13", False)
            topics = daemon.mqtt.client.by_topic()
            self.assertEqual(topics.get("t_dev/ir_edit/key/state"),
                             "F13 (KEY_F13)")
            self.assertEqual(topics.get("t_dev/ir_edit/action/state"),
                             "None (HA event only)")
            self.assertEqual(self.status(daemon),
                             "Captured: F13 (KEY_F13), choose an action")
            self.assertIsNone(daemon._ir_identify)
            # capture never changes a mapping
            self.assertIsNone(daemon.ir_map.get("KEY_F13"))

    def test_capture_of_mapped_key_shows_its_action(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            self.assertEqual(
                daemon.mqtt.client.by_topic().get("t_dev/ir_edit/action/state"),
                "Volume up")

    def test_capture_suppresses_event_and_dispatch(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            self.assertNotIn("t_dev/ir/event", daemon.mqtt.client.by_topic())
            self.assertAlmostEqual(daemon.audio.volume, 0.5)

    def test_capture_adds_key_to_options(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_F13", False)
            base = daemon.cfg.mqtt_base_topic
            key_sel = json.loads(
                daemon.mqtt.client.by_topic()[
                    f"{base}/select/t_dev_ir_edit_key/config"])
            self.assertIn("F13 (KEY_F13)", key_sel["options"])

    def test_timeout(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_edit_identify()
            daemon._ir_identify["deadline"] = 0.0
            daemon._ir_identify_tick()
            self.assertEqual(self.status(daemon), "timeout")
            self.assertIsNone(daemon._ir_identify)
            self.assertIsNone(daemon.ir_map.get("KEY_F13"))

    def test_status_returns_to_idle_after_capture(self):
        async def scenario(daemon):
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_F13", False)
            await asyncio.sleep(hubd.IR_IDENTIFY_IDLE_RETURN_S + 0.15)
            self.assertEqual(self.status(daemon), "idle")

        original = hubd.IR_IDENTIFY_IDLE_RETURN_S
        hubd.IR_IDENTIFY_IDLE_RETURN_S = 0.05
        try:
            with HubDaemonFixture() as daemon:
                asyncio.run(scenario(daemon))
        finally:
            hubd.IR_IDENTIFY_IDLE_RETURN_S = original

    def test_rearm_blocks_stale_idle_return(self):
        async def scenario(daemon):
            daemon._on_ir_edit_identify()
            daemon._on_ir_key("KEY_F13", False)   # terminal: captured
            daemon._on_ir_edit_identify()         # re-arm before idle return
            armed = self.status(daemon)
            await asyncio.sleep(hubd.IR_IDENTIFY_IDLE_RETURN_S + 0.15)
            self.assertEqual(self.status(daemon), armed)

        original = hubd.IR_IDENTIFY_IDLE_RETURN_S
        hubd.IR_IDENTIFY_IDLE_RETURN_S = 0.05
        try:
            with HubDaemonFixture() as daemon:
                asyncio.run(scenario(daemon))
        finally:
            hubd.IR_IDENTIFY_IDLE_RETURN_S = original


class TestIREventEntity(unittest.TestCase):
    def event_count(self, daemon):
        return len([t for t, _ in daemon.mqtt.client.published
                    if t == "t_dev/ir/event"])

    def test_repeats_never_fire_events(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key("KEY_VOLUMEUP", True)
            self.assertEqual(self.event_count(daemon), 0)

    def test_new_key_gets_discovery_before_event(self):
        with HubDaemonFixture() as daemon:
            daemon._on_ir_key("KEY_F13", False)
            published = daemon.mqtt.client.published
            disc_idx = min(i for i, (t, _) in enumerate(published)
                           if t.endswith("/select/t_dev_ir_edit_key/config"))
            event_idx = min(i for i, (t, _) in enumerate(published)
                            if t == "t_dev/ir/event")
            self.assertLess(disc_idx, event_idx)
            payload = json.loads(published[event_idx][1])
            self.assertEqual(payload["event_type"], "KEY_F13")

    def test_known_key_press_does_not_republish_discovery(self):
        with HubDaemonFixture() as daemon:
            # seed the option cache with a full discovery pass
            daemon.mqtt._publish_discovery(daemon.mqtt.client)
            before = len(daemon.mqtt.client.published)
            daemon._on_ir_key("KEY_VOLUMEUP", False)
            new = [t for t, _ in daemon.mqtt.client.published[before:]
                   if t.endswith("/ir_edit_key/config")]
            self.assertEqual(new, [])
            self.assertEqual(self.event_count(daemon), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
