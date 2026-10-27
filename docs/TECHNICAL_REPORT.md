# Technical report: hybrid Bonsai 2 27B lab

**Updated:** 27 October 2026  
**Status:** local inference and OpenCode operational; RunPod start/stop, manual SSH tunnel and OpenCode CLOUD inference **validated**; persistent tunnel, Pod-side startup and watchdog **not yet deployed**.

## Architecture

- **nero:** Ubuntu 26.10, Ryzen 5 5600G, RTX 3060 (12 GB), RAM 32 GB. Bonsai 2 27B PQ2_0 runs as a systemd service, bound to `127.0.0.1:18080` (OpenAI-compatible `/v1`). Context configured to 65,536; GPU power limit systemd unit configured to 145 W.
- **OpenCode:** Docker container `opencode-lab`, OpenCode 1.19.4 with Python/document libraries and LibreOffice. `network_mode: host`; local Web service `127.0.0.1:4097`; private Tailscale Serve HTTPS on port 8443. Document agent can generate DOCX, XLSX and PDF files within a writable workspace.
- **obm:** Ubuntu ThinkPad T14 portable/backup environment; not required for cloud setup.
- **RunPod:** RTX 5090 32 GB, Ubuntu 24.04, CUDA Toolkit 12.8, driver 595.91.07, 60 GB RAM, 15 vCPU. GPU billed at approximately $1.20/hour in the tested configuration, plus possible stopped-Pod storage costs.

### Inference paths

```text
Mac browser -- private Tailscale HTTPS --> nero OpenCode (host network)
                                        |-- LOCAL: 127.0.0.1:18080 --> nero RTX 3060
                                        +-- CLOUD: 127.0.0.1:18082
                                                        |
                                                   SSH local forward
                                                        |
                                          RunPod 127.0.0.1:18080 --> RTX 5090
```

Do **not** expose the RunPod model HTTP API using its public HTTP proxy. The model server is bound to loopback and accessed only through direct-TCP SSH forwarding.

## Validated results

1. `runpod-bonsai start` using `POST /v2/pods/{id}/action` with `{"action":"start"}` succeeded (with confirmation).
2. `GET /v2/pods/{id}` reported `RUNNING`; the response included a direct TCP port mapping `private: 22, type: tcp`. SSH endpoint IP and port are dynamic; example addresses are intentionally omitted.
3. A dedicated Ed25519 key enabled noninteractive SSH authentication from `nero` to the Pod; manual `ssh -N -L 127.0.0.1:18082:127.0.0.1:18080` worked.
4. The Pod's `llama-server` had to be launched manually following a Pod restart; a stopped Pod does **not** currently restart inference automatically.
5. `curl http://127.0.0.1:18082/health` succeeded through the tunnel.
6. OpenCode Web completed a real chat inference with **Bonsai CLOUD** selected.
7. `runpod-bonsai stop` succeeded; subsequent Pod status was `EXITED`.

The working controller on `nero` is at `~/.local/bin/runpod-bonsai`. This repository's REST implementation is aligned with those operations but has **not yet been redeployed from GitHub** to `nero`.

### Model and benchmark

- Bonsai-demo repository: `PrismML-Eng/Bonsai-demo` at commit `294035e`.
- PrismML `llama.cpp` CUDA binary: build 10787, commit `73bfe6cc4`.
- `Ternary-Bonsai-2-27B-PQ2_0.gguf` (~6.8 GB); optional multimodal projector (~601 MB).
- Cloud sample: 1,000 generated tokens in 7,415.899 ms, **134.71 tokens/s** (42 prompt tokens, temperature 0, reasoning effort none).
- Local observed results: **34.6** and **36.8 tokens/s**, obtained under different measurements. Approximate speedup: **3.89×** or **3.66×**, respectively. These are **not controlled apples-to-apples benchmarks**.

## Current controller

`bin/runpod-bonsai` makes authenticated RunPod REST calls using `curl` and `jq` (no `runpodctl` requirement), with `status`, interactive-confirmation `start` and `stop`. Pod ID and API key are stored **only** in `~/.config/runpod.env`, mode 600.

RunPod API token was temporarily broadened to **All** after restricted permissions returned HTTP 403. Reduce to the minimum sufficient permissions as soon as RunPod's permission matrix is confirmed. Never place this token in GitHub.

The previously drafted `bin/runpod-tunnel` and systemd service have **not** been installed/tested in this live integration and currently rely on separately configured endpoint details. They must not be described as fully automated.

## Remaining engineering work

1. Discover the current direct TCP SSH `private:22` mapping from the Pod REST response on every start; validate IP and port, never hardcode.
2. Verify SSH host identity, then manage a dedicated known_hosts file without disabling host key checking.
3. Install `autossh` as a persistent systemd service on `nero`; start only when the Pod is ready and keep LOCAL independent.
4. Ensure Pod-side `llama-server` restarts automatically after Pod startup (the current RunPod container may not run systemd).
5. Implement and test an independent Pod-side watchdog monitoring `/slots`: protect active generation, stop after sustained inactivity, distinguish transient unhealthiness from persistent server failure, and enforce a deliberate session cap.
6. Test watchdog authorization and RunPod stop calls from **inside** the Pod before enabling actual shutdown. Keep dry-run default until validated.
7. Test stop/restart recovery, missing GPU capacity, SSH host rotation, and failure paths; verify Pod storage/billing and prepaid auto-top-up account settings.
8. Benchmark both GPUs using the same `llama-bench` options, model, prompt/generation sizes, driver and power settings.
9. Review any publication for credentials, access tokens, hostnames, sensitive internal or participant data.

## Operational note

The existing Pod is currently **stopped (`EXITED`)** after the successful end-to-end test. The LOCAL provider on `nero` continues independently.
