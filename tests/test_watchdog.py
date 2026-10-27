"""Offline watchdog regression tests. Network and billable actions are mocked."""
import importlib.util
import io
import json
import math
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

PATH = Path(__file__).resolve().parents[1] / "bin/bonsai-watchdog.py"
spec = importlib.util.spec_from_file_location("bonsai_watchdog", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class Response(io.BytesIO):
    def __init__(self, content=b"", status=200):
        super().__init__(content)
        self.status = status


class SlotTests(unittest.TestCase):
    def test_busy(self):
        with patch.object(mod, "open_request", return_value=Response(b'[{"is_processing": false}, {"is_processing": true}]')):
            self.assertTrue(mod.get_slot_state("http://localhost/slots"))

    def test_idle(self):
        with patch.object(mod, "open_request", return_value=Response(b'[{"is_processing": false}]')):
            self.assertFalse(mod.get_slot_state("http://localhost/slots"))

    def test_invalid(self):
        for payload in ([], {}, [{"other": True}], [{"is_processing": 1}], [{"is_processing": "false"}]):
            with patch.object(mod, "open_request", return_value=Response(json.dumps(payload).encode())):
                with self.assertRaises(ValueError):
                    mod.get_slot_state("http://localhost/slots")

    def test_oversized(self):
        with patch.object(mod, "open_request", return_value=Response(b"x" * (mod.BODY_LIMIT + 1))):
            with self.assertRaises(ValueError):
                mod.get_slot_state("http://localhost/slots")


class StateTests(unittest.TestCase):
    def test_idle_timeout(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        self.assertEqual(w.observe(0, processing=False), ("idle", None))
        self.assertEqual(w.observe(60, processing=False), ("idle", "idle-timeout"))

    def test_idle_begins_on_first_confirmed_idle_not_last_busy_poll(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(55, processing=True)
        self.assertEqual(w.observe(100, processing=False), ("idle", None))
        self.assertEqual(w.observe(116, processing=False), ("idle", None))
        self.assertEqual(w.observe(160, processing=False), ("idle", "idle-timeout"))

    def test_transient_outage(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(40, error=True)
        self.assertEqual(w.observe(100, error=True), ("unhealthy", None))
        self.assertEqual(w.observe(110, processing=False), ("idle", None))
        self.assertEqual(w.observe(169, processing=False), ("idle", None))
        self.assertEqual(w.observe(170, processing=False), ("idle", "idle-timeout"))

    def test_prolonged_outage(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(10, error=True)
        self.assertEqual(w.observe(129, error=True), ("unhealthy", None))
        self.assertEqual(w.observe(130, error=True), ("unhealthy", "prolonged-unhealthy-server"))

    def test_hard_ceiling_even_while_busy(self):
        self.assertEqual(mod.Watchdog(60, 120, 3600, 0).observe(3600, processing=True),
                         ("processing", "maximum-session-duration"))

    def test_rejects_nonfinite_or_nonpositive_timeout(self):
        for value in (math.nan, math.inf, -1, 0):
            with self.assertRaises(ValueError):
                mod.Watchdog(value, 120, 3600, 0)

    def test_unknown_not_implicitly_idle(self):
        with self.assertRaises(ValueError):
            mod.Watchdog(60, 120, 3600, 0).observe(0)


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "state/session.json"

    def tearDown(self):
        self.temp.cleanup()

    def test_restarts_reuse_origin_and_account_for_downtime(self):
        record = mod.SessionRecord(self.path, "pod", "run1", 1000)
        record.checkpoint(3500, 4500)
        record.close()
        record = mod.SessionRecord(self.path, "pod", "run1", 4700)
        try:
            self.assertEqual(record.elapsed, 3700)
            w = mod.Watchdog(60, 120, 3600, 10 - record.elapsed)
            self.assertEqual(w.observe(10, processing=True)[1], "maximum-session-duration")
        finally:
            record.close()

    def test_explicit_origin_includes_earlier_runtime(self):
        record = mod.SessionRecord(self.path, "pod", "run1", 1000, start_epoch=900)
        self.assertEqual(record.elapsed, 100)
        record.close()

    def test_new_session_requires_explicit_authorization(self):
        mod.SessionRecord(self.path, "pod", "run1", 1000).close()
        with self.assertRaises(ValueError):
            mod.SessionRecord(self.path, "pod", "run2", 1200)
        record = mod.SessionRecord(self.path, "pod", "run2", 1200, new_session=True)
        self.assertEqual(record.elapsed, 0)
        record.close()

    def test_same_session_cannot_be_reset(self):
        mod.SessionRecord(self.path, "pod", "run1", 1000).close()
        with self.assertRaises(ValueError):
            mod.SessionRecord(self.path, "pod", "run1", 1200, new_session=True)

    def test_corrupt_record_not_silently_reset(self):
        self.path.parent.mkdir()
        self.path.write_text("invalid json")
        self.path.chmod(0o600)
        with self.assertRaises(ValueError):
            mod.SessionRecord(self.path, "pod", "run1", 1000)
        self.assertEqual(self.path.read_text(), "invalid json")

    def test_second_watchdog_cannot_take_lock(self):
        record = mod.SessionRecord(self.path, "pod", "run1", 1000)
        try:
            with self.assertRaises(BlockingIOError):
                mod.SessionRecord(self.path, "pod", "run1", 1001)
        finally:
            record.close()

    def test_clock_rollback_is_not_fresh_budget(self):
        mod.SessionRecord(self.path, "pod", "run1", 1000).close()
        with self.assertRaises(ValueError):
            mod.SessionRecord(self.path, "pod", "run1", 900)

    def test_state_private(self):
        mod.SessionRecord(self.path, "pod", "run1", 1000).close()
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_symlink_state_rejected(self):
        target = Path(self.temp.name) / "target"
        target.write_text("untouched")
        self.path.parent.mkdir()
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            mod.SessionRecord(self.path, "pod", "run1", 1000)
        self.assertEqual(target.read_text(), "untouched")


class APITests(unittest.TestCase):
    def setUp(self):
        self.api = mod.RunPodAPI("testpod", "FAKE_KEY")

    def test_exact_rest_stop_and_empty_response(self):
        with patch.object(mod, "open_request", return_value=Response(b"", 204)) as mocked:
            self.api.stop()
        req, timeout = mocked.call_args.args
        self.assertEqual(req.full_url, "https://api.runpod.io/v2/pods/testpod/action")
        self.assertEqual(req.method, "POST")
        self.assertEqual(json.loads(req.data), {"action": "stop"})
        self.assertEqual(req.get_header("Authorization"), "Bearer FAKE_KEY")
        self.assertEqual(timeout, 15)

    def test_status_checks_id(self):
        with patch.object(mod, "open_request", return_value=Response(b'{"id":"other","status":"EXITED"}')):
            with self.assertRaises(mod.APIError):
                self.api.status()

    def test_http_errors_are_safe(self):
        for code in (401, 403, 429, 500, 503):
            error = urllib.error.HTTPError("https://ignored", code, "FAKE_KEY response", {}, None)
            with patch.object(mod, "open_request", side_effect=error):
                with self.assertRaises(mod.APIError) as caught:
                    self.api.stop()
                self.assertNotIn("FAKE_KEY", str(caught.exception))
                self.assertIn(str(code), str(caught.exception))

    def test_malformed_json_is_safe_error(self):
        with patch.object(mod, "open_request", return_value=Response(b"not json FAKE_KEY")):
            with self.assertRaises(mod.APIError) as caught:
                self.api.status()
        self.assertNotIn("FAKE_KEY", str(caught.exception))

    def test_transport_error_is_ambiguous_not_success(self):
        with patch.object(mod, "open_request", side_effect=TimeoutError("fake secret")):
            with self.assertRaises(mod.APIError):
                self.api.stop()

    def test_no_redirects(self):
        self.assertIsNone(mod.NoRedirect().redirect_request(None, None, 302, "", {}, "https://other"))

    def test_recheck_vetoes_idle_shutdown(self):
        api = Mock()
        api.status.return_value = "RUNNING"
        self.assertEqual(mod.shutdown_step(api, "idle-timeout", lambda: False), "cancelled")
        api.stop.assert_not_called()

    def test_hard_cap_is_explicitly_interrupting(self):
        api = Mock()
        api.status.return_value = "RUNNING"
        self.assertEqual(mod.shutdown_step(api, "maximum-session-duration", lambda: False), "accepted")
        api.stop.assert_called_once()

    def test_exited_confirmation_does_not_repeat_post(self):
        api = Mock()
        api.status.return_value = "EXITED"
        self.assertEqual(mod.shutdown_step(api, "idle-timeout", lambda: True), "confirmed")
        api.stop.assert_not_called()

    def test_pending_stop_is_not_cancelled_by_failed_slots(self):
        api = Mock()
        api.status.return_value = "RUNNING"
        self.assertEqual(mod.shutdown_step(api, "idle-timeout", lambda: False, settling=True), "pending")
        api.stop.assert_not_called()

    def test_bad_key_file_permissions_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "key"
            path.write_text("FAKE_KEY")
            path.chmod(0o644)
            with self.assertRaises(ValueError):
                mod.read_private_file(path)


class MainTests(unittest.TestCase):
    def run_main(self, args, api=None):
        clock = [0.0]
        def sleep(seconds):
            clock[0] += seconds
            if clock[0] > 100:
                raise AssertionError("retry loop did not terminate")
        with patch.object(mod.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(mod.time, "time", side_effect=lambda: 1000 + clock[0]), \
             patch.object(mod.time, "sleep", side_effect=sleep), \
             patch.object(mod, "get_slot_state", return_value=True), \
             patch.object(mod, "RunPodAPI", return_value=api) as factory, \
             patch.dict(os.environ, {"RUNPOD_API_KEY": "FAKE_KEY"}), \
             patch("sys.argv", [str(PATH), "--max-hours", "0.0005", "--interval-seconds", "1"] + args), \
             patch("sys.stdout", new_callable=io.StringIO) as out:
            result = mod.main()
        return result, out.getvalue(), factory

    def test_dry_run_makes_no_cloud_requests(self):
        result, out, factory = self.run_main([])
        self.assertEqual(result, 0)
        self.assertIn("DRY-RUN", out)
        factory.assert_not_called()

    def test_failed_stop_retries_then_confirms(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = Mock()
            api.status.side_effect = ["RUNNING", "RUNNING", "EXITED"]
            api.stop.side_effect = [mod.APIError("HTTP 403"), None]
            result, out, _ = self.run_main(["--execute-stop", "--pod-id", "testpod", "--session-id", "test-run",
                                            "--state-file", str(Path(tmp) / "state.json")], api)
        self.assertEqual(result, 0)
        self.assertEqual(api.stop.call_count, 2)
        self.assertIn("Stop not confirmed", out)
        self.assertIn("EXITED confirmed", out)

    def test_timeout_after_acceptance_reconciles_before_post(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = Mock()
            api.status.side_effect = ["RUNNING", "EXITED"]
            api.stop.side_effect = mod.APIError("transport failure")
            result, _, _ = self.run_main(["--execute-stop", "--pod-id", "testpod", "--session-id", "test-run",
                                         "--state-file", str(Path(tmp) / "state.json")], api)
        self.assertEqual(result, 0)
        api.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
