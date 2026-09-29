#!/usr/bin/env python3
"""Integration tests for hubd's device-selection reconciler.

Simulates the Pulse graph (stubbed AudioControl + recorded MQTT publishes)
and drives HubDaemon._reconcile_selection_tick through the spec's scenarios:
fresh-boot idempotence, output selection + move, absent-device hold, UR23
profile flip, duplicate-prefix options, and master-target priority.

    .venv/bin/python tests/test_selection_reconcile.py
"""

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "hubd"))
import main as hubd  # noqa: E402

BUILT_IN = "alsa_output.platform-fe00b840.mailbox.stereo-fallback"
BUILT_IN_IDX = 1
UR23_STEREO = "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.analog-stereo"
UR23_FALLBACK = "alsa_input.usb-HiFimeDIY_Audio_UR23_USB_SPDIF_Rx-01.stereo-fallback"
UR23_IDX = 5
UR23_FALLBACK_IDX = 7
DAC = "alsa_output.usb-GoDAC-00.analog-stereo"
DAC_IDX = 2


def make_config():
    return hubd.HubConfig(
        hostname="t", device_id="t_dev", device_name="T",
        mqtt_host="x", mqtt_port=1883, mqtt_username="u", mqtt_password="p",
        lineout_sink_pattern="Built-in Audio Stereo",
        tv_source_pattern="UR23")


class FakeAudio:
    """Stub for the AudioControl surface the reconciler touches."""

    def __init__(self, snap, defaults):
        self._snap = snap
        self._defaults = defaults
        self.moved_ducts = []
        self.moved_captures = []

    def routing_snapshot(self):
        return self._snap

    def factory_defaults(self):
        return self._defaults

    def move_ducts_to_sink(self, sink_index):
        self.moved_ducts.append(sink_index)
        self._snap["ducts"] = {n: sink_index for n in self._snap["ducts"]}
        return len(self._snap["ducts"])

    def move_tv_capture_to_source(self, source_index, attached_to):
        self.moved_captures.append(source_index)
        for n, i in self._snap["tv_captures"].items():
            if i in attached_to:
                self._snap["tv_captures"][n] = source_index
        return 1

    _run = None  # not used through this stub


def fresh_graph(with_dac=False, ur23=UR23_STEREO, ur23_idx=UR23_IDX):
    sinks = {BUILT_IN: BUILT_IN_IDX}
    if with_dac:
        sinks[DAC] = DAC_IDX
    return {
        "sinks": sinks,
        "sources": {ur23: ur23_idx},
        "ducts": {n: BUILT_IN_IDX for n in hubd.DUCT_PLAYBACK_NODES},
        "tv_captures": {"loopback.tv.capture": ur23_idx},
    }


class ReconcileTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        sel_path = os.path.join(self.tmp.name, "selection.json")
        original_store = hubd.SelectionStore
        patcher = mock.patch.object(hubd, "SelectionStore",
                                    lambda path=None: original_store(sel_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        cfg = make_config()
        self.daemon = hubd.HubDaemon(cfg)
        # record MQTT publishes instead of hitting paho; keep the bridge's
        # stored options in sync the way the real publish would
        self.published = []
        self.daemon.mqtt._publish = (
            lambda topic, payload: self.published.append((topic, payload)))

        def record_discovery(options):
            self.daemon.mqtt._selection_options = {
                "output": sorted(options.get("output", [])),
                "tv_source": sorted(options.get("tv_source", [])),
            }
            self.published.append(("discovery", json.dumps(options)))
        self.daemon.mqtt.publish_selection_discovery = record_discovery
        self.set_audio(fresh_graph())

    def set_audio(self, snap, defaults=None):
        self.audio = FakeAudio(snap, defaults or {
            "output": hubd.node_prefix(BUILT_IN),
            "tv_source": hubd.node_prefix(UR23_STEREO),
        })
        self.daemon.audio = self.audio

    def published_topics(self, suffix):
        return [p for t, p in self.published if t.endswith(suffix)]

    def published_options(self):
        for t, p in self.published:
            if t == "discovery":
                return json.loads(p)
        return None


class TestFreshBootIdempotence(ReconcileTestBase):
    def test_no_selection_no_moves(self):
        self.daemon._reconcile_selection_tick()
        self.assertEqual(self.audio.moved_ducts, [])
        self.assertEqual(self.audio.moved_captures, [])
        self.assertEqual(self.daemon._sel_effective["output"], hubd.node_prefix(BUILT_IN))
        self.assertEqual(self.daemon._sel_effective["tv_source"], hubd.node_prefix(UR23_STEREO))

    def test_second_tick_is_stable(self):
        self.daemon._reconcile_selection_tick()
        self.published.clear()
        self.daemon._reconcile_selection_tick()
        self.assertEqual(self.audio.moved_ducts, [])
        self.assertEqual(self.published, [])


class TestOutputSelection(ReconcileTestBase):
    def test_select_dac_moves_all_ducts(self):
        self.daemon._reconcile_selection_tick()  # initial: options published
        self.set_audio(fresh_graph(with_dac=True))
        self.daemon._reconcile_selection_tick()  # options republished (<=2 s)
        self.daemon._on_selection_command("output", hubd.node_prefix(DAC))
        self.assertEqual(self.audio.moved_ducts, [DAC_IDX])
        self.assertEqual(self.daemon._sel_effective["output"], hubd.node_prefix(DAC))
        self.assertEqual(self.daemon.selection.get("output"), hubd.node_prefix(DAC))
        self.assertIn(hubd.node_prefix(DAC),
                      self.published_topics("/output/state"))

    def test_unknown_option_rejected(self):
        self.daemon._reconcile_selection_tick()
        self.daemon._on_selection_command("output", "alsa_output.not-a-device")
        self.assertIsNone(self.daemon.selection.get("output"))
        self.assertEqual(self.audio.moved_ducts, [])

    def test_selection_persisted_across_instances(self):
        self.daemon._reconcile_selection_tick()
        self.set_audio(fresh_graph(with_dac=True))
        self.daemon._reconcile_selection_tick()  # options republished (<=2 s)
        self.daemon._on_selection_command("output", hubd.node_prefix(DAC))
        # a fresh daemon (boot) reads the same store and re-resolves
        cfg = make_config()
        daemon2 = hubd.HubDaemon(cfg)
        daemon2.mqtt._publish = lambda t, p: None
        daemon2.mqtt.publish_selection_discovery = lambda o: None
        audio2 = FakeAudio(fresh_graph(with_dac=True),
                           {"output": hubd.node_prefix(BUILT_IN),
                            "tv_source": hubd.node_prefix(UR23_STEREO)})
        daemon2.audio = audio2
        daemon2._reconcile_selection_tick()
        self.assertEqual(audio2.moved_ducts, [DAC_IDX])
        self.assertEqual(daemon2._sel_effective["output"], hubd.node_prefix(DAC))


class TestAbsentDevice(ReconcileTestBase):
    def test_holds_effective_state_when_selected_output_absent(self):
        self.set_audio(fresh_graph(with_dac=True))
        self.daemon._reconcile_selection_tick()  # options republished (<=2 s)
        self.daemon._on_selection_command("output", hubd.node_prefix(DAC))
        self.assertEqual(self.daemon._sel_effective["output"], hubd.node_prefix(DAC))
        dac_audio = self.audio  # the stub that saw the move
        # DAC unplugged: graph no longer has it; state must hold, not regress
        self.set_audio(fresh_graph())
        self.daemon._reconcile_selection_tick()
        self.assertEqual(self.daemon._sel_effective["output"], hubd.node_prefix(DAC))
        # ducts untouched (they follow PipeWire's default handling)
        self.assertEqual(dac_audio.moved_ducts, [DAC_IDX])
        self.assertEqual(self.audio.moved_ducts, [])


class TestTvSourceFlip(ReconcileTestBase):
    def test_flip_is_absorbed_without_moves(self):
        # Realistic post-flip graph: the capture waiting on the dead
        # analog-stereo candidate is unattached; the dual-candidate loopback
        # (tv2) has attached to the new stereo-fallback node on its own. The
        # reconciler confirms the routing and must move nothing.
        self.daemon._reconcile_selection_tick()
        snap = fresh_graph(ur23=UR23_FALLBACK, ur23_idx=UR23_FALLBACK_IDX)
        snap["tv_captures"] = {
            "loopback.tv.capture": -1,          # waiting (target gone)
            "loopback.tv2.capture": UR23_FALLBACK_IDX,  # linked by design
        }
        self.set_audio(snap, defaults={"output": hubd.node_prefix(BUILT_IN),
                                       "tv_source": hubd.node_prefix(UR23_STEREO)})
        self.daemon._reconcile_selection_tick()
        self.assertEqual(self.audio.moved_captures, [])
        self.assertEqual(self.daemon._sel_effective["tv_source"],
                         hubd.node_prefix(UR23_STEREO))

    def test_selecting_other_capture_moves_attached_one(self):
        OTHER = "alsa_input.usb-HDMI-Capture-00.analog-stereo"
        OTHER_IDX = 9
        self.daemon._reconcile_selection_tick()
        snap = fresh_graph()
        snap["sources"][OTHER] = OTHER_IDX
        self.set_audio(snap)
        self.daemon._reconcile_selection_tick()  # options republished (<=2 s)
        self.daemon._on_selection_command("tv_source", hubd.node_prefix(OTHER))
        self.assertEqual(self.audio.moved_captures, [OTHER_IDX])
        self.assertEqual(self.daemon._sel_effective["tv_source"], hubd.node_prefix(OTHER))

    def test_waiting_capture_is_not_moved(self):
        # the second (dual-candidate) loopback waits unattached; it must be
        # left alone so its declared target still handles the next flip
        snap = fresh_graph()
        snap["tv_captures"]["loopback.tv2.capture"] = -1  # unattached
        self.set_audio(snap)
        self.daemon._reconcile_selection_tick()
        self.assertEqual(self.audio.moved_captures, [])


class TestOptionsRepublish(ReconcileTestBase):
    def test_options_published_once_and_on_change(self):
        self.daemon._reconcile_selection_tick()
        first = self.published_options()
        self.assertIn(hubd.node_prefix(BUILT_IN), first["output"])
        self.published.clear()
        self.daemon._reconcile_selection_tick()
        self.assertIsNone(self.published_options())  # no republish when unchanged
        self.set_audio(fresh_graph(with_dac=True))
        self.daemon._reconcile_selection_tick()
        self.assertIn(hubd.node_prefix(DAC), self.published_options()["output"])

    def test_bt_and_buses_never_listed(self):
        snap = fresh_graph()
        snap["sinks"]["bus.tv"] = 90
        snap["sinks"]["bluez_sink.11_22_33.a2dp-sink"] = 91
        snap["sinks"]["auto_null.something"] = 92
        snap["sources"]["bus.tv.monitor"] = 93
        snap["sources"]["bluez_source.44_55"] = 94
        self.set_audio(snap)
        self.daemon._reconcile_selection_tick()
        opts = self.published_options()
        self.assertEqual(opts["output"], [hubd.node_prefix(BUILT_IN)])
        self.assertEqual(opts["tv_source"], [hubd.node_prefix(UR23_STEREO)])


class TestFindLineoutPriority(unittest.TestCase):
    class FakeSink:
        def __init__(self, name, description, index):
            self.name, self.description, self.index = name, description, index

    def _pulse(self, sinks):
        p = mock.Mock()
        p.sink_list.return_value = sinks
        return p

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sel = hubd.SelectionStore(os.path.join(self.tmp.name, "selection.json"))
        self.builtin = self.FakeSink(BUILT_IN, "Built-in Audio Stereo", BUILT_IN_IDX)
        self.dac = self.FakeSink(DAC, "GoDAC", DAC_IDX)

    def test_selection_wins_over_pattern(self):
        self.sel.set("output", hubd.node_prefix(DAC))
        ac = hubd.AudioControl(make_config(), self.sel)
        found = ac._find_lineout(self._pulse([self.builtin, self.dac]))
        self.assertEqual(found.index, DAC_IDX)

    def test_selected_absent_falls_back_to_pattern(self):
        self.sel.set("output", hubd.node_prefix(DAC))
        ac = hubd.AudioControl(make_config(), self.sel)
        found = ac._find_lineout(self._pulse([self.builtin]))
        self.assertEqual(found.index, BUILT_IN_IDX)

    def test_no_selection_uses_pattern(self):
        ac = hubd.AudioControl(make_config(), self.sel)
        found = ac._find_lineout(self._pulse([self.dac, self.builtin]))
        self.assertEqual(found.index, BUILT_IN_IDX)


if __name__ == "__main__":
    unittest.main(verbosity=2)
