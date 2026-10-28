"""Offline tests for the Modal deployment. No modal SDK, network, GPU or token."""
import ast
import http.server
import json
import os
import pathlib
import re
import sys
import tempfile
import threading
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "cloud"))
import bonsai_runtime as rt  # noqa: E402
import smoke  # noqa: E402

REV = "a" * 40


class SettingsTests(unittest.TestCase):
    def test_defaults_match_lab(self):
        s = rt.Settings.from_env({})
        self.assertEqual((s.gpu, s.context, s.parallel, s.vision), ("L40S", 65536, 1, False))

    def test_round_trip_through_image_env(self):
        s = rt.Settings.from_env({"BONSAI_MODAL_GPU": "A100-80GB", "BONSAI_CTX": "32768", "BONSAI_VISION": "1"})
        self.assertEqual(rt.Settings.from_env(s.as_env()), s)

    def test_rejects_bad_values(self):
        for env in ({"BONSAI_CTX": "1e5"}, {"BONSAI_CTX": "999999"}, {"BONSAI_SCALEDOWN_SECONDS": "5"},
                    {"BONSAI_PARALLEL": "-1"}, {"BONSAI_VISION": "yes"}, {"BONSAI_MODAL_GPU": "L40S; rm -rf /"}):
            with self.subTest(env=env), self.assertRaises(rt.ConfigError):
                rt.Settings.from_env(env)


class CommandTests(unittest.TestCase):
    def test_server_is_loopback_only_with_tool_calling(self):
        cmd = rt.llama_command(rt.Settings(), "/models/m.gguf")
        self.assertEqual(cmd[cmd.index("--host") + 1], "127.0.0.1")
        self.assertEqual(cmd[cmd.index("--port") + 1], str(rt.INTERNAL_PORT))
        self.assertNotEqual(rt.INTERNAL_PORT, rt.PUBLIC_PORT)
        for flag in ("--jinja", "--no-webui", "-fa"):
            self.assertIn(flag, cmd)
        self.assertNotIn("--mmproj", cmd)

    def test_vision_requires_projector(self):
        with self.assertRaises(rt.ConfigError):
            rt.llama_command(rt.Settings(vision=True), "/models/m.gguf", None)
        cmd = rt.llama_command(rt.Settings(vision=True), "/models/m.gguf", "/models/p.gguf")
        self.assertEqual(cmd[-2:], ["--mmproj", "/models/p.gguf"])


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "models"
        self.snap = self.root / "hf" / REV
        self.snap.mkdir(parents=True)
        (self.snap / "Ternary-Bonsai-2-27B-PQ2_0.gguf").write_bytes(b"GGUF")

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, data):
        (self.root / rt.MANIFEST_NAME).write_text(json.dumps(data))

    def test_round_trip_text_only(self):
        m = rt.build_manifest(self.root, self.snap, rt.HF_REPO, REV)
        rt.write_manifest(self.root, m)
        model, mmproj = rt.read_manifest(self.root)
        self.assertEqual(model.name, "Ternary-Bonsai-2-27B-PQ2_0.gguf")
        self.assertIsNone(mmproj)
        self.assertFalse((self.root / "current.json.tmp").exists())

    def test_round_trip_with_projector(self):
        (self.snap / "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf").write_bytes(b"GGUF")
        rt.write_manifest(self.root, rt.build_manifest(self.root, self.snap, rt.HF_REPO, REV))
        self.assertEqual(rt.read_manifest(self.root)[1].name, "Ternary-Bonsai-2-27B-mmproj-Q8_0.gguf")

    def test_requires_resolved_commit(self):
        with self.assertRaises(rt.ConfigError):
            rt.build_manifest(self.root, self.snap, rt.HF_REPO, "main")

    def test_ambiguous_model_rejected(self):
        (self.snap / "other-PQ2_0.gguf").write_bytes(b"GGUF")
        with self.assertRaises(rt.ConfigError):
            rt.build_manifest(self.root, self.snap, rt.HF_REPO, REV)

    def test_missing_manifest_explains_next_step(self):
        with self.assertRaisesRegex(rt.ConfigError, "download"):
            rt.read_manifest(self.root)

    def test_rejects_traversal_absolute_and_escaping_symlink(self):
        outside = pathlib.Path(self.tmp.name) / "outside.gguf"
        outside.write_bytes(b"GGUF")
        os.symlink(outside, self.root / "link.gguf")
        for bad in ("../outside.gguf", str(outside), "link.gguf", "hf/./x.gguf", ""):
            with self.subTest(bad=bad), self.assertRaises(rt.ConfigError):
                self.write({"format": 1, "model": bad})
                rt.read_manifest(self.root)

    def test_rejects_empty_model_and_wrong_format(self):
        (self.snap / "Ternary-Bonsai-2-27B-PQ2_0.gguf").write_bytes(b"")
        for data in ({"format": 1, "model": f"hf/{REV}/Ternary-Bonsai-2-27B-PQ2_0.gguf"},
                     {"format": 2, "model": "x"}, ["not", "a", "dict"]):
            with self.subTest(data=data), self.assertRaises(rt.ConfigError):
                self.write(data)
                rt.read_manifest(self.root)


