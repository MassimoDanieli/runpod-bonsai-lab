"""Pure runtime helpers for the Modal deployment. Standard library only.

Kept free of any `modal` import so the logic is unit-tested offline in CI.
The Modal app (bonsai_modal.py) imports this module both locally at deploy
time and inside the container.
"""
from __future__ import annotations

import fnmatch
import json
import math
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

# PrismML llama.cpp fork, prebuilt Linux CUDA 12.8 release. The archive ships
# native SASS for sm_86/sm_89/sm_120 and only PTX for sm_80/sm_90, so A100/H100
# would JIT-compile every kernel on each cold start: L40S (sm_89) is the default.
LLAMA_TAG = "prism-b10826-c9c34bc"
LLAMA_ASSET = f"llama-{LLAMA_TAG}-bin-linux-cuda-12.8-x64.tar.gz"
LLAMA_URL = f"https://github.com/PrismML-Eng/llama.cpp/releases/download/{LLAMA_TAG}/{LLAMA_ASSET}"
LLAMA_SHA256 = "e4345c9fed6452701fd511f9837726f39a45bc95fe103b8cae55360562eadf7f"
LLAMA_DIR = "/opt/llama"

HF_REPO = "prism-ml/Ternary-Bonsai-2-27B-gguf"
MODEL_PATTERN = "*-PQ2_0.gguf"
MMPROJ_PATTERN = "*mmproj-Q8_0.gguf"

MODELS_DIR = "/models"
MANIFEST_NAME = "current.json"
INTERNAL_PORT = 18080  # llama-server, loopback only inside the container
PUBLIC_PORT = 8080     # opened only after the model reports healthy

# PrismML's recommended Bonsai 2 sampling defaults; clients may override per request.
SAMPLING = ["--temp", "1.0", "--top-p", "0.95", "--top-k", "20", "--min-p", "0.05"]

_GPU_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}(:[1-8])?")


class ConfigError(ValueError):
    """Invalid deployment setting or model manifest."""


def _bounded_int(env, name, default, low, high):
    raw = env.get(name, str(default)).strip()
    if not re.fullmatch(r"[0-9]{1,7}", raw):
        raise ConfigError(f"{name} must be an integer")
    value = int(raw)
    if not low <= value <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return value


@dataclass(frozen=True)
class Settings:
    gpu: str = "L40S"
    context: int = 65536
    parallel: int = 1
    scaledown_seconds: int = 600
    startup_seconds: int = 900
    request_timeout_seconds: int = 1800
    vision: bool = False

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        gpu = env.get("BONSAI_MODAL_GPU", cls.gpu).strip()
        if not _GPU_RE.fullmatch(gpu):
            raise ConfigError("BONSAI_MODAL_GPU must look like 'L40S' or 'A100-80GB'")
        vision = env.get("BONSAI_VISION", "0").strip()
        if vision not in ("0", "1"):
            raise ConfigError("BONSAI_VISION must be 0 or 1")
        return cls(
            gpu=gpu,
            context=_bounded_int(env, "BONSAI_CTX", cls.context, 4096, 262144),
            parallel=_bounded_int(env, "BONSAI_PARALLEL", cls.parallel, 1, 8),
            scaledown_seconds=_bounded_int(env, "BONSAI_SCALEDOWN_SECONDS", cls.scaledown_seconds, 60, 3600),
            startup_seconds=_bounded_int(env, "BONSAI_STARTUP_SECONDS", cls.startup_seconds, 120, 1800),
            request_timeout_seconds=_bounded_int(env, "BONSAI_REQUEST_TIMEOUT_SECONDS",
                                                 cls.request_timeout_seconds, 60, 86400),
            vision=vision == "1",
        )

    def as_env(self):
        """Settings baked into the image, so the container sees deploy-time values."""
        return {
            "BONSAI_MODAL_GPU": self.gpu,
            "BONSAI_CTX": str(self.context),
            "BONSAI_PARALLEL": str(self.parallel),
            "BONSAI_SCALEDOWN_SECONDS": str(self.scaledown_seconds),
            "BONSAI_STARTUP_SECONDS": str(self.startup_seconds),
            "BONSAI_REQUEST_TIMEOUT_SECONDS": str(self.request_timeout_seconds),
            "BONSAI_VISION": "1" if self.vision else "0",
        }


