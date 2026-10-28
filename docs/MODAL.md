# Bonsai 2 27B on Modal (serverless, scale to zero)

For occasional use (a few hours a month), a serverless GPU replaces most of the
RunPod machinery: there is no Pod to start, stop, migrate or forget running, no
SSH tunnel, no endpoint discovery and no Pod-side watchdog. A GPU container
exists only while requests arrive plus an idle window, then Modal removes it.

**Status:** working on Modal since 28 October 2026 (L40S): 401 without a
token, 11.8 s cold start, 72–76 tok/s, and a 12,000-token stream completed in
157 s. Still open: a real OpenCode session with tool calls, and confirming
scale-down and that rejected requests never start a container. See the checklist.

```text
OpenCode --HTTPS + Modal-Key/Modal-Secret--> Modal proxy (401 without token)
                                                 |
                                   container (L40S, scales 0..1)
                                   socat 0.0.0.0:8080 -> llama-server 127.0.0.1:18080
                                                 |
                                   Volume "bonsai-models": PQ2_0 GGUF at a pinned commit
```

## What runs where

| Piece | Source | Pinning |
| --- | --- | --- |
| `llama-server` | PrismML fork prebuilt release `prism-b10826-c9c34bc`, CUDA 12.8 | SHA-256 checked during image build |
| CUDA runtime + cuBLAS | `nvidia-cuda-runtime-cu12` / `nvidia-cublas-cu12` wheels | exact versions |
| Model | `prism-ml/Ternary-Bonsai-2-27B-gguf`, `*-PQ2_0.gguf` (~7 GB) | branch resolved to a commit hash, stored in `current.json` |
| Driver (`libcuda.so.1`) | Modal GPU host | provider-managed |

The image build fails if any shared library other than `libcuda.so.1` is
missing, so a broken image costs a build, not a paid cold start.

## Choosing the GPU

The PrismML binary contains native kernels for `sm_86`, `sm_89` and `sm_120`
only; A100 (`sm_80`) and H100 (`sm_90`) get PTX that the driver must JIT-compile
on every cold start. Prefer a GPU with native code:

| `BONSAI_MODAL_GPU` | Arch | Native kernels | Price/hour* | Expected speed |
| --- | --- | --- | --- | --- |
| `L40S` (default) | sm_89 | yes | $1.95 | **72–76 tok/s measured** for Bonsai 2 PQ2_0 on Modal (28 Oct 2026) |
| `A10` | sm_86 | yes | $1.10 | lower memory bandwidth; untested |
| `RTX-PRO-6000` | sm_120 | yes | $3.03 | same architecture as the RunPod RTX 5090 (143 tok/s for Bonsai 2 PQ2_0 per PrismML); untested on Modal |
| `A100-*`, `H100` | sm_80/90 | PTX only | $2.10–$3.95 | JIT on each cold start: avoid |

