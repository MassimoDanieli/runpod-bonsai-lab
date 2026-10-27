import importlib.util
import io
import json
import pathlib
import unittest
from unittest.mock import patch

P = pathlib.Path(__file__).resolve().parents[1] / 'bin' / 'bonsai-watchdog.py'
spec = importlib.util.spec_from_file_location('bonsai_watchdog', P)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class SlotsTests(unittest.TestCase):
    def mock_response(self, payload):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self): return json.dumps(payload).encode()
        return Response()

    def test_busy(self):
        with patch.object(mod.urllib.request, 'urlopen', return_value=self.mock_response([{'is_processing': True}])):
            self.assertTrue(mod.get_slot_state('http://localhost/slots'))

    def test_idle(self):
        with patch.object(mod.urllib.request, 'urlopen', return_value=self.mock_response([{'is_processing': False}])):
            self.assertFalse(mod.get_slot_state('http://localhost/slots'))

    def test_invalid(self):
        with patch.object(mod.urllib.request, 'urlopen', return_value=self.mock_response([{'other': True}])):
            with self.assertRaises(ValueError):
                mod.get_slot_state('http://localhost/slots')




class WatchdogStateTests(unittest.TestCase):
    def test_idle_timeout(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        self.assertEqual(w.observe(0, processing=False), ('idle', None))
        self.assertEqual(w.observe(60, processing=False), ('idle', 'idle-timeout'))

    def test_processing_resets_idle(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(55, processing=True)
        self.assertEqual(w.observe(100, processing=False), ('idle', None))
        self.assertEqual(w.observe(116, processing=False), ('idle', 'idle-timeout'))

    def test_transient_outage(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(40, error=True)
        self.assertEqual(w.observe(100, error=True), ('unhealthy', None))
        self.assertEqual(w.observe(110, processing=False), ('idle', None))

    def test_prolonged_outage(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        w.observe(10, error=True)
        self.assertEqual(w.observe(129, error=True), ('unhealthy', None))
        self.assertEqual(w.observe(130, error=True), ('unhealthy', 'prolonged-unhealthy-server'))

    def test_hard_ceiling_even_while_busy(self):
        w = mod.Watchdog(60, 120, 3600, 0)
        self.assertEqual(w.observe(3600, processing=True), ('processing', 'maximum-session-duration'))


if __name__ == '__main__': unittest.main()