class FakeProcess:
    def __init__(self, code=None):
        self.code = code

    def poll(self):
        return self.code


class HealthTests(unittest.TestCase):
    def serve(self, codes):
        replies = list(codes)

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(replies.pop(0) if len(replies) > 1 else replies[0])
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}/health"

    def test_waits_through_loading_503(self):
        url = self.serve([503, 503, 200])
        self.assertGreaterEqual(rt.wait_healthy(url, 10, FakeProcess(), poll=0.01), 0)

    def test_child_exit_fails_fast(self):
        url = self.serve([503])
        with self.assertRaisesRegex(RuntimeError, "code 7"):
            rt.wait_healthy(url, 10, FakeProcess(7), poll=0.01)

    def test_times_out_with_last_status(self):
        url = self.serve([503])
        with self.assertRaisesRegex(TimeoutError, "HTTP 503"):
            rt.wait_healthy(url, 0.2, FakeProcess(), poll=0.01)

    def test_rejects_nonfinite_timeout(self):
        with self.assertRaises(ValueError):
            rt.wait_healthy("http://127.0.0.1:9/", float("inf"), FakeProcess())


class SmokeClientTests(unittest.TestCase):
    def test_requires_both_proxy_token_parts(self):
        with self.assertRaises(SystemExit):
            smoke.headers({"MODAL_PROXY_TOKEN_ID": "wk-x"})
        h = smoke.headers({"MODAL_PROXY_TOKEN_ID": "wk-x", "MODAL_PROXY_TOKEN_SECRET": "ws-y"})
        self.assertEqual((h["Modal-Key"], h["Modal-Secret"]), ("wk-x", "ws-y"))

    def test_only_https_modal_urls(self):
        for bad in ("http://x.modal.run", "https://example.com", ""):
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                smoke.base_url({"BONSAI_MODAL_URL": bad})
        self.assertEqual(smoke.base_url({"BONSAI_MODAL_URL": "https://w--a.modal.run/"}), "https://w--a.modal.run")


class AppInvariantTests(unittest.TestCase):
    """Cost and exposure guarantees, checked statically without importing modal."""

    @classmethod
    def setUpClass(cls):
        cls.source = (REPO / "cloud" / "bonsai_modal.py").read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source)

    def decorator_kwargs(self, func_name, decorator_name):
        for node in ast.walk(self.tree):
            if isinstance(node, ast.FunctionDef) and node.name == func_name:
                for dec in node.decorator_list:
                    if isinstance(dec, ast.Call) and getattr(dec.func, "attr", "") == decorator_name:
                        return {k.arg: k.value for k in dec.keywords}
        self.fail(f"@{decorator_name} not found on {func_name}")

    def test_endpoint_requires_proxy_auth(self):
        value = self.decorator_kwargs("serve", "web_server")["requires_proxy_auth"]
        self.assertIs(ast.literal_eval(value), True)

    def test_scales_to_zero_with_single_gpu_ceiling(self):
        kw = self.decorator_kwargs("serve", "function")
        self.assertEqual(ast.literal_eval(kw["min_containers"]), 0)
        self.assertEqual(ast.literal_eval(kw["max_containers"]), 1)
        self.assertIn("scaledown_window", kw)

    def test_download_function_has_no_gpu(self):
        self.assertNotIn("gpu", self.decorator_kwargs("download", "function"))

    def test_binary_and_wheels_are_pinned(self):
        self.assertRegex(rt.LLAMA_SHA256, r"^[0-9a-f]{64}$")
        self.assertIn("sha256sum -c", self.source)
        for package in re.findall(r'"((?:nvidia|huggingface)[^"]*)"', self.source):
            self.assertIn("==", package)


if __name__ == "__main__":
    unittest.main()
