#!/usr/bin/env python3
"""Unit tests for hubd's device-selection logic (docs/device-selection-spec.md).

Pure-logic coverage: prefix key, option filtering, option building with the
duplicate-prefix fallback, resolution, the SelectionStore round-trip, and the
select discovery payloads. Run from the repo root:

    .venv/bin/python tests/test_device_selection.py

(The repo .venv needs pulsectl/paho-mqtt/evdev — the same system packages the
Pi installs.) No audio stack or broker required.
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hubd"))
import main as hubd  # noqa: E402


class TestNodePrefix(unittest.TestCase):
    def test_strips_profile_component(self):
        self.assertEqual(
            hubd.node_prefix("alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo"),
            "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01")
        self.assertEqual(
            hubd.node_prefix("alsa_output.platform-fe00b840.mailbox.stereo-fallback"),
            "alsa_output.platform-fe00b840.mailbox")

    def test_profile_flip_yields_same_prefix(self):
        a = hubd.node_prefix("alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo")
        b = hubd.node_prefix("alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.stereo-fallback")
        self.assertEqual(a, b)

    def test_plain_name_unchanged(self):
        self.assertEqual(hubd.node_prefix("alsa_output.usb-DAC-00"), "alsa_output.usb-DAC-00")


class TestFiltering(unittest.TestCase):
    def test_outputs_exclude_buses_placeholders_bt(self):
        got = hubd.hardware_output_names([
            "bus.tv", "bus.bt", "bus.music",
            "auto_null.usb-DAC.analog-stereo",
            "bluez_sink.11_22_33_44_55_66.a2dp-sink",
            "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
            "alsa_output.usb-DAC-00.analog-stereo",
        ])
        self.assertEqual(got, [
            "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
            "alsa_output.usb-DAC-00.analog-stereo",
        ])

    def test_captures_exclude_monitors_and_bt(self):
        got = hubd.hardware_capture_names([
            "bus.tv.monitor",
            "alsa_output.platform-fe00b840.mailbox.stereo-fallback.monitor",
            "bluez_source.11_22_33_44_55_66",
            "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo",
        ])
        self.assertEqual(got,
                         ["alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo"])


class TestOptions(unittest.TestCase):
    def test_prefixes_by_default(self):
        got = hubd.build_options([
            "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo",
            "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
        ])
        self.assertEqual(got, [
            "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01",
            "alsa_output.platform-fe00b840.mailbox",
        ])

    def test_duplicate_prefix_falls_back_to_full_names(self):
        # Two live nodes sharing one stable prefix (same card exposing two
        # nodes that differ only in the final component) must not merge.
        got = hubd.build_options([
            "alsa_output.x.profile-a",
            "alsa_output.x.profile-b",
            "alsa_output.usb-OTHER-00.analog-stereo",
        ])
        self.assertEqual(got, [
            "alsa_output.usb-OTHER-00",
            "alsa_output.x.profile-a",
            "alsa_output.x.profile-b",
        ])

    def test_identical_dongles_keep_distinct_prefixes(self):
        # USB ordinals make two identical dongles distinct devices — the
        # common case, and no fallback is needed.
        got = hubd.build_options([
            "alsa_output.usb-DAC-00.analog-stereo",
            "alsa_output.usb-DAC-01.analog-stereo",
        ])
        self.assertEqual(got, ["alsa_output.usb-DAC-00", "alsa_output.usb-DAC-01"])

    def test_flip_between_profiles_keeps_one_option(self):
        live_a = ["alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo"]
        live_b = ["alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.stereo-fallback"]
        self.assertEqual(hubd.build_options(live_a), hubd.build_options(live_b))


class TestResolve(unittest.TestCase):
    NAMES = [
        "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo",
        "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.stereo-fallback",
        "alsa_output.platform-fe00b840.mailbox.stereo-fallback",
    ]

    def test_prefix_resolves_to_longest_live_node(self):
        self.assertEqual(
            hubd.resolve_option("alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01", self.NAMES),
            "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.stereo-fallback")

    def test_full_name_resolves_exactly(self):
        self.assertEqual(
            hubd.resolve_option("alsa_output.platform-fe00b840.mailbox.stereo-fallback", self.NAMES),
            "alsa_output.platform-fe00b840.mailbox.stereo-fallback")

    def test_absent_option_resolves_to_none(self):
        self.assertIsNone(hubd.resolve_option("alsa_output.usb-GONE-00", self.NAMES))

    def test_prefix_must_not_match_unrelated_suffixes(self):
        # 'alsa_input.usb-HiFimeDIY' must not match a node with that string
        # elsewhere in the name; the boundary is the dot.
        self.assertIsNone(hubd.resolve_option("alsa_input.usb-HiFimeDIY", self.NAMES))


class TestSelectionStore(unittest.TestCase):
    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sel", "selection.json")
            store = hubd.SelectionStore(path)
            self.assertIsNone(store.get("output"))
            self.assertIsNone(store.get("tv_source"))
            store.set("output", "alsa_output.usb-DAC-00")
            store.set("tv_source", "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01")
            # a second instance reads what the first persisted (boot recovery)
            again = hubd.SelectionStore(path)
            self.assertEqual(again.get("output"), "alsa_output.usb-DAC-00")
            self.assertEqual(again.get("tv_source"),
                             "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01")
            with open(path) as f:
                self.assertEqual(json.load(f)["output"], "alsa_output.usb-DAC-00")

    def test_corrupt_store_treated_as_absent(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "selection.json")
            with open(path, "w") as f:
                f.write("{not json")
            store = hubd.SelectionStore(path)
            self.assertIsNone(store.get("output"))

    def test_ignores_non_string_entries(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "selection.json")
            with open(path, "w") as f:
                json.dump({"output": 42, "tv_source": "alsa_input.usb-X"}, f)
            store = hubd.SelectionStore(path)
            self.assertIsNone(store.get("output"))
            self.assertEqual(store.get("tv_source"), "alsa_input.usb-X")


class TestSelectDiscovery(unittest.TestCase):
    def _bridge(self):
        cfg = hubd.HubConfig(
            hostname="t", device_id="t_dev", device_name="T",
            mqtt_host="x", mqtt_port=1883, mqtt_username="u", mqtt_password="p")
        return hubd.MQTTBridge(cfg)

    def test_payload_shape_and_topic_names(self):
        bridge = self._bridge()
        base = bridge.cfg.mqtt_base_topic
        availability = bridge._availability_topic()
        device = {"identifiers": ["t_dev"], "name": "T", "model": "m", "manufacturer": "c"}
        entities = bridge._selection_discovery(base, "t_dev", availability, device)
        by_topic = dict(entities)
        self.assertIn(f"{base}/select/t_dev_output/config", by_topic)
        self.assertIn(f"{base}/select/t_dev_tv_source/config", by_topic)
        out = by_topic[f"{base}/select/t_dev_output/config"]
        self.assertEqual(out["unique_id"], "t_dev_output")
        self.assertEqual(out["state_topic"], "t_dev/output/state")
        self.assertEqual(out["command_topic"], "t_dev/output/set")
        self.assertEqual(out["options"], [])
        tv = by_topic[f"{base}/select/t_dev_tv_source/config"]
        self.assertEqual(tv["unique_id"], "t_dev_tv_source")
        self.assertEqual(tv["command_topic"], "t_dev/tv_source/set")

    def test_options_update_and_republish(self):
        bridge = self._bridge()
        bridge.publish_selection_discovery(
            {"output": ["alsa_output.a"], "tv_source": ["alsa_input.b"]})
        self.assertEqual(bridge._selection_options["output"], ["alsa_output.a"])
        device = {"identifiers": ["t_dev"], "name": "T", "model": "m", "manufacturer": "c"}
        by_topic = dict(bridge._selection_discovery(
            bridge.cfg.mqtt_base_topic, "t_dev", bridge._availability_topic(), device))
        self.assertEqual(by_topic[bridge.cfg.mqtt_base_topic + "/select/t_dev_output/config"]["options"],
                         ["alsa_output.a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
