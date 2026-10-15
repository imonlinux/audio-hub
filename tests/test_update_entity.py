#!/usr/bin/env python3
"""Unit tests for the HA update entity and the request/status token
handshake (spec Section 4.7).

Covers: discovery payload, initial state, the install request file, the
token handshake against stale daily-check status files, failure statuses,
malformed status files, and publish dedup on the 2 s tick.

Run from the repo root:

    .venv/bin/python tests/test_update_entity.py

No audio stack, broker, or root required; MQTT publishes are recorded via a
stub client and the /run/audiohub paths are redirected into a temp dir.
"""

import json
import os
import re
import sys
import tempfile
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

    def set_enabled(self, enabled):
        self.ducking_enabled = enabled


def make_config(**overrides):
    kwargs = dict(
        hostname="t", device_id="t_dev", device_name="T",
        mqtt_host="x", mqtt_port=1883, mqtt_username="u", mqtt_password="p")
    kwargs.update(overrides)
    return hubd.HubConfig(**kwargs)


class ImmediateLoop:
    """Runs call_soon_threadsafe callbacks synchronously (tests the full
    paho _on_message -> loop -> handler path without a real loop)."""

    def call_soon_threadsafe(self, fn, *args):
        fn(*args)


def mqtt_msg(topic: str, payload: str):
    return type("M", (), {"topic": topic, "payload": payload.encode()})()


