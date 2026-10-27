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


if __name__ == '__main__': unittest.main()