def _safe_relative(value, field):
    if not isinstance(value, str) or not value:
        raise ConfigError(f"Manifest {field} must be a non-empty string")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ConfigError(f"Manifest {field} must be a plain relative path")
    return path


def _inside(root, relative, field):
    root = Path(root).resolve(strict=True)
    target = root.joinpath(*relative.parts).resolve(strict=False)
    if root not in target.parents:
        raise ConfigError(f"Manifest {field} escapes the models volume")
    if not target.is_file() or target.stat().st_size == 0:
        raise ConfigError(f"Manifest {field} is missing or empty: {relative}")
    return target


def select_one(directory, pattern, required=True):
    """Return the single file in `directory` (recursively) matching `pattern`."""
    directory = Path(directory)
    matches = sorted(p for p in directory.rglob("*")
                     if p.is_file() and fnmatch.fnmatchcase(p.name, pattern))
    if len(matches) > 1:
        raise ConfigError(f"Ambiguous: {len(matches)} files match {pattern}")
    if not matches:
        if required:
            raise ConfigError(f"No file matches {pattern} in {directory}")
        return None
    return matches[0]


def build_manifest(models_dir, snapshot_dir, repo, revision):
    if not re.fullmatch(r"[0-9a-f]{40}", revision or ""):
        raise ConfigError("Revision must be a resolved 40-character commit hash")
    models_dir = Path(models_dir).resolve(strict=True)
    model = select_one(snapshot_dir, MODEL_PATTERN).resolve(strict=True)
    mmproj = select_one(snapshot_dir, MMPROJ_PATTERN, required=False)
    entry = {"format": 1, "repo": repo, "revision": revision,
             "model": model.relative_to(models_dir).as_posix(),
             "model_bytes": model.stat().st_size, "mmproj": None}
    if mmproj is not None:
        entry["mmproj"] = mmproj.resolve(strict=True).relative_to(models_dir).as_posix()
    return entry


def write_manifest(models_dir, manifest):
    """Atomically replace the manifest; the previous one stays valid until rename."""
    final = Path(models_dir) / MANIFEST_NAME
    temp = final.with_suffix(".json.tmp")
    temp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, final)
    return final


def read_manifest(models_dir):
    """Validate the manifest and return absolute (model, mmproj-or-None) paths."""
    path = Path(models_dir) / MANIFEST_NAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ConfigError("No model on the volume yet: run the download function first") from None
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unreadable manifest: {exc}") from None
    if not isinstance(data, dict) or data.get("format") != 1:
        raise ConfigError("Unsupported manifest format")
    model = _inside(models_dir, _safe_relative(data.get("model"), "model"), "model")
    mmproj = None
    if data.get("mmproj") is not None:
        mmproj = _inside(models_dir, _safe_relative(data["mmproj"], "mmproj"), "mmproj")
    return model, mmproj


def llama_command(settings, model, mmproj=None, binary=f"{LLAMA_DIR}/llama-server"):
    command = [
        binary, "-m", str(model),
        "--host", "127.0.0.1", "--port", str(INTERNAL_PORT),
        "-ngl", "99", "-fa", "on", "-c", str(settings.context),
        "-np", str(settings.parallel), "--jinja", "--no-webui",
        *SAMPLING,
    ]
    if settings.vision:
        if mmproj is None:
            raise ConfigError("BONSAI_VISION=1 but no projector was downloaded (use --vision)")
        command += ["--mmproj", str(mmproj)]
    return command


def wait_healthy(url, timeout, process, poll=1.0, clock=time.monotonic, sleep=time.sleep):
    """Block until `url` answers 200, the child exits, or `timeout` elapses."""
    if not (math.isfinite(timeout) and timeout > 0):
        raise ValueError("timeout must be positive and finite")
    deadline = clock() + timeout
    last = "no response yet"
    while True:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"llama-server exited with code {code} before becoming healthy")
        try:
            with urllib.request.urlopen(url, timeout=min(5.0, max(0.1, deadline - clock()))) as reply:
                if reply.status == 200:
                    return clock() - (deadline - timeout)
                last = f"HTTP {reply.status}"
        except urllib.error.HTTPError as exc:  # 503 while the model loads
            last = f"HTTP {exc.code}"
            exc.close()
        except (urllib.error.URLError, OSError) as exc:
            last = type(exc).__name__
        if clock() >= deadline:
            raise TimeoutError(f"llama-server not healthy after {timeout:.0f}s ({last})")
        sleep(poll)


def stop(process, grace=20):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