class UpdateFixture:
    """HubDaemon with the update handshake redirected into a temp dir."""

    def __enter__(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HOME")
        os.environ["HOME"] = self._tmp.name
        run_dir = os.path.join(self._tmp.name, "run")
        os.makedirs(run_dir, exist_ok=True)
        self.request_path = os.path.join(run_dir, "update-request")
        self.status_path = os.path.join(run_dir, "update-status.json")
        self._patches = [
            mock.patch.object(hubd, "UPDATE_REQUEST_PATH", self.request_path),
            mock.patch.object(hubd, "UPDATE_STATUS_PATH", self.status_path),
        ]
        for p in self._patches:
            p.start()
        self.daemon = hubd.HubDaemon(make_config())
        self.daemon.audio = FakeAudio()
        self.daemon.ducking = FakeDucking()
        self.daemon.mqtt._loop = ImmediateLoop()
        self.client = RecordingClient()
        self.daemon.mqtt.client = self.client
        return self.daemon

    def __exit__(self, *exc):
        for p in self._patches:
            p.stop()
        if self._old_home is None:
            del os.environ["HOME"]
        else:
            os.environ["HOME"] = self._old_home
        self._tmp.cleanup()

    # -- helpers -------------------------------------------------------------
    def write_status(self, data: dict):
        with open(self.status_path, "w") as f:
            json.dump(data, f)

    def request_token(self):
        with open(self.request_path) as f:
            return f.read().strip()

    def update_payloads(self):
        return [json.loads(p) for t, p in self.client.published
                if t == "t_dev/update/state"]

    def latest_payload(self):
        return self.update_payloads()[-1]

    @staticmethod
    def update_payloads_count(daemon):
        return sum(1 for t, p in daemon.mqtt.client.published
                   if t == "t_dev/update/state")


def send_install(daemon):
    daemon.mqtt._on_message(None, None,
                            mqtt_msg("t_dev/update/install", "install"))


class TestUpdateDiscovery(unittest.TestCase):
    def test_firmware_entity_discovery_payload(self):
        with UpdateFixture() as daemon:
            daemon.mqtt._publish_discovery(daemon.mqtt.client)
            topic = "homeassistant/update/t_dev_firmware/config"
            payload = json.loads(daemon.mqtt.client.by_topic()[topic])
            self.assertEqual(payload["name"], "Firmware")
            self.assertEqual(payload["unique_id"], "t_dev_firmware")
            self.assertEqual(payload["entity_category"], "diagnostic")
            self.assertEqual(payload["state_topic"], "t_dev/update/state")
            self.assertEqual(payload["command_topic"], "t_dev/update/install")
            self.assertEqual(payload["payload_install"], "install")

    def test_initial_state_payload_shape(self):
        # No status file: installed == latest == the local version stamp, so
        # HA reads "up to date" until the daily check writes availability.
        with UpdateFixture() as daemon:
            daemon._update_snapshot()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertEqual(data["installed_version"],
                             hubd.audiohub_version())
            self.assertEqual(data["latest_version"], hubd.audiohub_version())
            self.assertFalse(data["in_progress"])
            self.assertEqual(data["title"], "audio-hub")


class TestInstallRequest(unittest.TestCase):
    def test_request_file_written_via_routing(self):
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            token = fx.request_token()
            self.assertRegex(token, r"^[0-9a-f]{32}$")
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertTrue(data["in_progress"])
            # the version fields stay truthful while pending
            self.assertEqual(data["installed_version"],
                             hubd.audiohub_version())

    def test_second_request_while_pending_ignored(self):
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            first = fx.request_token()
            send_install(daemon)
            self.assertEqual(fx.request_token(), first)

    def test_unrecognized_payload_ignored(self):
        fx = UpdateFixture()
        with fx as daemon:
            daemon.mqtt._on_message(None, None,
                                    mqtt_msg("t_dev/update/install", "please"))
            self.assertFalse(os.path.exists(fx.request_path))


class TestTokenHandshake(unittest.TestCase):
    def test_matching_token_completes_and_adopts_versions(self):
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            token = fx.request_token()
            fx.write_status({"installed_version": "v2.0.0",
                             "latest_version": "v2.0.0",
                             "in_progress": False,
                             "token": token, "error": None})
            daemon._update_status_tick()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertFalse(data["in_progress"])
            self.assertEqual(data["installed_version"], "v2.0.0")
            self.assertEqual(data["latest_version"], "v2.0.0")
            self.assertEqual(data["release_url"],
                             hubd.RELEASE_URL_BASE + "v2.0.0")

    def test_foreign_token_keeps_in_progress(self):
        # The status file of a DIFFERENT request must not complete ours.
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            fx.write_status({"installed_version": "v2.0.0",
                             "latest_version": "v2.0.0",
                             "in_progress": False,
                             "token": "deadbeef" * 4, "error": None})
            daemon._update_status_tick()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertTrue(data["in_progress"])
            self.assertEqual(data["installed_version"],
                             hubd.audiohub_version())

    def test_stale_daily_check_does_not_flap_pending_entity(self):
        # Section 4.9: while an install is running, yesterday's daily-check
        # status (no token, in_progress false) must NOT flap the entity back
        # to "up to date" mid-install.
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            fx.write_status({"installed_version": hubd.audiohub_version(),
                             "latest_version": hubd.audiohub_version(),
                             "in_progress": False,
                             "token": None, "error": None})
            daemon._update_status_tick()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertTrue(data["in_progress"])

    def test_no_pending_adopts_daily_check_result(self):
        # Fresh boot (or post-install reconnect): the daily check's status
        # is authoritative the moment nothing is pending.
        fx = UpdateFixture()
        with fx as daemon:
            fx.write_status({"installed_version": "v1.2.2",
                             "latest_version": "v1.3.0",
                             "in_progress": False,
                             "token": None, "error": None})
            daemon._update_snapshot()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertFalse(data["in_progress"])
            self.assertEqual(data["installed_version"], "v1.2.2")
            self.assertEqual(data["latest_version"], "v1.3.0")

    def test_error_status_completes_with_old_version(self):
        # Section 4.9: failed install echoes the token; the entity exits
        # in_progress showing the old installed version.
        fx = UpdateFixture()
        with fx as daemon:
            send_install(daemon)
            token = fx.request_token()
            fx.write_status({"installed_version": "v1.2.2",
                             "latest_version": "v2.0.0",
                             "in_progress": False,
                             "token": token,
                             "error": "install failed (rolled back)"})
            daemon._update_status_tick()
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertFalse(data["in_progress"])
            self.assertEqual(data["installed_version"], "v1.2.2")

    def test_malformed_status_file_ignored(self):
        fx = UpdateFixture()
        with fx as daemon:
            with open(fx.status_path, "w") as f:
                f.write("not json at all")
            daemon._update_status_tick()  # must not raise
            data = json.loads(
                daemon.mqtt.client.by_topic()["t_dev/update/state"])
            self.assertFalse(data["in_progress"])

    def test_missing_status_file_is_silent(self):
        fx = UpdateFixture()
        with fx as daemon:
            daemon._update_status_tick()  # must not raise
            self.assertFalse(os.path.exists(fx.request_path))


class TestPublishDedup(unittest.TestCase):
    def test_unchanged_status_not_republished(self):
        fx = UpdateFixture()
        with fx as daemon:
            daemon._update_snapshot()
            n = fx.update_payloads_count(daemon)
            fx.write_status({"installed_version": "v1.3.0",
                             "latest_version": "v1.3.0",
                             "in_progress": False,
                             "token": None, "error": None})
            daemon._update_status_tick()  # adoption -> one new payload
            after_change = fx.update_payloads_count(daemon)
            daemon._update_status_tick()
            daemon._update_status_tick()
            self.assertEqual(fx.update_payloads_count(daemon), after_change)
            self.assertEqual(after_change, n + 1)


if __name__ == "__main__":
    unittest.main()