\*Third-party summary of Modal's per-second rates, August 2026
([continuumcode.ai](https://continuumcode.ai/guides/modal-pricing/)); confirm on
Modal's pricing page. The GPU string accepted by Modal for the RTX PRO 6000 must
also be confirmed before use. Speeds come from PrismML's
[community benchmarks](https://github.com/PrismML-Eng/Bonsai-demo/tree/main/community-benchmarks).

**Estimate for ~5 h/month on L40S:** 5 h of use plus ~10 idle tails of 10 min
is about 6.7 GPU-hours, roughly **$13/month**. The same Pod-hour budget on RunPod
was ~$6 of GPU, but storage of a stopped volume and the risk of a forgotten Pod
(48 h ≈ $58) disappear here.

## Setup (one time)

```bash
python3 -m pip install --user "modal==1.6.3"
modal setup                                   # browser login, stores ~/.modal.toml

# Download the model onto the Volume. CPU only: no GPU is billed.
modal run cloud/bonsai_modal.py::download                 # text only
modal run cloud/bonsai_modal.py::download --vision        # also the image projector

modal deploy cloud/bonsai_modal.py            # publishes the endpoint
```

Settings are read **at deploy time** and baked into the image; redeploy after
changing them. All are optional:

| Variable | Default | Range |
| --- | --- | --- |
| `BONSAI_MODAL_GPU` | `L40S` | Modal GPU name |
| `BONSAI_CTX` | `65536` | 4096–262144 |
| `BONSAI_PARALLEL` | `1` | 1–8 slots (context is split across slots) |
| `BONSAI_SCALEDOWN_SECONDS` | `600` | 60–3600 idle seconds before scale-down |
| `BONSAI_STARTUP_SECONDS` | `900` | model load budget |
| `BONSAI_REQUEST_TIMEOUT_SECONDS` | `1800` | Modal function timeout |
| `BONSAI_VISION` | `0` | `1` loads the projector (download with `--vision` first) |

Create a **proxy auth token** in the Modal dashboard (Settings → Proxy Auth
Tokens) or with the `modal workspace proxy-tokens` CLI, then keep it out of Git:

```bash
# The URL includes a region (e.g. ...-serve.us-east.modal.run); ask Modal for it
# rather than building it by hand. The dashboard link printed by deploy is not it.
export BONSAI_MODAL_URL=$(python3 -c "import modal; print(modal.Function.from_name('bonsai-cloud', 'serve').get_web_url())")
export MODAL_PROXY_TOKEN_ID=wk-...
export MODAL_PROXY_TOKEN_SECRET=ws-...
```

## Use

```bash
python3 cloud/smoke.py            # cold start + one short chat, prints tok/s
```

For OpenCode, merge [`cloud/opencode.example.json`](../cloud/opencode.example.json)
into your config and select **Bonsai CLOUD (Modal)**. The two Modal headers are
read from the environment; the `apiKey` field is unused because llama-server has
no key of its own and is reachable only through the authenticated proxy.

The first request after an idle period waits for the cold start. `serve()` keeps
the public port closed until `/health` returns 200, so that request reaches a
loaded model instead of llama-server's loading 503.

## Cost and exposure controls

- `min_containers=0`: nothing runs, and nothing is billed, while idle.
- `max_containers=1`: at most one GPU at a time; concurrent requests queue.
- `scaledown_window`: idle time before the container is removed.
- `requires_proxy_auth=True`: requests without the token get a 401 from Modal's
  proxy. Whether a container is ever started for such a request is not stated in
  Modal's docs: check this during acceptance.
- Kill switch: `modal app stop bonsai-cloud` stops the app and its containers.
- Set a spending limit in the Modal workspace billing settings if your plan has one.

Compared with the RunPod tunnel, the endpoint is a public HTTPS URL protected by
a token, and Modal terminates TLS and therefore sees request contents. Do not
send confidential or regulated data.

## Live acceptance checklist

Run once with the dashboard open; on L40S the whole list costs about $1 of GPU time.
Results from the first run, 28 October 2026, L40S:

- [x] `modal deploy` builds the image (binary checksum, library check, `--version`
      reports build 10826 / c9c34bc9c). GPU functions need a payment method on
      the workspace, even to run the CPU-only `download`.
- [x] `download` writes `current.json` with a 40-character revision (`serve` refuses
      to start otherwise).
- [x] `curl` without headers returns 401.
- [ ] No container appears in the dashboard for that rejected request.
- [x] `smoke.py`: cold start 11.8 s; chat 72.4 tok/s cold, 75.5 tok/s warm.
- [x] `smoke.py --long 12000`: HTTP 200, 12,002/12,000 chunks in 157 s. Modal's
      documented 150 s web request limit did not cut a continuous stream.
- [ ] A real OpenCode session with tool calls.
- [ ] After the idle window, the container disappears and billing stops.

**If long requests ever fail:** a stream that sends tokens continuously passed
157 s, but a request that stays silent for more than 150 s (for example a very
long prompt still being processed) was not tested. The fallback is Modal's newer
`@app.server()` class API, which has no fixed request timeout but answers 503
while scaled to zero, plus a small wake-up step before starting OpenCode.

## Relationship to the RunPod path

The RunPod scripts stay as they are. Once the checklist passes, the RunPod Pod
no longer holds anything unique: the model is re-downloaded from a pinned
revision and the binary from a pinned release, so the Pod and its storage can
be terminated to stop storage charges.
