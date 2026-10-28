"""Bonsai 2 27B on Modal: scale-to-zero llama-server behind Modal proxy auth.

    modal run cloud/bonsai_modal.py::download            # once: model -> Volume (CPU only)
    modal deploy cloud/bonsai_modal.py                   # publish the endpoint
    python3 cloud/smoke.py                               # first request = cold start

Cost model: a GPU container exists only while requests arrive, plus
BONSAI_SCALEDOWN_SECONDS of idle time, and never more than one at a time.
Unauthenticated requests are rejected by Modal's proxy before any container
starts. See docs/MODAL.md.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import modal

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))  # locally: cloud/; in the container: /root
import bonsai_runtime as rt  # noqa: E402

SETTINGS = rt.Settings.from_env()  # read at deploy time, then baked into the image
PY_SITE = "/usr/local/lib/python3.12/site-packages"

image = (
    modal.Image.debian_slim(python_version="3.12")
    .apt_install("curl", "ca-certificates", "libgomp1", "openssl", "socat")
    .pip_install(
        "nvidia-cuda-runtime-cu12==12.8.90",
        "nvidia-cublas-cu12==12.8.5.5",
        "huggingface_hub==2.3.0",
    )
    .run_commands(
        f"curl -fsSL --retry 3 -o /tmp/llama.tgz {rt.LLAMA_URL}",
        f"echo '{rt.LLAMA_SHA256}  /tmp/llama.tgz' | sha256sum -c -",
        f"mkdir -p {rt.LLAMA_DIR} && tar -xzf /tmp/llama.tgz -C {rt.LLAMA_DIR} --strip-components=1"
        " && rm /tmp/llama.tgz",
    )
    .env({
        "LD_LIBRARY_PATH": f"{PY_SITE}/nvidia/cuda_runtime/lib:{PY_SITE}/nvidia/cublas/lib",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        **SETTINGS.as_env(),
    })
    # Fail the image build, not the first paid cold start, if a library is missing.
    # libcuda.so.1 is the only one expected to come from the GPU host's driver.
    .run_commands(
        f"! ldd {rt.LLAMA_DIR}/llama-server {rt.LLAMA_DIR}/libggml-cuda.so"
        " | grep -v 'libcuda.so.1' | grep 'not found'",
        f"{rt.LLAMA_DIR}/llama-server --version",
    )
    .add_local_file(HERE / "bonsai_runtime.py", "/root/bonsai_runtime.py")
)

app = modal.App("bonsai-cloud")
models = modal.Volume.from_name("bonsai-models", create_if_missing=True)


@app.function(image=image, volumes={rt.MODELS_DIR: models}, timeout=3600)
def download(revision: str = "main", vision: bool = False):
    """Fetch the PQ2_0 GGUF (and optionally the vision projector) onto the Volume.

    Runs on CPU. The branch/tag is resolved to a commit hash first, and the
    files land under hf/<commit>/, so the manifest always names one exact revision.
    """
    import json

    from huggingface_hub import HfApi, snapshot_download

    sha = HfApi().model_info(rt.HF_REPO, revision=revision).sha
    destination = Path(rt.MODELS_DIR) / "hf" / sha
    patterns = [rt.MODEL_PATTERN] + ([rt.MMPROJ_PATTERN] if vision else [])
    snapshot_download(rt.HF_REPO, revision=sha, allow_patterns=patterns, local_dir=destination)
    manifest = rt.build_manifest(rt.MODELS_DIR, destination, rt.HF_REPO, sha)
    rt.write_manifest(rt.MODELS_DIR, manifest)
    models.commit()
    print(json.dumps(manifest, indent=2))


def _exit_when_dead(process):
    """If llama-server dies, end the container so Modal replaces it cleanly."""
    code = process.wait()
    print(f"llama-server exited with code {code}; terminating container", flush=True)
    os._exit(1)


@app.function(
    image=image,
    gpu=SETTINGS.gpu,
    volumes={rt.MODELS_DIR: models},
    min_containers=0,  # scale to zero: no GPU billed while idle
    max_containers=1,  # hard ceiling: never more than one GPU at a time
    scaledown_window=SETTINGS.scaledown_seconds,
    timeout=SETTINGS.request_timeout_seconds,
)
@modal.concurrent(max_inputs=8)  # extra requests queue inside llama-server, not on a 2nd GPU
@modal.web_server(rt.PUBLIC_PORT, startup_timeout=60, requires_proxy_auth=True)
def serve():
    settings = rt.Settings.from_env()
    model, mmproj = rt.read_manifest(rt.MODELS_DIR)
    server = subprocess.Popen(rt.llama_command(settings, model, mmproj))
    try:
        elapsed = rt.wait_healthy(f"http://127.0.0.1:{rt.INTERNAL_PORT}/health",
                                  settings.startup_seconds, server)
    except BaseException:
        rt.stop(server)
        raise
    print(f"Model ready in {elapsed:.1f}s: {model.name}", flush=True)
    threading.Thread(target=_exit_when_dead, args=(server,), daemon=True).start()
    # Modal waits for this public port after serve() returns. It opens only now,
    # so the first request after a cold start never sees llama-server's loading 503.
    subprocess.Popen(["socat", f"TCP-LISTEN:{rt.PUBLIC_PORT},fork,reuseaddr,bind=0.0.0.0",
                      f"TCP:127.0.0.1:{rt.INTERNAL_PORT}"])
